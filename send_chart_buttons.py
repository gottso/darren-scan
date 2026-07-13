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
TICKER_COL_CANDIDATES = ["ticker", "Ticker", "종목", "종목코드", "symbol", "Symbol"]
SECTOR_COL_CANDIDATES = ["sector", "Sector", "섹터", "업종"]
DOLLARVOL_COL_CANDIDATES = ["dollar_vol", "DollarVol", "advol", "거래대금", "dollar_volume", "AdVol"]


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
    dv_col = find_column(df, DOLLARVOL_COL_CANDIDATES)

    if not ticker_col:
        print(f"티커 컬럼을 찾지 못했습니다 (컬럼 목록: {list(df.columns)}). 버튼 전송을 건너뜁니다.")
        return

    if dv_col:
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
