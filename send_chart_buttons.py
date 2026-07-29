# -*- coding: utf-8 -*-
"""
스캔 결과 → 섹터 드릴다운 방식 "차트 보기" 메뉴 전송
================================================================================
darren_us_screener.py / darren_kr_screener.py 실행 직후, 같은 워크플로우의
다음 스텝으로 실행된다. 스크리너가 저장한 darren_{market}_watchlist_*.csv를
읽어서 섹터별로 묶고, 텔레그램 메시지는 딱 1개만 보낸다.

기존(구버전)에는 섹터마다 메시지를 따로 보내서 US처럼 종목이 많을 때
채팅창이 버튼 메시지로 도배됐다. 이번 버전은 "섹터 목록" 버튼만 담긴
메시지 1개를 보내고, 사용자가 섹터를 탭하면 Cloudflare Worker가 같은
메시지를 그 섹터의 종목 버튼으로 편집(수정)한다 — 새 메시지가 쌓이지 않음.

Worker가 드릴다운 시점에 "섹터→티커" 매핑을 알아야 하므로, 텔레그램 전송과
별개로 state/last_{market}_buttons.json 에 저장해 둔다. 커밋은 워크플로우의
기존 "비교용 상태 파일 커밋" 스텝(`git add state/`)이 그대로 처리한다 —
yml을 추가로 손댈 필요 없음.

기존 스크리너 스크립트(darren_us_screener.py / darren_kr_screener.py)는
전혀 건드리지 않는다.

입력(환경변수):
  MARKET          : "US" 또는 "KR"
  DARREN_TG_TOKEN : 텔레그램 봇 토큰
  DARREN_TG_CHAT  : 텔레그램 chat_id
  TOP_N           : 버튼을 붙일 최대 종목 수 (기본 40, 거래대금 상위순 컷)
"""
import os
import glob
import json

import pandas as pd
import requests

MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")
TOP_N = int(os.environ.get("TOP_N", "40"))

STATE_DIR = "state"
BUTTONS_STATE_FILE = os.path.join(STATE_DIR, f"last_{MARKET.lower()}_buttons.json")

# CSV 컬럼명이 스크리너 버전에 따라 다를 수 있어 후보를 여러 개 두고 자동 탐지한다.
# (실제 확인된 컬럼: '티커','종목명','시장','sector','종가','advol60_억','natr50_%','gap20선_%','봉수','ipo')
TICKER_COL_CANDIDATES = ["티커", "ticker", "Ticker", "종목", "종목코드", "symbol", "Symbol"]
SECTOR_COL_CANDIDATES = ["sector", "Sector", "섹터", "업종"]
DOLLARVOL_COL_CANDIDATES = ["dollar_vol", "DollarVol", "advol", "거래대금", "dollar_volume", "AdVol"]
MARKET_TYPE_COL_CANDIDATES = ["시장", "market", "Market", "구분"]  # KR: 코스피/코스닥 구분용


def find_column(df, candidates, contains=None):
    for c in candidates:
        if c in df.columns:
            return c
    # 정확히 일치하는 컬럼이 없으면 부분 문자열로도 한 번 더 탐색
    # (예: 'advol60_억' 처럼 접미사가 붙은 실제 컬럼명 대응)
    seeds = contains if contains else candidates
    for col in df.columns:
        for seed in seeds:
            if seed.lower() in str(col).lower():
                return col
    return None


def resolve_kr_suffix(market_value):
    """'시장' 컬럼 값으로 코스피/코스닥을 판별해 야후 파이낸스 접미사를 정한다."""
    v = str(market_value).strip().upper()
    if "코스닥" in v or "KOSDAQ" in v or v == "KQ":
        return ".KQ"
    if "코스피" in v or "KOSPI" in v or v == "KS":
        return ".KS"
    return None


