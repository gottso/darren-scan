# -*- coding: utf-8 -*-
"""
데런식 주말 워치리스트 자동 스크리너 (한국장: KOSPI/KOSDAQ 개별주) + 텔레그램 알림
================================================================================
GitHub Actions 클라우드 실행용.
- 토큰/chat_id는 환경변수(GitHub Secrets)에서만 읽습니다.
- KRX 데이터는 pykrx로 가져옵니다. 해외(GitHub) 서버에서 접속이
  불안정할 수 있으므로 재시도 로직과 실패 시 텔레그램 알림을 넣었습니다.

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
    from pykrx import stock
except ImportError:
    print("pykrx가 없습니다:  pip install pykrx pandas requests")
    sys.exit(1)

# ============================================================
# 설정 (데런 원문 기준값 — 본인 스타일에 맞게 조절)
# ============================================================
ADVOL_MIN_EOK = 30       # 평균 거래대금 하한 (억 원). 후보 많으면 40/50/60 상향
NATR_MIN = 2.0           # NATR(50) 하한 (%). 더 빠른 종목만 원하면 3.0
LOOKBACK_DAYS = 640      # 데이터 조회 기간 (거래일 200봉 이상 확보용)
MARKETS = ["KOSPI", "KOSDAQ"]
EXCLUDE_KEYWORDS = ["스팩", "ETN", "리츠"]  # 종목명 기준 제외
FETCH_RETRY = 3          # 개별 종목 조회 실패 시 재시도 횟수

# 텔레그램 — GitHub Secrets에서 주입됨 (코드에 토큰을 넣지 마세요)
TELEGRAM_TOKEN = os.environ.get("DARREN_TG_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("DARREN_TG_CHAT", "").strip()  # 비우면 자동 탐지


# ============================================================
# 사전 점검: Secret 없으면 스캔 전에 즉시 실패
# ============================================================
def preflight_check():
    """토큰 유효성 + chat_id 확보를 스캔 전에 검증. 실패 시 즉시 종료."""
    if not TELEGRAM_TOKEN:
        print("=" * 60)
        print("❌ DARREN_TG_TOKEN Secret이 설정되지 않았습니다.")
        print("   저장소 → Settings → Secrets and variables → Actions")
        print("=" * 60)
        sys.exit(1)

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
            timeout=15,
        )
        data = r.json()
        if not data.get("ok"):
            print(f"❌ 토큰이 유효하지 않습니다: {data}")
            sys.exit(1)
        print(f"✅ 봇 연결 확인: @{data['result'].get('username', '?')}")
    except Exception as e:
        print(f"❌ 텔레그램 API 접속 실패: {e}")
        sys.exit(1)

    chat_id = resolve_chat_id()
    if not chat_id:
        print("=" * 60)
        print("❌ chat_id를 확보하지 못했습니다.")
        print("   DARREN_TG_CHAT Secret에 chat_id 숫자를 등록하세요.")
        print("=" * 60)
        sys.exit(1)
    print(f"✅ chat_id 확인: {chat_id}")


# ============================================================
# 텔레그램
# ============================================================
def resolve_chat_id() -> str:
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("ok") and data.get("result"):
            for upd in reversed(data["result"]):
                msg = upd.get("message") or upd.get("edited_message")
                if msg and "chat" in msg:
                    TELEGRAM_CHAT_ID = str(msg["chat"]["id"])
                    print(f"chat_id 자동 탐지: {TELEGRAM_CHAT_ID}")
                    return TELEGRAM_CHAT_ID
    except Exception as e:
        print(f"getUpdates 오류: {e}")
    return ""


def send_telegram(text: str) -> bool:
    """텔레그램 전송. 4096자 제한 자동 분할."""
    chat_id = resolve_chat_id()
    if not chat_id:
        print("chat_id가 없어 전송을 건너뜁니다.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    ok = True
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        try:
            r = requests.post(
                url, data={"chat_id": chat_id, "text": chunk}, timeout=15,
            )
            if r.status_code != 200:
                print(f"텔레그램 전송 실패: {r.text}")
                ok = False
        except Exception as e:
            print(f"텔레그램 전송 오류: {e}")
            ok = False
    return ok


# ============================================================
# KRX 데이터 조회 (재시도 포함)
# ============================================================
def safe_get_ohlcv(fromdate, todate, ticker, retry=FETCH_RETRY):
    """pykrx OHLCV 조회 — 해외 서버 불안정 대비 재시도"""
    for attempt in range(retry):
        try:
            df = stock.get_market_ohlcv(fromdate, todate, ticker)
            if df is not None and not df.empty:
                return df
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


# ============================================================
# 지표 계산
# ============================================================
def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def atr_series(df: pd.DataFrame, n: int) -> pd.Series:
    high, low, close = df["고가"], df["저가"], df["종가"]
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
    """df: pykrx OHLCV (컬럼: 시가/고가/저가/종가/거래량/거래대금)"""
    bars = len(df)
    if bars < 60:
        return False, {}

    close = df["종가"]
    price = float(close.iloc[-1])

    # --- 1) advol: 평균 거래대금 (억 원) ---
    advol60 = float(df["거래대금"].tail(60).mean()) / 1e8
    advol20 = float(df["거래대금"].tail(20).mean()) / 1e8
    if not (advol60 > ADVOL_MIN_EOK and advol20 > ADVOL_MIN_EOK):
        return False, {}

    sma20 = sma(close, 20)
    sma50 = sma(close, 50)
    sma100 = sma(close, 100)
    sma200 = sma(close, 200)
    atr50 = atr_series(df, 50)

    # --- 2) ! (sma20 < sma50)@{0..20} — 최근 21봉 내 역배열 금지 ---
    if bars >= 50:
        if (sma20 < sma50).tail(21).any():
            return False, {}

    # --- 3) natr(50) > 2 ---
    natr50 = float(atr50.iloc[-1] / price * 100.0)
    if not (natr50 > NATR_MIN):
        return False, {}

    # --- 4) ! (price < sma50 and sma50 trend_dn 20) — 하락추세 배제 ---
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
    한국장 개별주 전체 스캔.
    반환: (결과 DataFrame, TradingView 붙여넣기용 콤마 티커 문자열)
    """
    def log(msg):
        if verbose:
            print(msg, flush=True)

    today = dt.date.today()
    end = today.strftime("%Y%m%d")
    start = (today - dt.timedelta(days=int(LOOKBACK_DAYS * 1.6))).strftime("%Y%m%d")

    # --- 티커 + 종목명 수집 ---
    tickers = []
    for mkt in MARKETS:
        try:
            tickers.extend(stock.get_market_ticker_list(end, market=mkt))
        except Exception as e:
            log(f"티커 목록 조회 실패({mkt}): {e}")
    if not tickers:
        raise RuntimeError("KRX 티커 목록을 가져오지 못했습니다 (접속 차단 가능성).")

    names = {}
    for t in tickers:
        try:
            names[t] = stock.get_market_ticker_name(t)
        except Exception:
            names[t] = t
    tickers = [
        t for t in tickers
        if not any(kw in names[t] for kw in EXCLUDE_KEYWORDS)
    ]
    log(f"한국장 유니버스: {len(tickers)}종목 (KOSPI+KOSDAQ, 스팩/ETN/리츠 제외)")

    # --- 1차 유동성 프리필터: 당일 스냅샷 거래대금 ---
    liquid = set()
    for mkt in MARKETS:
        try:
            snap = stock.get_market_ohlcv(end, market=mkt)
            hit = snap[snap["거래대금"] > ADVOL_MIN_EOK * 1e8 / 3].index
            liquid.update(hit)
        except Exception as e:
            log(f"프리필터 스냅샷 실패({mkt}): {e}")
    if liquid:
        tickers = [t for t in tickers if t in liquid]
    log(f"유동성 프리필터 후: {len(tickers)}종목 → 정밀 스캔 시작")

    # --- 2차 정밀 스캔 ---
    passed = []
    fail_count = 0
    for i, t in enumerate(tickers, 1):
        df = safe_get_ohlcv(start, end, t)
        if df is None:
            fail_count += 1
            continue
        try:
            ok, detail = check_darren_conditions(df)
            if ok:
                passed.append({"티커": t, "종목명": names[t], **detail})
                log(f"  ✔ {names[t]}({t})  advol60={detail['advol60_억']}억  "
                    f"NATR={detail['natr50_%']}%")
        except Exception:
            continue
        if i % 200 == 0:
            log(f"  ... 진행 {i}/{len(tickers)}")

    # 조회 실패율이 지나치게 높으면 접속 문제로 간주
    if len(tickers) > 0 and fail_count / len(tickers) > 0.5:
        raise RuntimeError(
            f"종목 조회 실패율 과다({fail_count}/{len(tickers)}) — "
            f"GitHub 서버에서 KRX 접속이 차단된 것으로 보입니다."
        )

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
        # KRX 접속 실패 등은 텔레그램으로 알리고 빨간 X 종료
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
