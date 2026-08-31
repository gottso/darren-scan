# -*- coding: utf-8 -*-
"""
텔레그램 채널 방송 — 스캔 결과 정적 다이제스트
================================================================================
채널 구독자에게 스캔 결과를 읽기 전용으로 뿌린다.
스캔 워크플로우의 마지막 스텝으로 실행한다.

【왜 버튼을 안 쓰는가】
채널에서 인라인 버튼을 누르면 editMessageText 가 그 메시지 하나를 고치는데,
채널 메시지는 모든 구독자가 같은 것을 보므로 A가 섹터를 누르면 B의 화면도
같이 바뀐다. 그래서 채널은 정적 텍스트로만 보내고, 드릴다운은 봇 DM에서만
제공한다(봇 DM은 사람마다 별도 메시지라 안전).

【데이터 출처】
CSV를 다시 파싱하지 않고 이미 커밋된 state/*.json 을 읽는다.
DM 메뉴와 채널 방송이 같은 데이터를 보게 되어 어긋날 여지가 없다.
  state/last_{market}_buttons.json     주식 섹터·종목
  state/last_{market}_diff.json        추가/유지/제외
  state/etf_payload_{market}.json      ETF 카테고리·종목
  state/growth_payload_{market}.json   Top Growth Core

【입력(환경변수)】
  MODE                : "stock" | "etf" | "growth"
  MARKET              : "US" 또는 "KR"
  DARREN_TG_TOKEN     : 봇 토큰
  DARREN_TG_CHANNEL   : 채널 ID (@myChannel 또는 -1001234567890)
                        비어 있으면 아무것도 하지 않고 조용히 끝난다
  CHANNEL_MAX_TICKERS : 섹터당 표시할 최대 종목 수 (기본 25)

【사전 준비】
봇을 채널 관리자로 추가하고 "메시지 게시" 권한을 켜야 한다.
"""
import os
import json
import datetime as dt

import requests

MODE = os.environ.get("MODE", "stock").strip().lower()
MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
CHANNEL = os.environ.get("DARREN_TG_CHANNEL", "").strip()
MAX_TICKERS = int(os.environ.get("CHANNEL_MAX_TICKERS", "25"))

STATE_DIR = "state"
TG_LIMIT = 3800          # 4096 제한에 여유를 둔 값
TIER_EMOJI = {"A": "🟢", "B": "🟡", "C": "⚪"}
FLAG = {"US": "🇺🇸", "KR": "🇰🇷"}

DISCLAIMER = (
    "※ 가격·거래량 구조만으로 뽑은 후보군이며 매수 신호가 아닙니다.\n"
    "   진입 판단은 각자 차트를 직접 확인하세요."
)


# ══════════════════════════════════════════════════════════════
# 전송
# ══════════════════════════════════════════════════════════════

def send(text):
    if not TG_TOKEN or not CHANNEL:
        print("[채널 미설정 - 전송 생략]\n" + text)
        return True
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHANNEL, "text": text,
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if res.status_code != 200:
            print(f"채널 전송 실패({res.status_code}): {res.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"채널 전송 예외: {e}")
        return False


def send_chunks(blocks, header):
    """블록 목록을 글자수 제한에 맞춰 여러 메시지로 나눠 보낸다.

    블록은 쪼개지 않는다 — 섹터가 메시지 경계에서 잘리면 읽기 나빠지므로,
    블록 단위로만 넘긴다.
    """
    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > TG_LIMIT:
            msgs.append(cur)
            cur = f"{header.splitlines()[0]} (계속)\n\n{b}"
        else:
            cur += "\n\n" + b if cur else b
    if cur.strip():
        msgs.append(cur)
    for i, m in enumerate(msgs):
        tail = f"\n\n{DISCLAIMER}" if i == len(msgs) - 1 else ""
        if not send(m + tail):
            return False
    return True


def load(path):
    p = os.path.join(STATE_DIR, path)
    if not os.path.exists(p):
        print(f"{p} 없음")
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"{p} 파싱 실패: {e}")
        return None


# ══════════════════════════════════════════════════════════════
# 주식 방송
# ══════════════════════════════════════════════════════════════

def broadcast_stock():
    state = load(f"last_{MARKET.lower()}_buttons.json")
    if not state or not state.get("sectors"):
        print("주식 스캔 결과 없음 — 방송 생략")
        return

    date = state.get("date") or dt.date.today().isoformat()
    sectors = state["sectors"]
    total = state.get("total", sum(len(s["tickers"]) for s in sectors))

    header = (f"{FLAG.get(MARKET, '')} {MARKET} 워치리스트 · {date}\n"
              f"상위 {total}종목 · {len(sectors)}개 섹터")

    # 변화 리포트가 있으면 요약 한 줄을 덧붙인다
    diff = load(f"last_{MARKET.lower()}_diff.json")
    if diff:
        header += (f"\n➕ 추가 {len(diff.get('added', []))}"
                   f" · ✅ 유지 {len(diff.get('kept', []))}"
                   f" · ➖ 제외 {len(diff.get('removed', []))}")

    blocks = []

    # 추가 종목은 이번 주 새로 들어온 것이라 따로 앞에 보여준다
    if diff and diff.get("added"):
        added = diff["added"][:30]
        names = [f"{t} {n}".strip() for t, n in added]
        more = f" 외 {len(diff['added']) - len(added)}개" if len(diff["added"]) > len(added) else ""
        blocks.append("━━ ➕ 이번 주 신규 ━━\n" + ", ".join(names) + more)

    for s in sectors:
        tks = s["tickers"][:MAX_TICKERS]
        more = f" 외 {len(s['tickers']) - len(tks)}개" if len(s["tickers"]) > len(tks) else ""
        blocks.append(f"━━ {s['name']} ({len(s['tickers'])}) ━━\n" + ", ".join(tks) + more)

    send_chunks(blocks, header)
    print(f"{MARKET} 주식 채널 방송 완료 — {len(sectors)}개 섹터")


