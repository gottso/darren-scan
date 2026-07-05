# -*- coding: utf-8 -*-
"""
데런식 주말 워치리스트 자동 스크리너 (한국장: KOSPI/KOSDAQ 개별주) + 텔레그램 알림
================================================================================
GitHub Actions 클라우드 실행용 (야후 파이낸스 가격 + CSV 섹터 + 재스캔 버튼).

구조:
- 종목 목록 + 업종(섹터): 저장소에 올려둔 kr_tickers.csv 에서 읽음
  (make_kr_tickers.py로 본인 PC/코랩에서 1회 생성, KRX-DESC 업종 포함)
- 가격 데이터: 야후 파이낸스(005930.KS / 035720.KQ 형식)로 조회

변경점(v5):
- 섹터를 야후에서 조회하지 않고 CSV의 sector 컬럼을 사용합니다.
  (야후가 한국 종목 섹터를 미분류로 반환하던 문제 해결)
- CSV에 sector 컬럼이 없으면(구버전 CSV) 전부 '미분류'로 처리하고 안내합니다.

※ 이 스캔은 '매수 시그널'이 아니라 후보군(유니버스) 압축 필터입니다.
   베이스/수축/셋업 캔들 판정은 트뷰 바둑판으로 눈으로 하는 겁니다.
"""

import datetime as dt
import json
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
ADVOL_MIN_EOK = 30       # 평균 거래대금 하한 (억 원)
NATR_MIN = 2.0           # NATR(50) 하한 (%)
MIN_PRICE_KRW = 1000     # 초저가주 제외 (원)
BATCH_SIZE = 200
HISTORY_PERIOD = "2y"
TICKER_CSV = "kr_tickers.csv"

# ↓↓↓ [필수 수정] 본인 저장소의 KR 워크플로우 페이지 주소로 바꾸세요 ↓↓↓
#   찾는 법: GitHub 저장소 → Actions 탭 → 왼쪽에서 'Darren KR Weekend Scan'
#            클릭 → 그때 브라우저 주소창의 URL을 그대로 복사
#   형식:   https://github.com/사용자명/저장소명/actions/workflows/darren_kr_scan.yml
GH_ACTIONS_URL = os.environ.get(
    "DARREN_GH_URL_KR",
    "https://github.com/사용자명/저장소명/actions/workflows/darren_kr_scan.yml",
)

