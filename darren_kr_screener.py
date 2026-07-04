# -*- coding: utf-8 -*-
"""
데런식 주말 워치리스트 자동 스크리너 (한국장: KOSPI/KOSDAQ 개별주) + 텔레그램 알림
================================================================================
GitHub Actions 클라우드 실행용 (야후 파이낸스 버전).

[중요] KRX 직접 접속(pykrx)은 GitHub 미국 서버에서 차단됩니다.
       그래서 이 버전은 다음 구조로 동작합니다:
       - 종목 목록: 저장소에 올려둔 kr_tickers.csv 에서 읽음
                    (make_kr_tickers.py로 본인 PC에서 1회 생성)
       - 가격 데이터: 야후 파이낸스(005930.KS / 035720.KQ 형식)로 조회
                    → 미국 서버에서 문제없이 접속됨

원본 조건식 (exch(krx)):
(exch(krx) and advol(60) > 30 and advol(20) > 30
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
# 설정 (데런 원문 기준값 — 본인 스타일에 맞게 조절)
# ============================================================
ADVOL_MIN_EOK = 30       # 평균 거래대금 하한 (억 원). 후보 많으면 40/50/60 상향
NATR_MIN = 2.0           # NATR(50) 하한 (%). 더 빠른 종목만 원하면 3.0
MIN_PRICE_KRW = 1000     # 초저가주 제외 (원)
BATCH_SIZE = 200         # 야후 배치 다운로드 크기
HISTORY_PERIOD = "2y"    # 정밀 스캔용 데이터 기간 (200봉 이상 확보)
TICKER_CSV = "kr_tickers.csv"   # 저장소에 올려둔 종목 목록 파일

# 텔레그램 — GitHub Secrets에서 주입됨 (코드에 토큰을 넣지 마세요)
TELEGRAM_TOKEN = os.environ.get("DARREN_TG_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("DARREN_TG_CHAT", "").strip()  # 비우면 자동 탐지


# ============================================================
# 사전 점검
# ============================================================
def preflight_check():
    """토큰/chat_id + 티커 CSV 존재를 스캔 전에 검증. 실패 시 즉시 종료."""
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

    if not os.path.exists(TICKER_CSV):
        print(f"❌ {TICKER_CSV} 파일이 없습니다.")
        send_telegram(f"⚠️ KR 스캔 중단: {TICKER_CSV} 파일이 저장소에 없습니다. "
                      f"make_kr_tickers.py로 생성 후 업로드하세요.")
        sys.exit(1)


# ============================================================
# 텔레그램
# ============================================================
def resolve_chat_id() -> str:
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            timeout=15)
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
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
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
# 지표 계산
# ============================================================
def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def atr_series(high, low, close, n: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


# ============================================================
# 데런 조건식 판정 (종목 1개)
# ============================================================
def check_darren_conditions(df: pd.DataFrame) -> tuple[bool, dict]:
    """df: 야후 OHLCV (Open/High/Low/Close/Volume). 가격 단위 = 원"""
    df = df.dropna(subset=["Close", "Volume"])
    bars = len(df)
    if bars < 60:
        return False, {}

    close = df["Close"]
    high, low, vol = df["High"], df["Low"], df["Volume"]
    price = float(close.iloc[-1])

    if price < MIN_PRICE_KRW:
        return False, {}

    # --- 1) advol: 평균 거래대금 (억 원) ---
    dollar_vol = close * vol
    advol60 = float(dollar_vol.tail(60).mean()) / 1e8
    advol20 = float(dollar_vol.tail(20).mean()) / 1e8
    if not (advol60 > ADVOL_MIN_EOK and advol20 > ADVOL_MIN_EOK):
        return False, {}

    sma20 = sma(close, 20)
    sma50 = sma(close, 50)
    sma100 = sma(close, 100)
    sma200 = sma(close, 200)
    atr50 = atr_series(high, low, close, 50)

    # --- 2) ! (sma20 < sma50)@{0..20} ---
    if bars >= 50:
        if (sma20 < sma50).tail(21).any():
            return False, {}

    # --- 3) natr(50) > 2 ---
    natr50 = float(atr50.iloc[-1] / price * 100.0)
    if not (natr50 > NATR_MIN):
        return False, {}

    # --- 4) ! (price < sma50 and sma50 trend_dn 20) ---
    if bars >= 70:
        s_now = float(sma50.iloc[-1])
        s_ago = float(sma50.iloc[-21])
        if price < s_now and s_now < s_ago:
            return False, {}

    # --- 5) ! price < (sma50 - atr50) ---
    if bars >= 50:
        if price < (float(sma50.iloc[-1]) - float(atr50.iloc[-1])):
            return False, {}

    # --- 6) (price > sma100 or price > sma200 or bars < 200) ---
    if bars < 200:
        pass
    else:
        if not (price > float(sma100.iloc[-1]) or price > float(sma200.iloc[-1])):
            return False, {}

    # --- 7) ! (price < sma20 and price < sma50)@{0..15} ---
    if bars >= 50:
        both_below = ((close < sma20) & (close < sma50)).tail(16)
        if both_below.any():
            return False, {}

    detail = {
        "종가": int(price),
        "advol60_억": round(advol60, 1),
        "natr50_%": round(natr50, 2),
        "gap20선_%": round((price / float(sma20.iloc[-1]) - 1) * 100, 1)
        if bars >= 20 else None,
        "봉수": bars,
        "ipo": bars < 200,
    }
    return True, detail


# ============================================================
# 스캔 본체
# ============================================================
def run_kr_scan(verbose: bool = True) -> tuple[pd.DataFrame, str]:
    """
    한국장 개별주 전체 스캔 (야후 파이낸스).
    반환: (결과 DataFrame, TradingView 붙여넣기용 콤마 티커 문자열)
    """
    def log(msg):
        if verbose:
            print(msg, flush=True)

    # --- 종목 목록 로드 ---
    tk = pd.read_csv(TICKER_CSV, dtype={"code": str})
    yf_tickers = tk["yf_ticker"].tolist()
    meta = {row["code"]: (row["name"], row["market"])
            for _, row in tk.iterrows()}
    log(f"한국장 유니버스: {len(yf_tickers)}종목 (CSV 로드)")

    # --- 1차 프리필터: 최근 5일 데이터로 거래대금 압축 ---
    log("1차 유동성 프리필터 진행 중...")
    survivors = []
    for i in range(0, len(yf_tickers), BATCH_SIZE):
        batch = yf_tickers[i:i + BATCH_SIZE]
        try:
            data = yf.download(
                batch, period="5d", interval="1d",
                group_by="ticker", progress=False, threads=True,
                auto_adjust=True,
            )
        except Exception:
            continue
        for t in batch:
            try:
                d = data[t] if len(batch) > 1 else data
                dv = (d["Close"] * d["Volume"]).mean()
                if pd.notna(dv) and dv / 1e8 > ADVOL_MIN_EOK / 3:
                    survivors.append(t)
            except (KeyError, TypeError):
                continue
        log(f"  ... {min(i + BATCH_SIZE, len(yf_tickers))}/{len(yf_tickers)} "
            f"(현재 생존 {len(survivors)})")
        time.sleep(0.5)

    log(f"프리필터 통과: {len(survivors)}종목 → 정밀 스캔 시작")

    # --- 2차 정밀 스캔 ---
    passed = []
    for i in range(0, len(survivors), BATCH_SIZE):
        batch = survivors[i:i + BATCH_SIZE]
        try:
            data = yf.download(
                batch, period=HISTORY_PERIOD, interval="1d",
                group_by="ticker", progress=False, threads=True,
                auto_adjust=True,
            )
        except Exception:
            continue
        for t in batch:
            try:
                d = data[t] if len(batch) > 1 else data
                ok, detail = check_darren_conditions(d)
                if ok:
                    code = t.split(".")[0]
                    name, market = meta.get(code, (code, ""))
                    passed.append({
                        "티커": code, "종목명": name, "시장": market, **detail})
                    log(f"  ✔ {name}({code})  advol60={detail['advol60_억']}억  "
                        f"NATR={detail['natr50_%']}%")
            except (KeyError, TypeError):
                continue
        time.sleep(0.5)

    result = pd.DataFrame(passed)
    if not result.empty:
        result = result.sort_values("advol60_억", ascending=False)
        tv_string = ",".join(f"KRX:{r['티커']}" for _, r in result.iterrows())
    else:
        tv_string = ""

    log(f"\n최종 통과: {len(result)}종목")
    return result, tv_string


# ============================================================
# 리포트 포맷
# ============================================================
def format_report(result: pd.DataFrame, tv_string: str) -> str:
    today = dt.date.today().strftime("%Y-%m-%d")
    lines = [f"📋 데런식 KR 주말 스캔 ({today})"]

    if result.empty:
        lines.append("\n통과 종목 없음.")
        lines.append("필터 통과 수 자체가 시장 온도계입니다 — "
                     "지금은 시장이 차갑다는 신호일 수 있습니다.")
        return "\n".join(lines)

    lines.append(f"통과: {len(result)}종목 (거래대금순)\n")
    for _, row in result.head(40).iterrows():
        ipo_tag = " [IPO]" if row["ipo"] else ""
        lines.append(
            f"• {row['종목명']}({row['티커']}){ipo_tag}  {row['종가']:,}원  "
            f"거래대금 {row['advol60_억']}억  NATR {row['natr50_%']}%  "
            f"20선괴리 {row['gap20선_%']}%"
        )
    if len(result) > 40:
        lines.append(f"... 외 {len(result) - 40}종목 (CSV 참조)")

    lines.append("\n[트뷰 워치리스트 붙여넣기용]")
    lines.append(tv_string)
    lines.append("\n※ 후보군 압축일 뿐, 매수 시그널 아님. "
                 "베이스/수축/셋업 캔들은 바둑판으로 직접 확인.")
    return "\n".join(lines)


# ============================================================
# 메인
# ============================================================
def main():
    preflight_check()
    send_telegram("🔍 데런식 KR 스캔 시작 (GitHub Actions). "
                  "완료까지 다소 시간이 걸립니다.")

    try:
        result, tv_string = run_kr_scan(verbose=True)
    except Exception as e:
        err_msg = f"⚠️ KR 스캔 실패: {e}"
        print(err_msg)
        send_telegram(err_msg)
        sys.exit(1)

    today = dt.date.today().strftime("%Y%m%d")
    if not result.empty:
        result.to_csv(f"darren_kr_watchlist_{today}.csv",
                      index=False, encoding="utf-8-sig")

    report = format_report(result, tv_string)
    sent = send_telegram(report)
    if sent:
        print("텔레그램 전송 완료")
    else:
        print("텔레그램 전송 실패 — 콘솔 출력:")
        print(report)
        sys.exit(1)


if __name__ == "__main__":
    main()
