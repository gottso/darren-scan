#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
darren_core.py
==============
Darren 워크플로우 공용 코어 모듈. 주식 스캐너와 ETF 스캐너가 같은 로직을 쓰도록 분리.

구성
  1) 지표      : SMA / Wilder ATR / NATR / advol
  2) 7개 필터  : MarketInOut 조건식 1:1 이식
  3) 랭킹      : 6개 지표 → Tier A/B/C 분류 + 티어 내 정렬 점수

랭킹은 '매수 신호'가 아니라 '차트 볼 순서'입니다.
셋업 캔들 판정은 여전히 사람 눈으로 합니다. (자동화 경계선 유지)

원본 MarketInOut 조건식:
  advol(60) > TH and advol(20) > TH
  and ! (sma(20) < sma(50))@{0..20}
  and natr(50) > NATR_TH
  and ! (price < sma(50) and sma(50) trend_dn 20)
  and ! price < (sma(50) - atr(50))
  and (price > sma(100) or price > sma(200) or bars() < 200)
  and ! (price < sma(50) and price < sma(20))@{0..15}

[변경 이력]
  1. 티어 임계값을 모듈 상수에서 ScanConfig로 이동.
     ETF는 바스켓이라 개별 종목만큼 변동성이 수축하지 않아
     주식(0.75)과 ETF(0.85)의 압축 기준을 분리.

  2. Tier A에 상대강도 하한(tier_a_rs_pct_min) 추가.
     실측에서 티어가 RS 기준으로 역전되는 현상 발견 —
     강한 종목은 강하기 때문에 20SMA에서 멀어지고(이격>2 → Tier B),
     약한 종목은 못 가니까 20SMA에 붙어 있어(이격<2 → Tier A) 후발주가 상위로 올라옴.
     데런 방법론은 리더를 사는 것이므로 RS 하위권은 Tier A에서 배제.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════


@dataclass
class ScanConfig:
    """시장/자산군별 스캔 + 랭킹 파라미터."""

    name: str = "US_STOCK"

    # ── 필터 ──
    advol_min: float = 30.0          # 거래대금 임계값 (unit_divisor 적용 후 기준)
    unit_divisor: float = 1e6        # US=1e6($M) / KR=1e8(억원)
    unit_label: str = "$M"
    natr_min: float = 2.0            # 주식 2.0 / ETF 1.0
    min_price: float = 5.0           # 파이썬 전용 사전 필터
    benchmark: str = "SPY"           # RS 계산 기준
    atr_method: str = "wilder"       # "wilder" | "sma"

    # ── 티어 판정 ──
    tier_a_contraction_max: float = 0.75   # atr5/atr20 — 낮을수록 수축
    tier_a_ext_min: float = 0.0            # 20SMA 이격 하한 (ATR 배수)
    tier_a_ext_max: float = 2.0            # 20SMA 이격 상한
    tier_a_liq_min: float = 1.10           # advol20/advol60 — 거래대금 레벨업
    tier_a_rs_pct_min: float = 50.0        # RS 백분위 하한 — 후발주 배제
    tier_b_ext_min: float = 2.0            # 이 이상은 '확장' 취급
    tier_b_rs_pct_min: float = 70.0        # RS 상위 30%


# ── 티어 내 정렬 점수 가중치 (합 1.0) ──────────────────────────
W_LIQ = 0.25          # 거래대금 레벨업
W_CONTRACTION = 0.25  # 압축도
W_VDU = 0.15          # 거래량 마름
W_EXT = 0.15          # 20SMA 이격 (스위트스팟 근접도)
W_RS = 0.20           # 상대강도

EXT_SWEET_SPOT = 0.75   # 20SMA 위 0.75 ATR을 최적으로 봄
EXT_TOLERANCE = 3.0     # 여기서 ±3 ATR 벗어나면 0점


# ══════════════════════════════════════════════════════════════
# 1) 지표
# ══════════════════════════════════════════════════════════════


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int, method: str = "wilder") -> pd.Series:
    tr = true_range(df)
    if method == "wilder":
        # Wilder RMA = ewm(alpha=1/n). TradingView ta.atr 기본값과 동일
        return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    return tr.rolling(n, min_periods=n).mean()


def natr(df: pd.DataFrame, n: int, method: str = "wilder") -> pd.Series:
    return atr(df, n, method) / df["Close"] * 100.0


