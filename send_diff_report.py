# -*- coding: utf-8 -*-
"""
스캔 변화 리포트 (➕추가 / ✅유지 / ➖제외) — 요약 1개 메시지 + 드릴다운
================================================================================
darren_us_screener.py / darren_kr_screener.py 실행 직후, 같은 워크플로우의
다음 스텝으로 실행된다. 이번 스캔 CSV의 티커 목록을 직전 스캔 목록과 비교해서
추가/유지/제외를 분류한다.

(구버전은 요약 → 추가 → 제외 → 유지 순서로 메시지를 여러 개 보내서 US처럼
목록이 길면 채팅창이 리포트로 도배됐다. 이번 버전은 요약 1줄만 담긴 메시지
1개를 보내고, "➕ 추가 보기" 등을 탭하면 Cloudflare Worker가 같은 메시지를
해당 목록으로 편집한다 — 새 메시지가 쌓이지 않음.)

Worker가 드릴다운 시점에 목록 데이터를 알아야 하므로, 텔레그램 전송과
별개로 state/last_{market}_diff.json 에 저장해 둔다. 커밋은 워크플로우의
기존 "비교용 상태 파일 커밋" 스텝(`git add state/`)이 그대로 처리한다.

핵심 설계:
- GitHub Actions는 실행마다 파일시스템이 초기화되므로, 직전 스캔 목록을
  저장소 안의 작은 텍스트 파일(state/last_us_tickers.txt 등)로 커밋해서 보존한다.
- 기존 스크리너 스크립트는 전혀 건드리지 않는다.

동작 규칙:
- CSV가 없으면(스캔 실패 또는 통과 0종목) 아무것도 하지 않고 상태 파일도
  건드리지 않는다 → 실패한 주에 기준선이 날아가는 것을 방지.
- 상태 파일이 없으면(첫 실행) 비교 없이 "첫 실행" 안내만 보내고 기준선을 저장.
- 두 번째 실행부터 요약 + 드릴다운 리포트가 나간다.

입력(환경변수):
  MARKET          : "US" 또는 "KR"
  DARREN_TG_TOKEN : 텔레그램 봇 토큰
  DARREN_TG_CHAT  : 텔레그램 chat_id
  SHOW_KEPT       : "0"이면 요약에 유지 "보기" 버튼을 안 붙임(개수만 표시). 기본 "1"
"""
import os
import glob
import json

import pandas as pd
import requests

MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")
SHOW_KEPT = os.environ.get("SHOW_KEPT", "1").strip() != "0"

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, f"last_{MARKET.lower()}_tickers.txt")
DIFF_STATE_FILE = os.path.join(STATE_DIR, f"last_{MARKET.lower()}_diff.json")

# CSV 컬럼명이 스크리너 버전에 따라 다를 수 있어 후보를 여러 개 두고 자동 탐지
# (실제 확인된 컬럼: '티커','종목명','시장','sector','종가','advol60_억','natr50_%','gap20선_%','봉수','ipo')
TICKER_COL_CANDIDATES = ["티커", "ticker", "Ticker", "종목", "종목코드", "symbol", "Symbol"]
NAME_COL_CANDIDATES = ["종목명", "name", "Name", "이름", "회사명", "company"]


# ------------------------------------------------------------
# 공통 유틸
# ------------------------------------------------------------
def find_column(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def find_latest_csv():
    pattern = f"darren_{MARKET.lower()}_watchlist_*.csv"
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def tg_send(text, reply_markup=None):
    if not TG_TOKEN or not TG_CHAT:
        print("[전송 생략 - 토큰/챗ID 없음]\n" + text)
        return True
    body = {"chat_id": TG_CHAT, "text": text}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=body, timeout=15,
        )
        return res.status_code == 200
    except Exception as e:
        print(f"전송 실패: {e}")
        return False


def normalize_ticker(raw):
    t = str(raw).strip().upper()
    if not t or t in ("NAN", "NONE"):
        return ""
    # KR 종목코드가 CSV에서 앞자리 0이 잘린 채 읽힐 경우 복원 (예: 5930 → 005930)
    if MARKET == "KR" and t.isdigit() and len(t) < 6:
        t = t.zfill(6)
    return t


def clean_name(raw):
    n = str(raw).strip()
    if not n or n.lower() in ("nan", "none"):
        return ""
    return n[:20]


