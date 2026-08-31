# -*- coding: utf-8 -*-
"""
Top Growth Core 스캔 결과 → 섹터 드릴다운 메뉴 (메시지 1개)
================================================================================
darren_growth.py 실행 직후, 같은 워크플로우의 다음 스텝으로 실행한다.
state/growth_payload_{market}.json 을 읽어 섹터 목록 버튼 메시지를 1개 보낸다.
섹터를 탭하면 Cloudflare Worker 가 같은 메시지를 종목 버튼으로 편집한다.

기존 주식 스캔(send_chart_buttons.py)과 페이로드 형식이 같아서
Worker 쪽 키보드 빌더를 그대로 재사용한다. 다른 것은 콜백 접두어뿐이다.
  기존 스캔 : sec:/secback:   → state/last_{market}_buttons.json
  Growth   : gsec:/gsecback: → state/growth_payload_{market}.json

입력(환경변수):
  MARKET          : "US" 또는 "KR"
  DARREN_TG_TOKEN : 봇 토큰
  DARREN_TG_CHAT  : chat_id
"""
import os
import json

import requests

MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")

PAYLOAD = os.path.join("state", f"growth_payload_{MARKET.lower()}.json")
FLAG = {"US": "🇺🇸", "KR": "🇰🇷"}


def tg_send(text, reply_markup=None):
    if not TG_TOKEN or not TG_CHAT:
        print("[전송 생략 - 토큰/챗ID 없음]\n" + text)
        if reply_markup:
            print("[키보드]", json.dumps(reply_markup, ensure_ascii=False))
        return True
    body = {"chat_id": TG_CHAT, "text": text}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        res = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                            json=body, timeout=15)
        if res.status_code != 200:
            print(f"전송 실패({res.status_code}): {res.text[:300]}")
        return res.status_code == 200
    except Exception as e:
        print(f"전송 실패: {e}")
        return False


def main():
    if not os.path.exists(PAYLOAD):
        print(f"{PAYLOAD} 없음 — 메뉴 전송 생략")
        return
    try:
        with open(PAYLOAD, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"페이로드 파싱 실패: {e}")
        return

    sectors = data.get("sectors", [])
    total = data.get("total", 0)
    date = data.get("date", "")

    header = f"🌱 {FLAG.get(MARKET, '')} {MARKET} Top Growth Core · {date}"

    if not sectors or total == 0:
        tg_send(header + "\n\n통과 종목이 없습니다.\n"
                "매출 성장 + 정배열 + 횡보 조건을 동시에 만족하는 종목이 없는 구간입니다.")
        print(f"{MARKET} Growth: 통과 0 — 요약만 전송")
        return

    keyboard = []
    row = []
    for idx, s in enumerate(sectors):
        row.append({"text": f"{s['name']} ({len(s['tickers'])})",
                    "callback_data": f"gsec:{MARKET}:{idx}"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    tg_send(
        f"{header}\n{total}종목 · {len(sectors)}개 섹터\n"
        "매출성장 25%↑ · 정배열 · ADR 3%↑ · 최근 1주 ±5% (ADR 순)\n"
        "섹터를 선택하면 종목 버튼이 나옵니다.",
        {"inline_keyboard": keyboard},
    )
    print(f"{MARKET} Growth 메뉴 전송 완료 — {total}종목 · {len(sectors)}개 섹터")


if __name__ == "__main__":
    main()
