# -*- coding: utf-8 -*-
"""
데런식 개별 종목 차트 생성기 (일봉) — 텔레그램 전송
================================================================================
GitHub Actions 워크플로우(darren_chart.yml)에서 workflow_dispatch로 호출됨.
텔레그램 인라인 버튼("📊 TICKER")을 누르면 Cloudflare Worker가 이 워크플로우를
실행시키고, 이 스크립트가 해당 종목의 일봉 차트(SMA20/50 + 거래량 + NATR)를
이미지로 만들어 텔레그램으로 전송한다.

입력(환경변수):
  TICKER          : 종목 티커. US는 그대로(NVDA), KR은 6자리 코드(005930) 또는
                     이미 .KS/.KQ가 붙은 형태 모두 허용 (자동으로 KS→KQ 순서 시도).
  MARKET          : "US" 또는 "KR"
  DARREN_TG_TOKEN : 텔레그램 봇 토큰
  DARREN_TG_CHAT  : 텔레그램 chat_id

※ 이 차트는 참고용입니다. 셋업 캔들/베이스 품질/리스크라인 판정은
   데런 프레임워크상 사람이 직접 눈으로 확인해야 하는 영역입니다.
"""
import os
import sys
import io
import datetime as dt

import pandas as pd
import yfinance as yf
import mplfinance as mpf
import requests


TICKER_RAW = os.environ.get("TICKER", "").strip().upper()
MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")


def send_telegram_text(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"텔레그램 텍스트 전송 실패: {e}")


def send_telegram_photo(png_bytes, caption):
    if not TG_TOKEN or not TG_CHAT:
        print("DARREN_TG_TOKEN/DARREN_TG_CHAT 없음 - 전송 생략(로컬 테스트 모드)")
        return True
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT, "caption": caption},
            files={"photo": ("chart.png", png_bytes, "image/png")},
            timeout=30,
        )
        return res.status_code == 200
    except Exception as e:
        print(f"텔레그램 사진 전송 실패: {e}")
        return False


def resolve_kr_symbol(code):
    """KR 종목코드에 .KS(코스피)/.KQ(코스닥) 접미사를 순서대로 시도해 유효한 쪽을 찾는다."""
    code = code.replace(".KS", "").replace(".KQ", "")
    for suffix in (".KS", ".KQ"):
        sym = f"{code}{suffix}"
        try:
            df = yf.Ticker(sym).history(period="5d")
            if not df.empty:
                return sym
        except Exception:
            continue
    return None


def resolve_symbol():
    if MARKET == "KR":
        if TICKER_RAW.endswith(".KS") or TICKER_RAW.endswith(".KQ"):
            return TICKER_RAW
        return resolve_kr_symbol(TICKER_RAW)
    return TICKER_RAW


def calc_natr(df, period=50):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return (atr / close) * 100


def main():
    if not TICKER_RAW:
        send_telegram_text("⚠️ 차트 생성 실패: 티커가 비어 있습니다.")
        sys.exit(1)

    symbol = resolve_symbol()
    if not symbol:
        send_telegram_text(f"⚠️ 차트 생성 실패: '{TICKER_RAW}' 심볼을 찾을 수 없습니다 (KS/KQ 모두 실패).")
        sys.exit(1)

    try:
        df = yf.download(symbol, period="1y", interval="1d", progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    except Exception as e:
        send_telegram_text(f"⚠️ {symbol} 데이터 조회 실패: {e}")
        sys.exit(1)

    if df.empty or len(df) < 60:
        send_telegram_text(f"⚠️ {symbol} 데이터가 부족합니다 (최소 60봉 필요).")
        sys.exit(1)

    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["NATR50"] = calc_natr(df, 50)

    plot_df = df.tail(140)  # 최근 약 140거래일만 표시 (베이스/셋업 구간 확인용)

    last = df.iloc[-1]
    price = last["Close"]
    sma20 = last["SMA20"]
    sma50 = last["SMA50"]
    natr = last["NATR50"]
    sma20_gap = (price / sma20 - 1) * 100 if pd.notna(sma20) else float("nan")

    addplots = [
        mpf.make_addplot(plot_df["SMA20"], color="#2196F3", width=1.2),
        mpf.make_addplot(plot_df["SMA50"], color="#FF9800", width=1.2),
    ]

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350",
                                edge="inherit", wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="yahoo", marketcolors=mc,
                                gridstyle=":", gridcolor="#e0e0e0")

    buf = io.BytesIO()
    title = f"{symbol}  ·  일봉  ·  {dt.date.today().strftime('%Y-%m-%d')}"

    mpf.plot(
        plot_df,
        type="candle",
        style=style,
        addplot=addplots,
        volume=True,
        title=title,
        figsize=(11, 7),
        panel_ratios=(3, 1),
        tight_layout=True,
        savefig=dict(fname=buf, dpi=150, bbox_inches="tight"),
    )
    buf.seek(0)

    caption = (
        f"📊 {symbol}\n"
        f"현재가: {price:,.2f}\n"
        f"20SMA: {sma20:,.2f} ({sma20_gap:+.1f}%)\n"
        f"50SMA: {sma50:,.2f}\n"
        f"NATR(50): {natr:.2f}%\n"
        f"※ 셋업 캔들/베이스 품질은 직접 눈으로 확인하세요."
    )

    ok = send_telegram_photo(buf.getvalue(), caption)
    if not ok:
        send_telegram_text(f"⚠️ {symbol} 차트 이미지 전송 실패.")
        sys.exit(1)

    print(f"{symbol} 차트 전송 완료")


if __name__ == "__main__":
    main()