def advol(df: pd.DataFrame, n: int, unit_divisor: float) -> pd.Series:
    """평균 거래대금. close * volume 의 n일 단순평균 / unit_divisor."""
    return (df["Close"] * df["Volume"]).rolling(n, min_periods=n).mean() / unit_divisor


def _last(s: pd.Series) -> float:
    if len(s) == 0:
        return float("nan")
    v = s.iloc[-1]
    return float(v) if pd.notna(v) else float("nan")


def _window_has_violation(cond: pd.Series, lookback: int) -> list[int]:
    """cond=True(나쁜 상태)인 봉의 인덱스(0=최신)를 0..lookback 범위에서 수집."""
    n = len(cond)
    hits: list[int] = []
    for i in range(lookback + 1):
        pos = n - 1 - i
        if pos < 0:
            break
        v = cond.iloc[pos]
        if pd.notna(v) and bool(v):
            hits.append(i)
    return hits


# ══════════════════════════════════════════════════════════════
# 2) 7개 필터
# ══════════════════════════════════════════════════════════════


@dataclass
class FilterResult:
    passed: bool
    failed_names: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance MultiIndex 평탄화 + 필수 컬럼만 남기고 결측 제거."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    need = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in need):
        return pd.DataFrame()
    out = df[need].dropna()
    return out[out["Close"] > 0]


def apply_filters(df: pd.DataFrame, cfg: ScanConfig) -> FilterResult:
    """7개 필터 + MIN_PRICE 적용. 실패 필터명과 계산값을 함께 반환."""
    df = normalize_ohlcv(df)
    bars = len(df)
    if bars < 60:
        return FilterResult(False, ["데이터부족"], {"bars": bars})

    close = df["Close"]
    s20, s50 = sma(close, 20), sma(close, 50)
    s100, s200 = sma(close, 100), sma(close, 200)
    a50 = atr(df, 50, cfg.atr_method)
    n50 = natr(df, 50, cfg.atr_method)
    av60 = advol(df, 60, cfg.unit_divisor)
    av20 = advol(df, 20, cfg.unit_divisor)

    price = _last(close)
    v_s20, v_s50 = _last(s20), _last(s50)
    v_a50, v_n50 = _last(a50), _last(n50)
    v_av60, v_av20 = _last(av60), _last(av20)

    failed: list[str] = []

    # 0) 최소 주가
    if not (price >= cfg.min_price):
        failed.append("0.MIN_PRICE")

    # 1) 거래대금
    if not (v_av60 > cfg.advol_min and v_av20 > cfg.advol_min):
        failed.append("1.advol")

    # 2) 최근 21봉 정배열 유지 (상장 50봉 미만 면제)
    hits2: list[int] = []
    if bars >= 50:
        hits2 = _window_has_violation(s20 < s50, 20)
        if hits2:
            failed.append("2.정배열21")

    # 3) 변동성
    if not (v_n50 > cfg.natr_min):
        failed.append("3.NATR")

    # 4) 하락추세 배제
    s50_prev = float(s50.iloc[-21]) if bars > 21 and pd.notna(s50.iloc[-21]) else np.nan
    trend_dn = (not np.isnan(s50_prev)) and (v_s50 < s50_prev)
    if (price < v_s50) and trend_dn:
        failed.append("4.하락추세")

    # 5) 깊은 붕괴 배제
    if not np.isnan(v_s50) and not np.isnan(v_a50) and price < (v_s50 - v_a50):
        failed.append("5.깊은붕괴")

    # 6) 장기추세 (상장 200봉 미만 면제)
    if bars >= 200:
        if not (price > _last(s100) or price > _last(s200)):
            failed.append("6.장기추세")

    # 7) 최근 16봉 이중 이평선 붕괴 배제
    hits7 = _window_has_violation((close < s50) & (close < s20), 15)
    if hits7:
        failed.append("7.이중붕괴16")

    detail = {
        "bars": bars, "close": price, "sma20": v_s20, "sma50": v_s50,
        "atr50": v_a50, "natr50": v_n50, "advol60": v_av60, "advol20": v_av20,
        "hits2": hits2, "hits7": hits7,
    }
    return FilterResult(len(failed) == 0, failed, detail)