# ------------------------------------------------------------
# 상태 파일 (직전 스캔 목록) 읽기/쓰기
# 형식: 한 줄에 "티커<TAB>종목명" (종목명은 없을 수 있음)
# ------------------------------------------------------------
def load_prev_state():
    if not os.path.exists(STATE_FILE):
        return None
    items = []
    with open(STATE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            t = normalize_ticker(parts[0])
            n = clean_name(parts[1]) if len(parts) > 1 else ""
            if t:
                items.append((t, n))
    return items


def save_state(items):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for t, n in items:
            f.write(f"{t}\t{n}\n" if n else f"{t}\n")


def save_diff_state(added, removed, kept):
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {
        "market": MARKET,
        "date": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "added": [[t, n] for t, n in added],
        "removed": [[t, n] for t, n in removed],
        "kept": [[t, n] for t, n in kept],
    }
    with open(DIFF_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def summary_keyboard(added_count, removed_count, kept_count):
    row1 = []
    if added_count:
        row1.append({"text": f"➕ 추가 {added_count}", "callback_data": f"diffview:{MARKET}:added"})
    if removed_count:
        row1.append({"text": f"➖ 제외 {removed_count}", "callback_data": f"diffview:{MARKET}:removed"})
    keyboard = [row1] if row1 else []
    if SHOW_KEPT and kept_count:
        keyboard.append([{"text": f"✅ 유지 {kept_count} 보기", "callback_data": f"diffview:{MARKET}:kept"}])
    return keyboard


# ------------------------------------------------------------
# 메인
# ------------------------------------------------------------
def main():
    csv_path = find_latest_csv()
    if not csv_path:
        print(f"darren_{MARKET.lower()}_watchlist_*.csv 없음 — 변화 리포트 생략 (상태 파일 보존).")
        return

    df = pd.read_csv(csv_path, dtype=str).fillna("")
    ticker_col = find_column(df, TICKER_COL_CANDIDATES)
    if not ticker_col:
        print(f"티커 컬럼을 찾지 못했습니다 (컬럼: {list(df.columns)}). 변화 리포트 생략.")
        return
    name_col = find_column(df, NAME_COL_CANDIDATES)

    # 이번 스캔 목록 (CSV 순서 유지 = 섹터/거래대금 순, 중복 제거)
    seen = set()
    new_items = []
    for _, row in df.iterrows():
        t = normalize_ticker(row[ticker_col])
        if not t or t in seen:
            continue
        seen.add(t)
        n = clean_name(row[name_col]) if name_col else ""
        new_items.append((t, n))

    if not new_items:
        print("이번 스캔 티커가 비어 있어 변화 리포트를 생략합니다 (상태 파일 보존).")
        return

    prev_items = load_prev_state()
    today = pd.Timestamp.today().strftime("%Y-%m-%d")

    # 첫 실행: 비교 기준이 없음 → 기준선만 저장
    if prev_items is None:
        save_state(new_items)
        tg_send(
            f"🔄 스캔 변화 리포트 ({MARKET} · {today})\n"
            f"첫 실행이라 비교 기준이 없습니다.\n"
            f"이번 결과 {len(new_items)}종목을 기준으로 저장했습니다.\n"
            f"다음 스캔부터 추가/유지/제외가 표시됩니다."
        )
        return

    prev_map = {t: n for t, n in prev_items}
    new_set = {t for t, _ in new_items}
    prev_set = set(prev_map.keys())

    added = [(t, n) for t, n in new_items if t not in prev_set]
    kept = [(t, n) for t, n in new_items if t in prev_set]
    removed = [(t, prev_map[t]) for t, _ in prev_items if t not in new_set]

    save_diff_state(added, removed, kept)

    summary_text = (
        f"🔄 스캔 변화 리포트 ({MARKET} · {today})\n"
        f"직전 스캔 대비\n"
        f"➕ 추가 {len(added)} · ✅ 유지 {len(kept)} · ➖ 제외 {len(removed)}"
    )
    kb = summary_keyboard(len(added), len(removed), len(kept))
    if kb:
        tg_send(summary_text, {"inline_keyboard": kb})
    else:
        tg_send(summary_text)

    # 이번 목록을 다음 비교 기준으로 저장 (커밋은 워크플로우 스텝이 수행)
    save_state(new_items)
    print(f"변화 리포트 완료: 추가 {len(added)} / 유지 {len(kept)} / 제외 {len(removed)}")


if __name__ == "__main__":
    main()