def find_latest_csv():
    pattern = f"darren_{MARKET.lower()}_watchlist_*.csv"
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def tg_send(text, reply_markup=None):
    if not TG_TOKEN or not TG_CHAT:
        print("[전송 생략 - DARREN_TG_TOKEN/DARREN_TG_CHAT 없음]\n" + text)
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
    """CSV에서 숫자로 읽혀 앞자리 0이 잘린 KR 종목코드를 6자리로 복원한다."""
    t = str(raw).strip().upper()
    if t.endswith(".0"):  # pandas가 float으로 읽은 경우 (예: 5930.0)
        t = t[:-2]
    if MARKET == "KR" and t.replace(".KS", "").replace(".KQ", "").isdigit():
        core = t.replace(".KS", "").replace(".KQ", "")
        suffix = ".KS" if t.endswith(".KS") else (".KQ" if t.endswith(".KQ") else "")
        t = core.zfill(6) + suffix
    return t


def save_buttons_state(sectors, total):
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {
        "market": MARKET,
        "date": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "total": total,
        "sectors": sectors,  # [{"name": "...", "tickers": ["005930.KS", ...]}, ...]
    }
    with open(BUTTONS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def sector_menu_keyboard(sectors):
    """섹터 목록 버튼 (2개씩 한 줄). 실제 종목 버튼은 Worker가 탭 시점에 만든다."""
    keyboard = []
    row = []
    for idx, s in enumerate(sectors):
        row.append({
            "text": f"{s['name']} ({len(s['tickers'])})",
            "callback_data": f"sec:{MARKET}:{idx}",
        })
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return keyboard


def main():
    csv_path = find_latest_csv()
    if not csv_path:
        print(f"darren_{MARKET.lower()}_watchlist_*.csv 파일을 찾지 못했습니다. 버튼 전송을 건너뜁니다.")
        return

    df = pd.read_csv(csv_path)
    if df.empty:
        print("스캔 결과가 비어 있어 버튼 전송을 건너뜁니다.")
        return

    ticker_col = find_column(df, TICKER_COL_CANDIDATES)
    sector_col = find_column(df, SECTOR_COL_CANDIDATES)
    dv_col = find_column(df, DOLLARVOL_COL_CANDIDATES, contains=["advol", "dollar", "거래대금"])
    market_type_col = find_column(df, MARKET_TYPE_COL_CANDIDATES)

    if not ticker_col:
        print(f"티커 컬럼을 찾지 못했습니다 (컬럼 목록: {list(df.columns)}). 버튼 전송을 건너뜁니다.")
        return

    # 앞자리 0 복원 + (가능하면) 코스피/코스닥 접미사를 티커 자체에 붙여 넣는다.
    # → chart_generator.py가 KS/KQ를 추측할 필요 없이 바로 정확한 심볼로 요청됨.
    df[ticker_col] = df[ticker_col].astype(str).apply(normalize_ticker)
    if MARKET == "KR" and market_type_col:
        def append_suffix(row):
            t = row[ticker_col]
            if t.endswith(".KS") or t.endswith(".KQ"):
                return t
            suf = resolve_kr_suffix(row[market_type_col])
            return f"{t}{suf}" if suf else t
        df[ticker_col] = df.apply(append_suffix, axis=1)

    if dv_col:
        df[dv_col] = pd.to_numeric(df[dv_col], errors="coerce")
        df = df.sort_values(dv_col, ascending=False)
    df = df.head(TOP_N).copy()

    if sector_col:
        df[sector_col] = df[sector_col].fillna("미분류")
        sectors = [
            {"name": str(name), "tickers": g[ticker_col].astype(str).tolist()}
            for name, g in df.groupby(sector_col, sort=False)
        ]
    else:
        sectors = [{"name": "전체", "tickers": df[ticker_col].astype(str).tolist()}]

    save_buttons_state(sectors, len(df))

    text = (
        f"📊 종목별 차트 보기 ({MARKET}, 상위 {len(df)}종목 · {len(sectors)}개 섹터)\n"
        f"섹터를 선택하면 종목 버튼이 나옵니다."
    )
    tg_send(text, {"inline_keyboard": sector_menu_keyboard(sectors)})


if __name__ == "__main__":
    main()