TELEGRAM_TOKEN = os.environ.get("DARREN_TG_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("DARREN_TG_CHAT", "").strip()


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
    if not os.path.exists(TICKER_CSV):
        print(f"❌ {TICKER_CSV} 파일이 없습니다.")
        send_telegram(f"⚠️ KR 스캔 중단: {TICKER_CSV} 파일이 저장소에 없습니다.")
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


def send_telegram(text: str, button_url: str = None,
                  button_text: str = None) -> bool:
    """
    텔레그램 전송. 긴 메시지는 줄 단위 4000자 청크로 분할.
    button_url이 있으면 '마지막 청크'에만 URL 버튼(인라인 키보드)을 붙인다.
    """
    chat_id = resolve_chat_id()
    if not chat_id:
        print("chat_id 없음 — 전송 건너뜀.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = [text]

    ok = True
    for idx, chunk in enumerate(chunks):
        data = {"chat_id": chat_id, "text": chunk}
        if button_url and button_text and idx == len(chunks) - 1:
            data["reply_markup"] = json.dumps({
                "inline_keyboard": [[{"text": button_text, "url": button_url}]]
            })
        try:
            r = requests.post(url, data=data, timeout=15)
            if r.status_code != 200:
                print(f"전송 실패: {r.text}")
                ok = False
        except Exception as e:
            print(f"전송 오류: {e}")
            ok = False
    return ok


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
    if price < MIN_PRICE_KRW:
        return False, {}

    dollar_vol = close * vol
    advol60 = float(dollar_vol.tail(60).mean()) / 1e8
    advol20 = float(dollar_vol.tail(20).mean()) / 1e8
    if not (advol60 > ADVOL_MIN_EOK and advol20 > ADVOL_MIN_EOK):
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
        "종가": int(price),
        "advol60_억": round(advol60, 1),
        "natr50_%": round(natr50, 2),
        "gap20선_%": round((price / float(sma20.iloc[-1]) - 1) * 100, 1)
        if bars >= 20 else None,
        "봉수": bars,
        "ipo": bars < 200,
    }


# ============================================================
# 스캔 본체
# ============================================================
def run_kr_scan(verbose: bool = True) -> tuple[pd.DataFrame, str]:
    def log(msg):
        if verbose:
            print(msg, flush=True)

    tk = pd.read_csv(TICKER_CSV, dtype={"code": str})
    yf_tickers = tk["yf_ticker"].tolist()

    # code -> (종목명, 시장, 섹터) 매핑. sector 컬럼이 없으면 '미분류'.
    has_sector = "sector" in tk.columns
    if not has_sector:
        log("⚠️ kr_tickers.csv에 sector 컬럼이 없습니다. "
            "make_kr_tickers.py(섹터 포함 버전)로 CSV를 다시 만들어 주세요.")
    meta = {}
    for _, row in tk.iterrows():
        sec = row["sector"] if has_sector and pd.notna(row.get("sector")) else "미분류"
        meta[row["code"]] = (row["name"], row["market"], sec if sec else "미분류")

    log(f"한국장 유니버스: {len(yf_tickers)}종목 (CSV 로드"
        f"{', 섹터 포함' if has_sector else ', 섹터 없음'})")

    # --- 1차 프리필터 ---
    log("1차 유동성 프리필터 진행 중...")
    survivors = []
    for i in range(0, len(yf_tickers), BATCH_SIZE):
        batch = yf_tickers[i:i + BATCH_SIZE]
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
                if pd.notna(dv) and dv / 1e8 > ADVOL_MIN_EOK / 3:
                    survivors.append(t)
            except (KeyError, TypeError):
                continue
        log(f"  ... {min(i + BATCH_SIZE, len(yf_tickers))}/{len(yf_tickers)} "
            f"(생존 {len(survivors)})")
        time.sleep(0.5)

    log(f"프리필터 통과: {len(survivors)}종목 → 정밀 스캔 시작")

    # --- 2차 정밀 스캔 ---
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
                    code = t.split(".")[0]
                    name, market, sector = meta.get(code, (code, "", "미분류"))
                    passed.append({"티커": code, "종목명": name, "시장": market,
                                   "sector": sector, **detail})
            except (KeyError, TypeError):
                continue
        time.sleep(0.5)

    result = pd.DataFrame(passed)
    if result.empty:
        log("\n최종 통과: 0종목")
        return result, ""

    # 섹터 순서: 섹터별 총 거래대금 큰 순, 섹터 내는 거래대금 내림차순
    order = (result.groupby("sector")["advol60_억"].sum()
             .sort_values(ascending=False).index.tolist())
    result["__sec_order"] = result["sector"].map({s: i for i, s in enumerate(order)})
    result = result.sort_values(
        ["__sec_order", "advol60_억"], ascending=[True, False]
    ).drop(columns="__sec_order")

    tv_string = ",".join(f"KRX:{r['티커']}" for _, r in result.iterrows())
    log(f"\n최종 통과: {len(result)}종목")
    return result, tv_string


# ============================================================
# 리포트 (섹터별)
# ============================================================
def format_report(result: pd.DataFrame, tv_string: str) -> str:
    today = dt.date.today().strftime("%Y-%m-%d")
    lines = [f"📋 데런식 KR 주말 스캔 ({today})"]
    if result.empty:
        lines.append("\n통과 종목 없음. 필터 통과 수 자체가 시장 온도계입니다.")
        return "\n".join(lines)

    lines.append(f"통과 {len(result)}종목 · 섹터별 / 섹터 내 거래대금순\n")
    for sector, grp in result.groupby("sector", sort=False):
        lines.append(f"━━ {sector} ({len(grp)}) ━━")
        for _, row in grp.iterrows():
            ipo = " [IPO]" if row["ipo"] else ""
            lines.append(
                f"• {row['종목명']}({row['티커']}){ipo}  {row['종가']:,}원  "
                f"{row['advol60_억']}억  NATR {row['natr50_%']}%  "
                f"20선 {row['gap20선_%']}%")
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
    send_telegram("🔍 데런식 KR 스캔 시작 (GitHub Actions).")
    try:
        result, tv_string = run_kr_scan(verbose=True)
    except Exception as e:
        err = f"⚠️ KR 스캔 실패: {e}"
        print(err)
        send_telegram(err)
        sys.exit(1)

    today = dt.date.today().strftime("%Y%m%d")
    if not result.empty:
        result.to_csv(f"darren_kr_watchlist_{today}.csv",
                      index=False, encoding="utf-8-sig")

    report = format_report(result, tv_string)
    sent = send_telegram(
        report,
        button_url=GH_ACTIONS_URL,
        button_text="🔄 지금 다시 스캔하기 (KR)",
    )
    if sent:
        print("텔레그램 전송 완료")
    else:
        print("텔레그램 전송 실패:\n" + report)
        sys.exit(1)


if __name__ == "__main__":
    main()
