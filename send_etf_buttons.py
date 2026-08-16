# -*- coding: utf-8 -*-
"""
ETF 스캔 결과 → 3단 드릴다운 메뉴 전송 (메시지 1개)
================================================================================
darren_etf_scan.py 실행 직후, 같은 워크플로우의 다음 스텝으로 실행된다.
darren_etf_scan.py 가 저장한 state/etf_payload.json 을 읽어서
텔레그램 메시지를 딱 1개만 보낸다.

드릴다운 구조 (전부 같은 메시지를 편집 — 새 메시지가 쌓이지 않음):
  [1단] 시장 선택      🇺🇸 US ETF (53) / 🇰🇷 KR ETF (0)
  [2단] 카테고리 선택   반도체 (A3/B1/C7)   ← 섹터 히트 라벨
  [3단] 종목 버튼      🟢 SOXX  🟡 XLK  ⚪ IYF   ← 티어 이모지

2·3단 편집은 Cloudflare Worker가 GitHub Contents API로
state/etf_payload.json 을 읽어서 처리한다. 이 스크립트는 1단 메시지만 보낸다.
(커밋은 워크플로우의 git 커밋 스텝이 처리)

추가 기능: 통과 종목 수 추이를 state/etf_count_history.json 에 누적한다.
핸드오프 문서 §7의 "통과 개수 추이(0 → 3 → 8) 자체가 시장 건강도 지표"에 해당.
종목이 0개여도 이 추이 때문에 매주 돌릴 가치가 있다.

입력(환경변수):
  DARREN_TG_TOKEN : 텔레그램 봇 토큰
  DARREN_TG_CHAT  : 텔레그램 chat_id
  ETF_PAYLOAD     : 페이로드 경로 (기본 state/etf_payload.json)
"""
import os
import json
import datetime as dt

import requests

TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")

STATE_DIR = "state"
PAYLOAD_FILE = os.environ.get("ETF_PAYLOAD", os.path.join(STATE_DIR, "etf_payload.json"))
HISTORY_FILE = os.path.join(STATE_DIR, "etf_count_history.json")
HISTORY_KEEP = 8  # 최근 8회분만 보관

MARKET_LABEL = {"us": "🇺🇸 US", "kr": "🇰🇷 KR"}


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
            return json.load(f)
    except Exception as e:
        print(f"{PAYLOAD_FILE} 파싱 실패: {e}")
        return None


def market_count(mdata):
    """해당 시장의 통과 종목 수. tickers 우선, 없으면 menu의 count 합계."""
    if not mdata:
        return 0
    tickers = mdata.get("tickers")
    if isinstance(tickers, list):
        return len(tickers)
    return sum(int(m.get("count", 0)) for m in mdata.get("menu", []))


def update_history(counts):
    """통과 종목 수 추이 누적 → 최근 몇 회분 반환."""
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
    entry = {"date": today, **counts}
    # 같은 날 재실행이면 덮어쓴다 (수동 재실행 시 중복 방지)
    history = [h for h in history if h.get("date") != today]
    history.append(entry)
    history = history[-HISTORY_KEEP:]

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)
    return history


def format_trend(history, market):
    """예: '0 → 3 → 8' (최근 5회)"""
    vals = [str(h.get(market, 0)) for h in history[-5:]]
    return " → ".join(vals) if len(vals) > 1 else None


def main():
    payload = load_payload()
    if not payload:
        return

    counts = {m: market_count(payload.get(m)) for m in ("us", "kr")}
    history = update_history(counts)

    # 생성 시각 (payload 우선, 없으면 오늘)
    gen = ""
    for m in ("us", "kr"):
        g = (payload.get(m) or {}).get("generated_at", "")
        if g:
            gen = g[:10]
            break
    if not gen:
        gen = dt.date.today().isoformat()

    lines = [f"📦 ETF 스캔 ({gen})"]

    for m in ("us", "kr"):
        mdata = payload.get(m)
        if mdata is None:
            continue
        line = f"{MARKET_LABEL[m]} {counts[m]}종목"
        trend = format_trend(history, m)
        if trend:
            line += f"   (추이 {trend})"
        lines.append(line)

    # 벤치마크 실패 경고 — RS(상대강도) 판정이 무효인 상태이므로 반드시 표시
    bad = [MARKET_LABEL[m] for m in ("us", "kr")
           if payload.get(m) is not None and payload[m].get("benchmark_ok") is False]
    if bad:
        lines.append("")
        lines.append(f"⚠️ 벤치마크 조회 실패 ({', '.join(bad)}) — RS(상대강도) 판정 무효. 티어를 신뢰하지 마세요.")

    if counts["us"] == 0 and counts["kr"] == 0:
        lines.append("")
        lines.append("통과 종목이 없습니다. 필터를 푸는 대신 신호등 RED로 간주하고 관망하세요.")
        tg_send("\n".join(lines))
        return

    lines.append("")
    lines.append("시장을 선택하세요. (🟢 TierA  🟡 TierB  ⚪ TierC)")

    keyboard = [[
        {"text": f"{MARKET_LABEL[m]} ETF ({counts[m]})", "callback_data": f"etfmkt:{m}"}
        for m in ("us", "kr") if payload.get(m) is not None
    ]]

    tg_send("\n".join(lines), {"inline_keyboard": keyboard})
    print(f"ETF 메뉴 전송 완료 — US {counts['us']} / KR {counts['kr']}")


if __name__ == "__main__":
    main()