# ══════════════════════════════════════════════════════════════
# 3) 랭킹
# ══════════════════════════════════════════════════════════════


def compute_metrics(
    df: pd.DataFrame,
    cfg: ScanConfig,
    bench_ret63: float = 0.0,
    leverage: float = 1.0,
) -> dict:
    """랭킹용 6개 지표 계산. leverage는 RS 정규화에 사용(3x ETF 보정)."""
    df = normalize_ohlcv(df)
    if len(df) < 60:
        return {}

    close, vol, high = df["Close"], df["Volume"], df["High"]
    s20 = sma(close, 20)
    a5 = atr(df, 5, cfg.atr_method)
    a20 = atr(df, 20, cfg.atr_method)

    v_close, v_s20, v_a20 = _last(close), _last(s20), _last(a20)

    # 거래대금 레벨업
    av60 = _last(advol(df, 60, cfg.unit_divisor))
    av20 = _last(advol(df, 20, cfg.unit_divisor))
    liq_ratio = av20 / av60 if av60 and av60 > 0 else np.nan

    # 압축도 (낮을수록 수축)
    v_a5 = _last(a5)
    contraction = v_a5 / v_a20 if v_a20 and v_a20 > 0 else np.nan

    # 거래량 마름 VDU (낮을수록 마름)
    vol5 = _last(vol.rolling(5, min_periods=5).mean())
    vol50 = _last(vol.rolling(50, min_periods=50).mean())
    vdu = vol5 / vol50 if vol50 and vol50 > 0 else np.nan

    # 20SMA 이격 (ATR 배수)
    ext = (v_close - v_s20) / v_a20 if v_a20 and v_a20 > 0 else np.nan

    # 상대강도 — 63거래일(약 3개월), 레버리지 정규화
    if len(close) > 63:
        base = float(close.iloc[-64])
        ret63 = (v_close / base - 1.0) if base > 0 else np.nan
    else:
        ret63 = np.nan
    lev = leverage if leverage and leverage > 0 else 1.0
    rs = ((ret63 - bench_ret63) / lev) if not np.isnan(ret63) else np.nan

    # 52주 고점 근접도
    hi252 = _last(high.rolling(min(252, len(high)), min_periods=20).max())
    near_high = v_close / hi252 if hi252 and hi252 > 0 else np.nan

    return {
        "close": v_close, "advol20": av20, "advol60": av60,
        "liq_ratio": liq_ratio, "contraction": contraction, "vdu": vdu,
        "ext": ext, "ret63": ret63, "rs": rs, "near_high": near_high,
        "natr50": _last(natr(df, 50, cfg.atr_method)),
    }


def _pct_rank(s: pd.Series, ascending: bool = True) -> pd.Series:
    """0~100 백분위. ascending=False면 값이 작을수록 높은 점수."""
    return (s.rank(pct=True, ascending=ascending, na_option="keep") * 100.0).fillna(50.0)


def _ext_score(ext: pd.Series) -> pd.Series:
    """20SMA 이격 스위트스팟 근접도. EXT_SWEET_SPOT에서 100, 멀어질수록 감점."""
    d = (ext - EXT_SWEET_SPOT).abs()
    return ((1.0 - d / EXT_TOLERANCE) * 100.0).clip(lower=0.0, upper=100.0).fillna(0.0)


