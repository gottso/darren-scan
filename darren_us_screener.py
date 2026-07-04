# -*- coding: utf-8 -*-
"""
데런식 주말 워치리스트 자동 스크리너 (미국장: NYSE/NASDAQ 개별주) + 텔레그램 알림
================================================================================
GitHub Actions 클라우드 실행용 (섹터별 분류 버전).

변경점(v3):
- 최종 통과 종목에 야후 파이낸스 섹터 정보를 붙입니다.
- 텔레그램 리포트를 '섹터별'로 묶고, 섹터 내에서는 거래대금 내림차순 정렬합니다.
- 섹터 순서는 섹터별 총 거래대금이 큰 순서입니다.

원본 조건식:
(exch(nyse,nasdaq) and advol(60) > 30 and advol(20) > 30
 and ! (sma(20) < sma(50))@{0..20}
 and natr(50) > 2
 and ! (price < sma(50) and sma(50) trend_dn 20)
 and ! price < (sma(50) - atr(50))
 and (price > sma(100) or price > sma(200) or bars() < 200)
 and ! (price < sma(20) and price < sma(50))@{0..15})

※ 이 스캔은 '매수 시그널'이 아니라 후보군(유니버스) 압축 필터입니다.
   베이스/수축/셋업 캔들 판정은 트뷰 바둑판으로 눈으로 하는 겁니다.
"""

import datetime as dt
import io
import os
import sys
import time

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    print("yfinance가 없습니다:  pip install yfinance pandas requests")
    sys.exit(1)

# ============================================================
# 설정
# ============================================================
ADVOL_MIN_M = 30.0        # 평균 거래대금 하한 (백만 달러)
NATR_MIN = 2.0            # NATR(50) 하한 (%)
MIN_PRICE = 3.0           # 초저가 잡주 제외 (달러)
BATCH_SIZE = 200
HISTORY_PERIOD = "2y"
SECTOR_SLEEP = 0.2        # 섹터 조회 간 지연 (rate limit 완화)

TELEGRAM_TOKEN = os.environ.get("DARREN_TG_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("DARREN_TG_CHAT", "").strip()

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


# ============================================================
# 사전 점검
# ============================================================
def preflight_check():
    if not TELEGRAM_TOKEN:
        print("❌ DARREN_TG_TOKEN Secret이 없습니다.")
        sys.exit(1)
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=15)
        if not r.json().get("ok"):
            print(f"❌ 토큰 무효: {r.json()}")
            sys.exit(1)
        print(f"✅ 봇 연결: @{r.json()['result'].get('username', '?')}")
    except Exception as e:
        print(f"❌ 텔레그램 접속 실패: {e}")
        sys.exit(1)
    if not resolve_chat_id():
        print("❌ chat_id 확보 실패. DARREN_TG_CHAT Secret을 등록하세요.")
        sys.exit(1)
    print(f"✅ chat_id 확인: {TELEGRAM_CHAT_ID}")


# ============================================================
# 텔레그램
# ============================================================
def resolve_chat_id() -> str:
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=15)
        data = r.json()
        if data.get("ok") and data.get("result"):
            for upd in reversed(data["result"]):
                msg = upd.get("message") or upd.get("edited_message")
                if msg and "chat" in msg:
                    TELEGRAM_CHAT_ID = str(msg["chat"]["id"])
                    return TELEGRAM_CHAT_ID
    except Exception as e:
        print(f"getUpdates 오류: {e}")
    return ""


