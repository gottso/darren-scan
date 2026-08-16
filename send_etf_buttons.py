# -*- coding: utf-8 -*-
"""
ETF 스캔 결과 → 카테고리 드릴다운 메뉴 전송 (시장별 단일 스캔, 메시지 1개)
================================================================================
darren_etf_scan.py --market us (또는 --market kr) 실행 직후, 같은 워크플로우의
다음 스텝으로 실행된다. US ETF와 KR ETF는 이제 완전히 분리된 워크플로우로
각자 따로 돌기 때문에, 이 스크립트도 기존 주식 스크립트(send_chart_buttons.py)
와 동일하게 MARKET 환경변수 하나로 단일 시장만 처리한다.

메시지는 딱 1개: 카테고리 목록 버튼 (기존처럼 시장부터 고르는 단계 없음 —
US ETF 결과는 US ETF 워크플로우에서만 오므로 애초에 고를 필요가 없다).
카테고리 탭 → 종목 버튼으로 편집(드릴다운)하는 건 Cloudflare Worker가
state/etf_payload_{market}.json 을 GitHub Contents API로 읽어서 처리한다.

카테고리 정렬은 darren_core.sector_heat() 가 이미 정한 순서(A 많은 순)를
그대로 따른다 — 여기서 또 정렬하면 worker.js와 조용히 어긋난다(실제로 한 번
그래서 순서가 깨진 적 있음). 정렬 기준을 바꾸려면 darren_core 쪽을 고칠 것.

추가 기능: 통과 종목 수 추이를 state/etf_count_history_{market}.json 에
시장별로 따로 누적한다. "통과 개수 추이 자체가 시장 건강도 지표"이므로
0개인 주에도 매주 돌릴 가치가 있다는 게 원래 설계 의도.

입력(환경변수):
  MARKET          : "US" 또는 "KR"
  DARREN_TG_TOKEN : 텔레그램 봇 토큰
  DARREN_TG_CHAT  : 텔레그램 chat_id
  ETF_PAYLOAD     : 페이로드 경로 (기본 state/etf_payload_{market}.json)
"""
import os
import json
import datetime as dt

import requests

MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")

STATE_DIR = "state"
PAYLOAD_FILE = os.environ.get(
    "ETF_PAYLOAD", os.path.join(STATE_DIR, f"etf_payload_{MARKET.lower()}.json")
)
HISTORY_FILE = os.path.join(STATE_DIR, f"etf_count_history_{MARKET.lower()}.json")
HISTORY_KEEP = 8  # 최근 8회분만 보관

MARKET_LABEL = {"US": "🇺🇸 US", "KR": "🇰🇷 KR"}
TIER_EMOJI = {"A": "🟢", "B": "🟡", "C": "⚪"}


def tg_send(text, reply_markup=None):
    if not TG_TOKEN or not TG_CHAT:
        print("[전송 생략 - DARREN_TG_TOKEN/DARREN_TG_CHAT 없음]\n" + text)
        if reply_markup:
            print("[키보드]", json.dumps(reply_markup, ensure_ascii=False))
        return True
    body = {"chat_id": TG_CHAT, "text": text}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=body, timeout=15,
        )
        if res.status_code != 200:
            print(f"전송 실패({res.status_code}): {res.text[:300]}")
        return res.status_code == 200
    except Exception as e:
        print(f"전송 실패: {e}")
        return False


def load_payload():
    if not os.path.exists(PAYLOAD_FILE):
        print(f"{PAYLOAD_FILE} 이 없습니다. ETF 메뉴 전송을 건너뜁니다.")
        return None
    try:
        with open(PAYLOAD_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"{PAYLOAD_FILE} 파싱 실패: {e}")
        return None
    # darren_etf_scan.py --json 출력은 {"us": {...}} 또는 {"kr": {...}} 형태
    return data.get(MARKET.lower())


def market_count(mdata):
    if not mdata:
        return 0
    tickers = mdata.get("tickers")
    if isinstance(tickers, list):
        return len(tickers)
    return sum(int(m.get("count", 0)) for m in mdata.get("menu", []))


def update_history(count):
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                history = loaded
        except Exception as e:
            print(f"추이 파일 읽기 실패(무시하고 새로 시작): {e}")

    today = dt.date.today().isoformat()
    history = [h for h in history if h.get("date") != today]  # 같은 날 재실행 시 덮어쓰기
    history.append({"date": today, "count": count})
    history = history[-HISTORY_KEEP:]

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)
    return history


def format_trend(history):
    vals = [str(h.get("count", 0)) for h in history[-5:]]
    return " → ".join(vals) if len(vals) > 1 else None


def parse_tier_label(label):
    out = {"A": 0, "B": 0, "C": 0}
    if not label:
        return out
    for part in str(label).split("/"):
        part = part.strip()
        if part[:1] in out and part[1:].isdigit():
            out[part[0]] = int(part[1:])
    return out


def category_keyboard(menu):
    """menu 순서를 그대로 따른다 (darren_core.sector_heat 가 이미 정렬함 — 재정렬 금지)."""
    keyboard = []
    for idx, entry in enumerate(menu):
        tiers = parse_tier_label(entry.get("label"))
        mark = "🔥 " if tiers["A"] >= 3 else ""
        label = f" ({entry['label']})" if entry.get("label") else f" ({entry.get('count', 0)})"
        keyboard.append([{
            "text": f"{mark}{entry['category']}{label}",
            "callback_data": f"etfcat:{MARKET}:{idx}",
        }])
    return keyboard


def main():
    mdata = load_payload()
    if mdata is None:
        return

    count = market_count(mdata)
    history = update_history(count)
    trend = format_trend(history)

    gen = (mdata.get("generated_at") or "")[:10] or dt.date.today().isoformat()
    menu = mdata.get("menu", [])

    lines = [f"📦 {MARKET_LABEL.get(MARKET, MARKET)} ETF ({gen})"]
    line = f"{count}종목"
    if trend:
        line += f"   (추이 {trend})"
    lines.append(line)

    if mdata.get("benchmark_ok") is False:
        lines.append("")
        lines.append("⚠️ 벤치마크 조회 실패 — RS(상대강도) 판정 무효. 티어를 신뢰하지 마세요.")

    if not menu or count == 0:
        lines.append("")
        lines.append("통과 종목이 없습니다. 필터를 푸는 대신 신호등 RED로 간주하고 관망하세요.")
        tg_send("\n".join(lines))
        print(f"{MARKET} ETF: 통과 0 — 메뉴 없이 요약만 전송")
        return

    lines.append("")
    lines.append(f"{len(menu)}개 카테고리. A가 많은 순. (🟢A 🟡B ⚪C, 🔥=A 3개 이상)")

    tg_send("\n".join(lines), {"inline_keyboard": category_keyboard(menu)})
    print(f"{MARKET} ETF 메뉴 전송 완료 — {count}종목 · {len(menu)}개 카테고리")


if __name__ == "__main__":
    main()
