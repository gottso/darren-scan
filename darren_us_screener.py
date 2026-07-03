# -*- coding: utf-8 -*-
"""
데런식 주말 워치리스트 자동 스크리너 (미국장) + 텔레그램 알림
=============================================================
GitHub Actions 클라우드 실행용 버전 v2.
- 토큰/chat_id는 환경변수(GitHub Secrets)에서만 읽습니다.
- v2 변경점: Secret이 없으면 스캔 시작 전에 즉시 실패(빨간 X)하여
  '스캔은 성공했는데 메시지만 조용히 안 오는' 상황을 방지합니다.

원본 조건식 (exch만 미국으로 변경):
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
# 설정 (데런 원문 기준값 — 본인 스타일에 맞게 조절)
# ============================================================
ADVOL_MIN_M = 30.0        # 평균 거래대금 하한 (백만 달러). 후보 많으면 40/50/60 상향
NATR_MIN = 2.0            # NATR(50) 하한 (%). 더 빠른 종목만 원하면 3.0
MIN_PRICE = 3.0           # 초저가 잡주 제외 (달러)
BATCH_SIZE = 200          # yfinance 배치 다운로드 크기
HISTORY_PERIOD = "2y"     # 정밀 스캔용 데이터 기간 (200봉 이상 확보)

# 텔레그램 — GitHub Secrets에서 주입됨 (코드에 토큰을 넣지 마세요)
TELEGRAM_TOKEN = os.environ.get("DARREN_TG_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("DARREN_TG_CHAT", "").strip()  # 비우면 자동 탐지

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


# ============================================================
# 사전 점검: Secret 없으면 스캔 전에 즉시 실패
# ============================================================
def preflight_check():
    """토큰 유효성 + chat_id 확보를 스캔 전에 검증. 실패 시 즉시 종료."""
    if not TELEGRAM_TOKEN:
        print("=" * 60)
        print("❌ DARREN_TG_TOKEN Secret이 설정되지 않았습니다.")
        print("   저장소 → Settings → Secrets and variables → Actions")
        print("   → New repository secret → 이름: DARREN_TG_TOKEN")
        print("=" * 60)
        sys.exit(1)

    # 토큰 생존 확인 (getMe)
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
            timeout=15,
        )
        data = r.json()
        if not data.get("ok"):
            print(f"❌ 토큰이 유효하지 않습니다 (폐기된 토큰?): {data}")
            sys.exit(1)
        print(f"✅ 봇 연결 확인: @{data['result'].get('username', '?')}")
    except Exception as e:
        print(f"❌ 텔레그램 API 접속 실패: {e}")
        sys.exit(1)

    # chat_id 확보
    chat_id = resolve_chat_id()
    if not chat_id:
        print("=" * 60)
        print("❌ chat_id를 확보하지 못했습니다.")
        print("   방법 1(권장): DARREN_TG_CHAT Secret에 chat_id 숫자 등록")
        print("   방법 2: 봇에게 메시지를 하나 보낸 직후 재실행")
        print("=" * 60)
        sys.exit(1)
    print(f"✅ chat_id 확인: {chat_id}")


# ============================================================
# 텔레그램: chat_id 자동 탐지
# ============================================================
def resolve_chat_id() -> str:
    """
    TELEGRAM_CHAT_ID가 비어 있으면 getUpdates에서 자동으로 찾는다.
    (사전에 봇에게 아무 메시지나 하나 보내둬야 함)
    """
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
                    print(f"chat_id 자동 탐지 성공: {TELEGRAM_CHAT_ID}")
                    print("→ 이 값을 GitHub Secret DARREN_TG_CHAT 에 "
                          "등록해 두면 이후 실행이 안정적입니다.")
                    return TELEGRAM_CHAT_ID
    except Exception as e:
        print(f"getUpdates 오류: {e}")
    return ""


# ============================================================
# 유니버스 수집 (NYSE + NASDAQ 전 종목)
# ============================================================
def get_us_universe() -> list[str]:
    """나스닥 트레이더 공식 심볼 파일에서 NYSE/NASDAQ 보통주 티커 수집"""
    tickers = set()

    # NASDAQ 상장
    r = requests.get(NASDAQ_LISTED_URL, timeout=30)
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["Test Issue"] == "N"]
    if "ETF" in df.columns:
        df = df[df["ETF"] == "N"]  # ETF 제외 (개별주 스캔)
    tickers.update(df["Symbol"].dropna().astype(str))

    # NYSE 등 기타 거래소
    r = requests.get(OTHER_LISTED_URL, timeout=30)
    df = pd.read_csv(io.StringIO(r.text), sep="|")
    df = df[df["Test Issue"] == "N"]
    df = df[df["Exchange"].isin(["N"])]  # N = NYSE
    if "ETF" in df.columns:
        df = df[df["ETF"] == "N"]
    tickers.update(df["ACT Symbol"].dropna().astype(str))

    # 우선주/워런트/유닛 등 특수 심볼 제거
    clean = [
        t for t in tickers
        if t.isalpha() and 1 <= len(t) <= 5 and "File" not in t
    ]
    return sorted(clean)


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
    """
    df: 컬럼 Open/High/Low/Close/Volume 인 일봉 데이터
    반환: (통과 여부, 상세 지표)
    """
    df = df.dropna(subset=["Close", "Volume"])
    bars = len(df)
    if bars < 60:
        return False, {}

    close = df["Close"]
    high, low, vol = df["High"], df["Low"], df["Volume"]
    price = float(close.iloc[-1])

    if price < MIN_PRICE:
        return False, {}

    # --- 1) advol: 평균 거래대금 (백만 달러) ---
    dollar_vol = close * vol
    advol60 = float(dollar_vol.tail(60).mean()) / 1e6
    advol20 = float(dollar_vol.tail(20).mean()) / 1e6
    if not (advol60 > ADVOL_MIN_M and advol20 > ADVOL_MIN_M):
        return False, {}

    sma20 = sma(close, 20)
    sma50 = sma(close, 50)
    sma100 = sma(close, 100)
    sma200 = sma(close, 200)
    atr50 = atr_series(high, low, close, 50)

    # --- 2) ! (sma20 < sma50)@{0..20} — 최근 21봉 내 역배열 발생 금지 ---
    if bars >= 50:
        if (sma20 < sma50).tail(21).any():
            return False, {}
    # bars < 50: IPO 신생주 → 조건 면제

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

    # --- 5) ! price < (sma50 - atr50) — 50선에서 깊게 무너진 종목 배제 ---
    if bars >= 50:
        if price < (float(sma50.iloc[-1]) - float(atr50.iloc[-1])):
            return False, {}

    # --- 6) (price > sma100 or price > sma200 or bars < 200) ---
    if bars < 200:
        pass  # IPO 신생주 허용
    else:
        if not (price > float(sma100.iloc[-1]) or price > float(sma200.iloc[-1])):
            return False, {}

    # --- 7) ! (price < sma20 and price < sma50)@{0..15} ---
    if bars >= 50:
        both_below = ((close < sma20) & (close < sma50)).tail(16)
        if both_below.any():
            return False, {}

    detail = {
        "close": round(price, 2),
        "advol60_M$": round(advol60, 1),
        "natr50_%": round(natr50, 2),
        "gap_20sma_%": round((price / float(sma20.iloc[-1]) - 1) * 100, 1)
        if bars >= 20 else None,
        "bars": bars,
        "ipo": bars < 200,
    }
    return True, detail


# ============================================================
# 스캔 본체
# ============================================================
def run_us_scan(verbose: bool = True) -> tuple[pd.DataFrame, str]:
    """
    미국장 전체 스캔 실행.
    반환: (결과 DataFrame, TradingView 붙여넣기용 콤마 티커 문자열)
    """
    def log(msg):
        if verbose:
            print(msg, flush=True)

    universe = get_us_universe()
    log(f"미국장 유니버스: {len(universe)}종목 (NYSE+NASDAQ, ETF 제외)")

    # --- 1차 프리필터: 최근 5일 데이터로 거래대금 압축 ---
    log("1차 유동성 프리필터 진행 중...")
    survivors = []
    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i:i + BATCH_SIZE]
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
                if pd.notna(dv) and dv / 1e6 > ADVOL_MIN_M / 3:
                    survivors.append(t)
            except (KeyError, TypeError):
                continue
        log(f"  ... {min(i + BATCH_SIZE, len(universe))}/{len(universe)} "
            f"(현재 생존 {len(survivors)})")
        time.sleep(0.5)

    log(f"프리필터 통과: {len(survivors)}종목 → 정밀 스캔 시작")

    # --- 2차 정밀 스캔: 2년치 데이터로 7개 조건 전체 판정 ---
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
                    passed.append({"ticker": t, **detail})
                    log(f"  ✔ {t}  advol60=${detail['advol60_M$']}M  "
                        f"NATR={detail['natr50_%']}%")
            except (KeyError, TypeError):
                continue
        time.sleep(0.5)

    result = pd.DataFrame(passed)
    if not result.empty:
        result = result.sort_values("advol60_M$", ascending=False)
        tv_string = ",".join(result["ticker"])
    else:
        tv_string = ""

    log(f"\n최종 통과: {len(result)}종목")
    return result, tv_string


# ============================================================
# 텔레그램 전송
# ============================================================
def send_telegram(text: str) -> bool:
    """텔레그램 Bot API로 메시지 전송. 4096자 제한 자동 분할."""
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
                url,
                data={"chat_id": chat_id, "text": chunk},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"텔레그램 전송 실패: {r.text}")
                ok = False
        except Exception as e:
            print(f"텔레그램 전송 오류: {e}")
            ok = False
    return ok


def format_report(result: pd.DataFrame, tv_string: str) -> str:
    """텔레그램용 리포트 포맷"""
    today = dt.date.today().strftime("%Y-%m-%d")
    lines = [f"📋 데런식 US 주말 스캔 ({today})"]

    if result.empty:
        lines.append("\n통과 종목 없음.")
        lines.append("필터 통과 수 자체가 시장 온도계입니다 — "
                     "지금은 시장이 차갑다는 신호일 수 있습니다.")
        return "\n".join(lines)

    lines.append(f"통과: {len(result)}종목 (거래대금순)\n")
    for _, row in result.head(40).iterrows():
        ipo_tag = " [IPO]" if row["ipo"] else ""
        lines.append(
            f"• {row['ticker']}{ipo_tag}  ${row['close']}  "
            f"거래대금 ${row['advol60_M$']}M  NATR {row['natr50_%']}%  "
            f"20선괴리 {row['gap_20sma_%']}%"
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
    # Secret 미설정/토큰 무효/chat_id 실패 시 여기서 즉시 빨간 X
    preflight_check()

    send_telegram("🔍 데런식 US 스캔 시작 (GitHub Actions). "
                  "완료까지 15~40분 소요됩니다.")

    result, tv_string = run_us_scan(verbose=True)

    # CSV 저장 (Actions 아티팩트로 업로드됨)
    today = dt.date.today().strftime("%Y%m%d")
    if not result.empty:
        result.to_csv(f"darren_us_watchlist_{today}.csv",
                      index=False, encoding="utf-8-sig")

    report = format_report(result, tv_string)
    sent = send_telegram(report)
    if sent:
        print("텔레그램 전송 완료")
    else:
        print("텔레그램 전송 실패 — 콘솔 출력:")
        print(report)
        sys.exit(1)  # 전송 실패도 빨간 X로 표시


if __name__ == "__main__":
    main()