def send_telegram(text: str) -> bool:
    chat_id = resolve_chat_id()
    if not chat_id:
        print("chat_id 없음 — 전송 건너뜀.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    ok = True
    # 줄 단위로 4000자 이하 청크 구성 (줄 중간이 잘리지 않게)
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    for chunk in chunks or [text]:
        try:
            r = requests.post(
                url, data={"chat_id": chat_id, "text": chunk}, timeout=15)
            if r.status_code != 200:
                print(f"전송 실패: {r.text}")
                ok = False
        except Exception as e:
            print(f"전송 오류: {e}")
            ok = False
    return ok


# ============================================================
# 유니버스 수집
# ============================================================
def get_us_universe() -> list[str]:
    tickers = set()
    r = requests.get(NASDAQ_LISTED_URL, timeout=30)
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["Test Issue"] == "N"]
    if "ETF" in df.columns:
        df = df[df["ETF"] == "N"]
    tickers.update(df["Symbol"].dropna().astype(str))

    r = requests.get(OTHER_LISTED_URL, timeout=30)
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["Test Issue"] == "N"]
    df = df[df["Exchange"].isin(["N"])]
    if "ETF" in df.columns:
        df = df[df["ETF"] == "N"]
    tickers.update(df["ACT Symbol"].dropna().astype(str))

    return sorted(
        t for t in tickers
        if t.isalpha() and 1 <= len(t) <= 5 and "File" not in t
    )


# ============================================================
# 지표
# ============================================================
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def atr_series(high, low, close, n: int) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


# ============================================================
# 데런 조건식 판정
# ============================================================
def check_darren_conditions(df: pd.DataFrame) -> tuple[bool, dict]:
    df = df.dropna(subset=["Close", "Volume"])
    bars = len(df)
    if bars < 60:
        return False, {}
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    price = float(close.iloc[-1])
    if price < MIN_PRICE:
        return False, {}

    dollar_vol = close * vol
    advol60 = float(dollar_vol.tail(60).mean()) / 1e6
    advol20 = float(dollar_vol.tail(20).mean()) / 1e6
    if not (advol60 > ADVOL_MIN_M and advol20 > ADVOL_MIN_M):
        return False, {}

    sma20, sma50 = sma(close, 20), sma(close, 50)
    sma100, sma200 = sma(close, 100), sma(close, 200)
    atr50 = atr_series(high, low, close, 50)

    if bars >= 50 and (sma20 < sma50).tail(21).any():
        return False, {}
    natr50 = float(atr50.iloc[-1] / price * 100.0)
    if not (natr50 > NATR_MIN):
        return False, {}
    if bars >= 70:
        if price < float(sma50.iloc[-1]) and float(sma50.iloc[-1]) < float(sma50.iloc[-21]):
            return False, {}
    if bars >= 50 and price < (float(sma50.iloc[-1]) - float(atr50.iloc[-1])):
        return False, {}
    if bars >= 200:
        if not (price > float(sma100.iloc[-1]) or price > float(sma200.iloc[-1])):
            return False, {}
    if bars >= 50 and ((close < sma20) & (close < sma50)).tail(16).any():
        return False, {}

    return True, {
        "close": round(price, 2),
        "advol60_M$": round(advol60, 1),
        "natr50_%": round(natr50, 2),
        "gap_20sma_%": round((price / float(sma20.iloc[-1]) - 1) * 100, 1)
        if bars >= 20 else None,
        "bars": bars,
        "ipo": bars < 200,
    }


# ============================================================
# 섹터 정보 부착
# ============================================================
def add_sector_info(result: pd.DataFrame, log) -> pd.DataFrame:
    """통과 종목 각각에 야후 파이낸스 섹터를 붙인다."""
    log(f"섹터 정보 조회 중... ({len(result)}종목)")
    sectors = []
    for t in result["ticker"]:
        sec = "미분류"
        try:
            info = yf.Ticker(t).info
            sec = info.get("sector") or "미분류"
        except Exception:
            sec = "미분류"
        sectors.append(sec)
        time.sleep(SECTOR_SLEEP)
    result = result.copy()
    result["sector"] = sectors
    return result


