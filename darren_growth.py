# -*- coding: utf-8 -*-
"""
Top Growth Core 스크리너 — 기존 7개 필터와는 별개의 스크린
================================================================================
기존 darren_us_screener / darren_kr_screener 는 데런의 MarketInOut 주말
워치리스트(7개 필터)를 그대로 옮긴 것이고, 이 파일은 그와 **다른 목적**의
스크린이다. 둘은 서로 대체하지 않고 나란히 돌린다.

  기존 7개 세트  : 건강한 상승추세 종목 전부 (돌파·풀백·수축 단계 모두 포함)
  Top Growth Core: 매출이 크게 늘고 있으면서 지금 조용히 눌려 있는 성장주

【필터】
  가격 / 시가총액 / 60일 평균 거래(대금) / 당일 거래(대금)
  분기 매출 성장률 YoY 25% 초과      ← 펀더멘털
  ADR 3% 이상
  EMA10 > SMA20 > SMA50
  최근 1주 등락 -5% ~ +5%
  Biotechnology 산업 제외
  정렬: ADR 내림차순

【2단계 스캔 — 왜 나눴나】
스캔 시간의 거의 전부는 OHLCV 다운로드(네트워크 I/O)다. 반면 시가총액·
매출성장률·산업분류는 yfinance 에서 **종목당 별도 호출**이라, 수천 종목에
전부 걸면 몇 시간이 걸려 Actions 에서 돌릴 수 없다.

  1단계: 일괄 다운로드 → 가격·거래량·이평선·ADR·주간등락 필터
         (수천 종목 → 수백 종목)
  2단계: 살아남은 종목에만 .info 호출 → 시총·매출성장률·산업
         (수백 종목이면 스레드 병렬로 수 분 내 완료)

【지표 계산】
sma / ema / adr / week_perf 는 전부 darren_core 의 것을 그대로 쓴다.
같은 지표를 두 벌 구현하면 두 스크린의 값이 조용히 어긋나기 때문이다.

【사용법】
  python darren_growth.py --market us --datadir . \
      --csv state/growth_scan --json state/growth_payload_us.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yfinance as yf

import darren_core as dc


# ══════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════

@dataclass
class GrowthConfig:
    name: str = "US_GROWTH"

    # ── 1단계: 가격·거래 필터 ──
    min_price: float = 5.0
    # 거래량 기준을 '주수'로 볼지 '거래대금'으로 볼지 (US=주수, KR=거래대금)
    volume_basis: str = "shares"       # "shares" | "value"
    avg_vol_min: float = 800_000.0     # 60일 평균 (주수 또는 거래대금)
    today_vol_min: float = 500_000.0   # 당일 (주수 또는 거래대금)
    vol_unit_divisor: float = 1.0      # 거래대금일 때 나눌 단위
    vol_unit_label: str = "주"

    adr_min: float = 3.0               # ADR(20) 하한 %
    adr_period: int = 20
    week_perf_abs_max: float = 5.0     # 최근 1주 등락 ±%
    week_perf_bars: int = 5
    require_ema10_above_sma20: bool = True
    require_sma20_above_sma50: bool = True

    # ── 2단계: 펀더멘털 필터 ──
    min_market_cap: float = 300_000_000.0     # 시가총액 하한 (현지 통화)
    market_cap_label: str = "$300M"
    rev_growth_min: float = 0.25              # 분기 매출 성장률 YoY 하한
    excluded_industries: tuple = ("biotechnology",)

    benchmark: str = "SPY"
    universe_file: str = ""          # KR 만 사용 (US 는 웹에서 받음)


CFG_US_GROWTH = GrowthConfig(
    name="US_GROWTH",
    min_price=5.0,
    volume_basis="shares", avg_vol_min=800_000.0, today_vol_min=500_000.0,
    vol_unit_divisor=1.0, vol_unit_label="주",
    min_market_cap=300_000_000.0, market_cap_label="$300M",
    benchmark="SPY", universe_file="",
)

CFG_KR_GROWTH = GrowthConfig(
    name="KR_GROWTH",
    min_price=2_000.0,
    # 한국은 주수보다 거래대금이 의미 있다 (2천원 20만주 ≠ 10만원 20만주)
    volume_basis="value",
    avg_vol_min=30.0,          # 60일 평균 거래대금 3B KRW = 30억
    today_vol_min=10.0,        # 당일 거래대금 1B KRW = 10억
    vol_unit_divisor=1e8, vol_unit_label="억",
    min_market_cap=100_000_000_000.0, market_cap_label="1000억",
    benchmark="^KS11", universe_file="kr_tickers.csv",
)


# ══════════════════════════════════════════════════════════════
# 유니버스
# ══════════════════════════════════════════════════════════════
# US 는 darren_us_screener.py 와 동일하게 nasdaqtrader.com 목록을 받아 쓴다.
# (CSV 파일이 아니다 — 기존 스캔과 유니버스가 어긋나면 두 스크린을 비교할 수
#  없으므로 같은 출처를 쓰는 것이 중요하다.)
# KR 은 기존 KR 스크리너와 같이 kr_tickers.csv 를 쓴다.

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

TICKER_COLS = ["yf_ticker", "티커", "ticker", "Ticker", "종목코드", "symbol", "code"]
NAME_COLS = ["종목명", "name", "Name", "이름", "회사명"]
SECTOR_COLS = ["sector", "Sector", "섹터", "업종"]


def _find_col(df, cands):
    for c in cands:
        if c in df.columns:
            return c
    return None


def get_us_universe() -> list[str]:
    """NYSE + NASDAQ 개별주 목록 (ETF·테스트 종목 제외).

    darren_us_screener.get_us_universe() 와 동일한 로직·필터를 쓴다.
    """
    import io
    import requests

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
    df = df[df["Exchange"].isin(["N"])]          # NYSE 만
    if "ETF" in df.columns:
        df = df[df["ETF"] == "N"]
    tickers.update(df["ACT Symbol"].dropna().astype(str))

    return sorted(t for t in tickers
                  if t.isalpha() and 1 <= len(t) <= 5 and "File" not in t)


def load_universe(cfg: GrowthConfig, datadir: str) -> pd.DataFrame:
    """(ticker, name, sector) 컬럼의 유니버스를 만든다."""
    if cfg.name.startswith("US"):
        tickers = get_us_universe()
        return pd.DataFrame({"ticker": tickers, "name": "", "sector": ""})

    path = os.path.join(datadir, cfg.universe_file)
    if not os.path.exists(path):
        print(f"[오류] 유니버스 파일이 없습니다: {path}")
        print("       기존 KR 스크리너가 쓰는 kr_tickers.csv 를 같은 위치에 두세요.")
        sys.exit(1)

    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    tcol = _find_col(df, TICKER_COLS)
    if not tcol:
        print(f"[오류] 티커 컬럼을 못 찾았습니다. 컬럼: {list(df.columns)}")
        sys.exit(1)
    ncol = _find_col(df, NAME_COLS)
    scol = _find_col(df, SECTOR_COLS)

    out = pd.DataFrame({"ticker": df[tcol].astype(str).str.strip()})
    out["name"] = df[ncol].astype(str).str.strip() if ncol else ""
    out["sector"] = df[scol].astype(str).str.strip() if scol else ""
    out = out[out["ticker"].str.len() > 0].drop_duplicates("ticker")
    return out.reset_index(drop=True)


def prefilter_liquidity(tickers: list[str], cfg: GrowthConfig,
                        batch: int = 200) -> list[str]:
    """5일치만 받아 거래량이 턱없이 적은 종목을 싸게 걷어낸다.

    darren_us_screener 가 쓰는 방식과 같다. 2년치를 수천 종목에 받으면
    시간이 폭증하므로, 먼저 5일치로 후보를 3분의 1 기준으로 거른 뒤
    살아남은 종목에만 긴 히스토리를 받는다.
    """
    survivors = []
    total = len(tickers)
    threshold = cfg.avg_vol_min / 3.0
    print(f"[프리필터] {total}종목 · 기준 {threshold:,.0f}{cfg.vol_unit_label}")

    for i in range(0, total, batch):
        chunk = tickers[i:i + batch]
        try:
            data = yf.download(chunk, period="5d", interval="1d",
                               group_by="ticker", progress=False,
                               threads=True, auto_adjust=True)
        except Exception:
            continue
        for t in chunk:
            try:
                d = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                if cfg.volume_basis == "value":
                    v = float((d["Close"] * d["Volume"]).mean()) / cfg.vol_unit_divisor
                else:
                    v = float(d["Volume"].mean())
                if pd.notna(v) and v > threshold:
                    survivors.append(t)
            except (KeyError, TypeError, ValueError):
                continue
        print(f"  {min(i + batch, total)}/{total} · 생존 {len(survivors)}")
        time.sleep(0.5)

    return survivors


# ══════════════════════════════════════════════════════════════
# 1단계 — 가격·거래 필터 (일괄 다운로드)
# ══════════════════════════════════════════════════════════════

def stage1_check(df: pd.DataFrame, cfg: GrowthConfig) -> tuple[bool, list, dict]:
    """OHLCV 하나를 받아 1단계 필터를 적용. (통과여부, 실패목록, 지표)"""
    df = dc.normalize_ohlcv(df)
    if len(df) < 60:
        return False, ["데이터부족"], {"bars": len(df)}

    close, vol = df["Close"], df["Volume"]
    price = float(close.iloc[-1])
    failed = []

    # 거래량 기준: 미국은 주수, 한국은 거래대금
    if cfg.volume_basis == "value":
        vol_series = (close * vol) / cfg.vol_unit_divisor
    else:
        vol_series = vol
    avg_vol = float(vol_series.rolling(60, min_periods=60).mean().iloc[-1]) \
        if len(vol_series) >= 60 else np.nan
    today_vol = float(vol_series.iloc[-1])

    v_ema10 = dc._last(dc.ema(close, 10))
    v_s20 = dc._last(dc.sma(close, 20))
    v_s50 = dc._last(dc.sma(close, 50))
    v_adr = dc._last(dc.adr(df, cfg.adr_period))
    v_wperf = dc.week_perf(close, cfg.week_perf_bars)

    if not (price >= cfg.min_price):
        failed.append("가격")
    if np.isnan(avg_vol) or not (avg_vol > cfg.avg_vol_min):
        failed.append("60일평균거래")
    if not (today_vol > cfg.today_vol_min):
        failed.append("당일거래")
    if np.isnan(v_adr) or not (v_adr >= cfg.adr_min):
        failed.append("ADR")
    if cfg.require_ema10_above_sma20:
        if np.isnan(v_ema10) or np.isnan(v_s20) or not (v_ema10 > v_s20):
            failed.append("EMA10>SMA20")
    if cfg.require_sma20_above_sma50:
        if np.isnan(v_s20) or np.isnan(v_s50) or not (v_s20 > v_s50):
            failed.append("SMA20>SMA50")
    if np.isnan(v_wperf) or abs(v_wperf) > cfg.week_perf_abs_max:
        failed.append("주간횡보")

    metrics = {
        "close": price, "avg_vol60": avg_vol, "today_vol": today_vol,
        "adr": v_adr, "week_perf": v_wperf,
        "ema10": v_ema10, "sma20": v_s20, "sma50": v_s50,
        "natr50": dc._last(dc.natr(df, 50)),
    }
    return (len(failed) == 0), failed, metrics


def run_stage1(universe: pd.DataFrame, cfg: GrowthConfig,
               batch: int = 200, period: str = "1y",
               use_prefilter: bool = True) -> pd.DataFrame:
    """유동성 프리필터 → 정밀 가격 필터."""
    tickers = universe["ticker"].tolist()
    if use_prefilter:
        tickers = prefilter_liquidity(tickers, cfg, batch=batch)
        print(f"[프리필터] 통과 {len(tickers)}종목 → 정밀 스캔")
    rows = []
    total = len(tickers)
    print(f"[1단계] {total}종목 가격 필터 시작")

    for i in range(0, total, batch):
        chunk = tickers[i:i + batch]
        try:
            data = yf.download(chunk, period=period, interval="1d",
                               group_by="ticker", progress=False,
                               auto_adjust=False, threads=True)
        except Exception as e:
            print(f"  배치 {i // batch + 1} 다운로드 실패: {e}")
            continue

        for t in chunk:
            try:
                sub = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                if sub is None or sub.dropna(how="all").empty:
                    continue
                ok, failed, m = stage1_check(sub, cfg)
                if ok:
                    rows.append({"ticker": t, **m})
            except Exception:
                continue

        done = min(i + batch, total)
        print(f"  {done}/{total} 처리 · 1단계 통과 {len(rows)}")
        time.sleep(0.5)

    res = pd.DataFrame(rows)
    if res.empty:
        return res
    return res.merge(universe, on="ticker", how="left")


# ══════════════════════════════════════════════════════════════
# 2단계 — 펀더멘털 (통과 종목에만 개별 호출)
# ══════════════════════════════════════════════════════════════

def fetch_fundamental(ticker: str, retries: int = 2) -> dict:
    """시가총액 · 분기 매출성장률(YoY) · 산업분류.

    info['revenueGrowth'] 가 분기 매출 성장률 YoY 이다.
    없으면 분기 손익계산서에서 직접 계산한다(5개 분기 이상 있을 때만 가능).
    """
    for attempt in range(retries + 1):
        try:
            tk = yf.Ticker(ticker)
            info = tk.info or {}
            cap = info.get("marketCap")
            growth = info.get("revenueGrowth")
            industry = info.get("industry") or ""
            sector = info.get("sector") or ""

            if growth is None:
                try:
                    q = tk.quarterly_income_stmt
                    if q is not None and "Total Revenue" in q.index and q.shape[1] >= 5:
                        rev = q.loc["Total Revenue"]
                        base = float(rev.iloc[4])
                        if base > 0:
                            growth = float(rev.iloc[0]) / base - 1.0
                except Exception:
                    pass

            return {"ticker": ticker, "market_cap": cap,
                    "rev_growth": growth, "industry": industry,
                    "yf_sector": sector, "fund_ok": True}
        except Exception as e:
            if attempt >= retries:
                return {"ticker": ticker, "market_cap": None, "rev_growth": None,
                        "industry": "", "yf_sector": "", "fund_ok": False,
                        "error": str(e)[:80]}
            time.sleep(1.0 + attempt)
    return {"ticker": ticker, "fund_ok": False}


def run_stage2(cand: pd.DataFrame, cfg: GrowthConfig, workers: int = 8) -> pd.DataFrame:
    """1단계 통과 종목에만 펀더멘털을 조회하고 2단계 필터를 적용."""
    if cand.empty:
        return cand
    tickers = cand["ticker"].tolist()
    print(f"[2단계] {len(tickers)}종목 펀더멘털 조회 (스레드 {workers})")

    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_fundamental, t): t for t in tickers}
        for n, f in enumerate(as_completed(futs), 1):
            out.append(f.result())
            if n % 50 == 0:
                print(f"  {n}/{len(tickers)} 조회 완료")

    fund = pd.DataFrame(out)
    df = cand.merge(fund, on="ticker", how="left")

    fail_cnt = int((~df["fund_ok"].fillna(False)).sum())
    if fail_cnt:
        print(f"  조회 실패 {fail_cnt}종목 — 판정 불가로 제외합니다")

    # 시가총액
    cap_ok = pd.to_numeric(df["market_cap"], errors="coerce") >= cfg.min_market_cap
    # 매출 성장률
    g = pd.to_numeric(df["rev_growth"], errors="coerce")
    growth_ok = g > cfg.rev_growth_min
    # 산업 제외 (대소문자·부분일치)
    ind = df["industry"].fillna("").str.lower()
    excluded = pd.Series(False, index=df.index)
    for kw in cfg.excluded_industries:
        excluded |= ind.str.contains(kw, na=False)

    df["cap_ok"] = cap_ok
    df["growth_ok"] = growth_ok
    df["excluded_industry"] = excluded

    passed = df[cap_ok & growth_ok & ~excluded & df["fund_ok"].fillna(False)].copy()
    print(f"  시총 통과 {int(cap_ok.sum())} · 성장률 통과 {int(growth_ok.sum())}"
          f" · 산업 제외 {int(excluded.sum())} → 최종 {len(passed)}")
    return passed


# ══════════════════════════════════════════════════════════════
# 출력
# ══════════════════════════════════════════════════════════════

def to_payload(res: pd.DataFrame, market: str, max_per_sector: int = 40) -> dict:
    """텔레그램 드릴다운용 페이로드.

    기존 주식 스캔의 state/last_{market}_buttons.json 과 같은 형식이라
    Worker 쪽 키보드 빌더를 그대로 재사용할 수 있다.
    """
    sectors = []
    if not res.empty:
        key = "yf_sector" if res["yf_sector"].str.len().gt(0).any() else "sector"
        res = res.copy()
        res[key] = res[key].replace("", np.nan).fillna("미분류")
        # ADR 내림차순 (원본 스크린의 정렬 기준)
        res = res.sort_values("adr", ascending=False)
        for name, g in res.groupby(key, sort=False):
            sectors.append({"name": str(name),
                            "tickers": g["ticker"].head(max_per_sector).tolist()})
    return {
        "market": market,
        "date": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "total": 0 if res.empty else len(res),
        "sectors": sectors,
    }


# ══════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Top Growth Core 스크리너")
    ap.add_argument("--market", choices=["us", "kr"], required=True)
    ap.add_argument("--datadir", default=".")
    ap.add_argument("--csv", default="", help="CSV 저장 경로 접두사")
    ap.add_argument("--json", default="", help="텔레그램 페이로드 저장 경로")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="유니버스 상위 N개만 (테스트용)")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="유동성 프리필터를 건너뛴다 (소규모 테스트용)")
    args = ap.parse_args()

    cfg = CFG_US_GROWTH if args.market == "us" else CFG_KR_GROWTH
    print(f"=== {cfg.name} 스캔 ===")

    uni = load_universe(cfg, args.datadir)
    if args.limit:
        uni = uni.head(args.limit)
    print(f"유니버스 {len(uni)}종목")

    cand = run_stage1(uni, cfg, batch=args.batch,
                      use_prefilter=not args.no_prefilter)
    if cand.empty:
        print("1단계 통과 종목 없음 — 종료")
        res = cand
    else:
        res = run_stage2(cand, cfg, workers=args.workers)

    if not res.empty:
        res = res.sort_values("adr", ascending=False)
        cols = ["ticker", "name", "yf_sector", "industry", "close", "adr",
                "week_perf", "rev_growth", "market_cap", "avg_vol60", "natr50"]
        cols = [c for c in cols if c in res.columns]
        res = res[cols]

    if args.csv:
        path = f"{args.csv}_{args.market}_growth.csv"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        res.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"CSV 저장 → {path}")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(to_payload(res, args.market), f, ensure_ascii=False, indent=2)
        print(f"페이로드 저장 → {args.json}")

    print(f"\n최종 통과: {len(res)}종목")
    if not res.empty:
        print(res.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
