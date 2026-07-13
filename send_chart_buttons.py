# -*- coding: utf-8 -*-
"""
스캔 결과 → 종목별 "차트 보기" 버튼 메시지 전송
================================================================================
darren_us_screener.py / darren_kr_screener.py 실행 직후, 같은 워크플로우의
다음 스텝으로 실행된다. 스크리너가 저장한 darren_{market}_watchlist_*.csv를
읽어서 섹터별로 묶은 뒤, 종목마다 "📊 TICKER" 인라인 버튼 메시지를 텔레그램으로
보낸다. 버튼을 누르면 Cloudflare Worker → GitHub Actions(darren_chart.yml)가
실행되어 해당 종목의 일봉 차트가 도착한다.

기존 스크리너 스크립트(darren_us_screener.py / darren_kr_screener.py)는
전혀 건드리지 않는다 — 워크플로우 yml에 이 스크립트를 한 스텝 추가하는
방식으로 통합한다.

입력(환경변수):
  MARKET          : "US" 또는 "KR"
  DARREN_TG_TOKEN : 텔레그램 봇 토큰
  DARREN_TG_CHAT  : 텔레그램 chat_id
  TOP_N           : 버튼을 붙일 최대 종목 수 (기본 40, 거래대금 상위순 컷)
"""
import os
import glob
import time

import pandas as pd
import requests

MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")
TOP_N = int(os.environ.get("TOP_N", "40"))

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


def chunk_buttons(tickers, per_row=4):
    keyboard = []
    row = []
    for t in tickers:
        row.append({"text": f"📊 {t}", "callback_data": f"chart:{MARKET}:{t}"[:64]})
        if len(row) == per_row:
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

    tg_send(f"📊 종목별 차트 보기 (상위 {len(df)}종목, {MARKET})\n버튼을 누르면 해당 종목 일봉 차트가 도착합니다.")

    if sector_col:
        df[sector_col] = df[sector_col].fillna("미분류")
        for sector_name, gdf in df.groupby(sector_col, sort=False):
            tickers = gdf[ticker_col].astype(str).tolist()
            keyboard = chunk_buttons(tickers)
            tg_send(f"━━ {sector_name} ({len(tickers)}) ━━", {"inline_keyboard": keyboard})
            time.sleep(0.3)  # 텔레그램 레이트리밋 방지
    else:
        tickers = df[ticker_col].astype(str).tolist()
        keyboard = chunk_buttons(tickers)
        tg_send("━━ 전체 종목 ━━", {"inline_keyboard": keyboard})


if __name__ == "__main__":
    main()