# ============================================================
# 스캔 본체
# ============================================================
def run_us_scan(verbose: bool = True) -> tuple[pd.DataFrame, str]:
    def log(msg):
        if verbose:
            print(msg, flush=True)

    universe = get_us_universe()
    log(f"미국장 유니버스: {len(universe)}종목 (NYSE+NASDAQ, ETF 제외)")

    log("1차 유동성 프리필터 진행 중...")
    survivors = []
    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i:i + BATCH_SIZE]
        try:
            data = yf.download(batch, period="5d", interval="1d",
                               group_by="ticker", progress=False,
                               threads=True, auto_adjust=True)
        except Exception:
            continue
        for t in batch:
            try:
                d = data[t] if len(batch) > 1 else data
                dv = (d["Close"] * d["Volume"]).mean()
                if pd.notna(dv) and dv / 1e6 > ADVOL_MIN_M / 3:
                    survivors.append(t)
            except (KeyError, TypeError):
                continue
        log(f"  ... {min(i + BATCH_SIZE, len(universe))}/{len(universe)} "
            f"(생존 {len(survivors)})")
        time.sleep(0.5)

    log(f"프리필터 통과: {len(survivors)}종목 → 정밀 스캔 시작")

    passed = []
    for i in range(0, len(survivors), BATCH_SIZE):
        batch = survivors[i:i + BATCH_SIZE]
        try:
            data = yf.download(batch, period=HISTORY_PERIOD, interval="1d",
                               group_by="ticker", progress=False,
                               threads=True, auto_adjust=True)
        except Exception:
            continue
        for t in batch:
            try:
                d = data[t] if len(batch) > 1 else data
                ok, detail = check_darren_conditions(d)
                if ok:
                    passed.append({"ticker": t, **detail})
            except (KeyError, TypeError):
                continue
        time.sleep(0.5)

    result = pd.DataFrame(passed)
    if result.empty:
        log("\n최종 통과: 0종목")
        return result, ""

    # 섹터 부착 + 정렬
    result = add_sector_info(result, log)
    # 섹터 순서: 섹터별 총 거래대금 큰 순
    order = (result.groupby("sector")["advol60_M$"].sum()
             .sort_values(ascending=False).index.tolist())
    result["__sec_order"] = result["sector"].map({s: i for i, s in enumerate(order)})
    result = result.sort_values(
        ["__sec_order", "advol60_M$"], ascending=[True, False]
    ).drop(columns="__sec_order")

    tv_string = ",".join(result["ticker"])
    log(f"\n최종 통과: {len(result)}종목")
    return result, tv_string


# ============================================================
# 리포트 (섹터별)
# ============================================================
def format_report(result: pd.DataFrame, tv_string: str) -> str:
    today = dt.date.today().strftime("%Y-%m-%d")
    lines = [f"📋 데런식 US 주말 스캔 ({today})"]
    if result.empty:
        lines.append("\n통과 종목 없음. 필터 통과 수 자체가 시장 온도계입니다.")
        return "\n".join(lines)

    lines.append(f"통과 {len(result)}종목 · 섹터별 / 섹터 내 거래대금순\n")
    for sector, grp in result.groupby("sector", sort=False):
        lines.append(f"━━ {sector} ({len(grp)}) ━━")
        for _, row in grp.iterrows():
            ipo = " [IPO]" if row["ipo"] else ""
            lines.append(
                f"• {row['ticker']}{ipo}  ${row['close']}  "
                f"${row['advol60_M$']}M  NATR {row['natr50_%']}%  "
                f"20선 {row['gap_20sma_%']}%")
        lines.append("")

    lines.append("[트뷰 워치리스트 붙여넣기용]")
    lines.append(tv_string)
    lines.append("\n※ 후보군 압축일 뿐, 매수 시그널 아님. "
                 "베이스/수축/셋업 캔들은 바둑판으로 직접 확인.")
    return "\n".join(lines)


# ============================================================
# 메인
# ============================================================
def main():
    preflight_check()
    send_telegram("🔍 데런식 US 스캔 시작 (GitHub Actions).")
    try:
        result, tv_string = run_us_scan(verbose=True)
    except Exception as e:
        err = f"⚠️ US 스캔 실패: {e}"
        print(err)
        send_telegram(err)
        sys.exit(1)

    today = dt.date.today().strftime("%Y%m%d")
    if not result.empty:
        result.to_csv(f"darren_us_watchlist_{today}.csv",
                      index=False, encoding="utf-8-sig")

    report = format_report(result, tv_string)
    if send_telegram(report):
        print("텔레그램 전송 완료")
    else:
        print("텔레그램 전송 실패:\n" + report)
        sys.exit(1)


if __name__ == "__main__":
    main()