# ══════════════════════════════════════════════════════════════
# ETF 방송
# ══════════════════════════════════════════════════════════════

def broadcast_etf():
    payload = load(f"etf_payload_{MARKET.lower()}.json")
    mdata = payload.get(MARKET.lower()) if payload else None
    if not mdata:
        print("ETF 페이로드 없음 — 방송 생략")
        return

    menu = mdata.get("menu", [])
    tickers = mdata.get("tickers", [])
    count = len(tickers) if tickers else sum(m.get("count", 0) for m in menu)
    date = (mdata.get("generated_at") or "")[:10] or dt.date.today().isoformat()

    header = f"{FLAG.get(MARKET, '')} {MARKET} ETF 스캔 · {date}\n{count}종목 · {len(menu)}개 카테고리"
    if mdata.get("benchmark_ok") is False:
        header += "\n⚠️ 벤치마크 조회 실패 — RS(상대강도) 판정 무효"

    if not menu:
        send(header + "\n\n통과 종목이 없습니다.\n필터를 푸는 대신 관망 구간으로 보시면 됩니다."
             + f"\n\n{DISCLAIMER}")
        print(f"{MARKET} ETF 채널 방송 완료 — 통과 0")
        return

    header += "\n🟢 TierA  🟡 TierB  ⚪ TierC"

    blocks = []
    cats = mdata.get("categories", {})
    for entry in menu:              # darren_core.sector_heat 순서를 그대로 따른다
        cat = entry["category"]
        items = cats.get(cat, [])[:MAX_TICKERS]
        if not items:
            continue
        label = entry.get("label", "")
        mark = "🔥 " if label.startswith("A") and _tier_a(label) >= 3 else ""
        lines = []
        for it in items:
            emo = TIER_EMOJI.get(it.get("tier"), "⚪")
            lev = f" {it['lev']:g}x" if it.get("lev") and it["lev"] != 1 else ""
            score = f" {it['score']:.0f}" if isinstance(it.get("score"), (int, float)) else ""
            lines.append(f"{emo} {it['ticker']}{lev}{score}")
        more = (f"\n… 외 {len(cats.get(cat, [])) - len(items)}개"
                if len(cats.get(cat, [])) > len(items) else "")
        blocks.append(f"━━ {mark}{cat} ({label}) ━━\n" + "  ".join(lines) + more)

    send_chunks(blocks, header)
    print(f"{MARKET} ETF 채널 방송 완료 — {len(menu)}개 카테고리")


def broadcast_growth():
    """Top Growth Core — 기존 7개 필터 스캔과는 별개의 스크린."""
    data = load(f"growth_payload_{MARKET.lower()}.json")
    if not data:
        print("Growth 페이로드 없음 — 방송 생략")
        return

    sectors = data.get("sectors", [])
    total = data.get("total", 0)
    date = data.get("date", dt.date.today().isoformat())

    header = (f"🌱 {FLAG.get(MARKET, '')} {MARKET} Top Growth Core · {date}\n"
              f"{total}종목 · {len(sectors)}개 섹터\n"
              "매출성장 25%↑ · 정배열 · ADR 3%↑ · 최근 1주 ±5% (ADR 순)")

    if not sectors or total == 0:
        send(header + "\n\n통과 종목이 없습니다.\n"
             "성장 + 정배열 + 횡보를 동시에 만족하는 종목이 없는 구간입니다."
             + f"\n\n{DISCLAIMER}")
        print(f"{MARKET} Growth 채널 방송 완료 — 통과 0")
        return

    blocks = []
    for s_ in sectors:
        tks = s_["tickers"][:MAX_TICKERS]
        more = f" 외 {len(s_['tickers']) - len(tks)}개" if len(s_["tickers"]) > len(tks) else ""
        blocks.append(f"━━ {s_['name']} ({len(s_['tickers'])}) ━━\n" + ", ".join(tks) + more)

    send_chunks(blocks, header)
    print(f"{MARKET} Growth 채널 방송 완료 — {len(sectors)}개 섹터")


def _tier_a(label):
    """'A3/B1/C7' 에서 A 개수만 뽑는다."""
    for part in str(label).split("/"):
        part = part.strip()
        if part[:1] == "A" and part[1:].isdigit():
            return int(part[1:])
    return 0


# ══════════════════════════════════════════════════════════════

def main():
    if not CHANNEL:
        print("DARREN_TG_CHANNEL 미설정 — 채널 방송을 건너뜁니다.")
        return
    if MODE == "etf":
        broadcast_etf()
    elif MODE == "growth":
        broadcast_growth()
    else:
        broadcast_stock()


if __name__ == "__main__":
    main()