def assign_tiers(rows: pd.DataFrame, cfg: ScanConfig | None = None) -> pd.DataFrame:
    """
    지표 DataFrame(컬럼: liq_ratio/contraction/vdu/ext/rs)을 받아
    RS 백분위 → 티어 → 티어 내 정렬 점수를 붙여 반환.
    RS 백분위는 횡단면이라 전 종목을 모은 뒤에 계산해야 함.

    Tier A : 눌림+수축이 진행 중이고 확장되지 않았으며 상대강도가 중위 이상
    Tier B : 강하지만 확장 → 20SMA 눌림 대기
    Tier C : 나머지 (상위권은 A 조건 하나만 놓친 near-miss라 같이 훑을 가치 있음)
    """
    if rows.empty:
        return rows
    if cfg is None:
        cfg = ScanConfig()

    df = rows.copy()
    df["rs_pct"] = _pct_rank(df["rs"], ascending=True)

    a = (
        (df["contraction"] <= cfg.tier_a_contraction_max)
        & (df["ext"] >= cfg.tier_a_ext_min)
        & (df["ext"] <= cfg.tier_a_ext_max)
        & (df["liq_ratio"] >= cfg.tier_a_liq_min)
        & (df["rs_pct"] >= cfg.tier_a_rs_pct_min)   # 후발주 배제
    )
    b = (~a) & (df["ext"] > cfg.tier_b_ext_min) & (df["rs_pct"] >= cfg.tier_b_rs_pct_min)

    df["tier"] = np.where(a, "A", np.where(b, "B", "C"))

    df["score"] = (
        W_LIQ * _pct_rank(df["liq_ratio"], ascending=True)
        + W_CONTRACTION * _pct_rank(df["contraction"], ascending=False)  # 낮을수록 고점
        + W_VDU * _pct_rank(df["vdu"], ascending=False)                  # 낮을수록 고점
        + W_EXT * _ext_score(df["ext"])
        + W_RS * df["rs_pct"]
    ).round(1)

    df["_o"] = df["tier"].map({"A": 0, "B": 1, "C": 2})
    df = df.sort_values(["_o", "score"], ascending=[True, False]).drop(columns="_o")
    return df.reset_index(drop=True)


def benchmark_ret63(bench_df: pd.DataFrame) -> float:
    """벤치마크 63거래일 수익률. 실패 시 0.0 (= RS가 절대수익률로 폴백)."""
    bench_df = normalize_ohlcv(bench_df)
    if len(bench_df) < 64:
        return 0.0
    c = bench_df["Close"]
    base = float(c.iloc[-64])
    return (float(c.iloc[-1]) / base - 1.0) if base > 0 else 0.0


def sector_heat(rows: pd.DataFrame, group_col: str = "sector") -> pd.DataFrame:
    """섹터(또는 ETF 카테고리)별 티어 카운트. 드릴다운 메뉴 라벨용."""
    if rows.empty or group_col not in rows.columns:
        return pd.DataFrame()
    t = (
        rows.pivot_table(index=group_col, columns="tier", values="score", aggfunc="count")
        .fillna(0).astype(int)
    )
    for c in ("A", "B", "C"):
        if c not in t.columns:
            t[c] = 0
    t = t[["A", "B", "C"]]
    t["total"] = t.sum(axis=1)
    t["label"] = t.apply(lambda r: f"A{r['A']}/B{r['B']}/C{r['C']}", axis=1)
    return t.sort_values(["A", "total"], ascending=[False, False])


# ══════════════════════════════════════════════════════════════
# 사전 정의 프로파일
# ══════════════════════════════════════════════════════════════

CFG_US_STOCK = ScanConfig(
    name="US_STOCK", advol_min=30.0, unit_divisor=1e6, unit_label="$M",
    natr_min=2.0, min_price=5.0, benchmark="SPY",
    tier_a_contraction_max=0.75, tier_a_rs_pct_min=50.0,
)
CFG_KR_STOCK = ScanConfig(
    name="KR_STOCK", advol_min=300.0, unit_divisor=1e8, unit_label="억",
    natr_min=2.0, min_price=1000.0, benchmark="^KS11",
    tier_a_contraction_max=0.75, tier_a_rs_pct_min=50.0,
)
# ETF는 바스켓이라 개별 종목만큼 변동성이 수축하지 않음.
# 실측: 압축 임계값을 0.85 → 1.00 으로 풀어도 Tier A가 3 → 4 로만 늘어남(평탄 구간).
# 병목은 압축이 아니라 이격/레벨업/RS 쪽이므로 0.85에서 멈추는 게 타당.
CFG_US_ETF = ScanConfig(
    name="US_ETF", advol_min=30.0, unit_divisor=1e6, unit_label="$M",
    natr_min=1.0, min_price=5.0, benchmark="SPY",
    tier_a_contraction_max=0.85, tier_a_rs_pct_min=50.0,
)
# KR ETF: 300억을 그대로 쓰면 KODEX 레버리지급 10여 개만 남음 → 100억으로 낮춤
CFG_KR_ETF = ScanConfig(
    name="KR_ETF", advol_min=100.0, unit_divisor=1e8, unit_label="억",
    natr_min=1.0, min_price=1000.0, benchmark="^KS11",
    tier_a_contraction_max=0.85, tier_a_rs_pct_min=50.0,
)
