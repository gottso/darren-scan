# -*- coding: utf-8 -*-
"""
DPBO_Sys (Pine v6) 일봉 로직 Python 이식
================================================================================
TradingView 인디케이터 "Darren | PBO 멀티 타임프레임 시스템"의 f_logic() 중
**일봉 트랙**을 그대로 옮긴 것. WUC / CC / Setup / Setup★ 를 판정한다.

【이식 범위】
  포함: f_logic() 전체 (WUC, CC, Setup A~E, 등급)
  제외: 15분봉 MTF 트랙 (request.security), 라벨 그리기, 포지션 관리(7절)
        → 15분봉 트랙만 lookahead_on을 쓰므로, 일봉만 이식하면 리페인팅 문제 없음

【Pine ↔ pandas 대응】
  ta.sma(x, n)      → x.rolling(n, min_periods=n).mean()
  ta.rma(x, n)      → x.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
  ta.atr(n)         → rma(true_range, n)          (Pine ta.atr 기본이 RMA)
  ta.atr(1)         → alpha=1 이므로 당봉 TR 그 자체
  ta.rsi(c, n)      → RMA로 평활한 상승분/하락분 비율
  ta.highest(x, n)  → x.rolling(n).max()
  ta.lowest(x, n)   → x.rolling(n).min()
  x[k]              → x.shift(k)
  ta.barssince(c)   → 마지막으로 c가 True였던 이후 경과 봉 수 (당봉 True면 0)

【알려진 미세 차이】
  Pine의 ta.rma는 최초 n개를 SMA로 시드한 뒤 RMA를 적용하고,
  pandas ewm(adjust=False)는 첫 값으로 시드한다. 시계열 앞부분에서만
  값이 다르고 뒤로 갈수록 수렴하므로, 2년치(약 500봉)를 넣으면
  최신 봉에서의 차이는 무시할 수준이다. 데이터를 짧게 넣으면 어긋날 수 있다.

【거래대금 단위 주의】
  Pine 원본은 시장과 무관하게 dollarVol/1e6 을 쓴다. 따라서 KR 종목을
  TradingView에서 보면 CC의 거래대금 조건(minAdVolCC=3.0)이 "300만원 초과"가
  되어 사실상 무력화된다. 여기서도 기본값은 원본과 동일하게 1e6 을 써서
  차트와 결과가 일치하도록 했다. KR에서 이 조건을 실제로 쓰고 싶으면
  advol_divisor 를 1e8(억원)로, min_advol_cc 를 적절히 올려서 호출할 것.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════
# Pine 입력 기본값 (인디케이터 설정 화면의 default 와 동일)
# ══════════════════════════════════════════════════════════════

@dataclass
class PBOConfig:
    # PPC/WUC
    vol_mult_ppc: float = 1.5        # volMultPPC — WUC 거래량 스파이크 배수

    # CC
    min_advol_cc: float = 3.0        # minAdVolCC (M$)
    natr_thresh_cc: float = 1.5      # natrThreshCC
    use_vdu_filter: bool = False     # useVduFilter (기본 꺼짐)
    advol_divisor: float = 1e6       # Pine 원본 하드코딩 값

    # Setup Candle
    zone_len: int = 5                # zoneLen — 컨제션 존 길이
    close_pos_min: float = 0.7       # closePosMin — 종가 위치 최소치
    max_stop_atr: float = 2.0        # maxStopATR — 최대 스탑폭(ATR20 배수)
    vol_confirm_mult: float = 1.3    # volConfirmMult — 등급용 거래량 배수
    cc_window: int = 3               # ccWindow — CC 이후 셋업 허용 봉 수


# ══════════════════════════════════════════════════════════════
# Pine 내장함수 대응 구현
# ══════════════════════════════════════════════════════════════

def rma(s: pd.Series, n: int) -> pd.Series:
    """Pine ta.rma — Wilder 평활. n=1이면 원본 그대로 반환."""
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["Close"].shift(1)
    return pd.concat([
        df["High"] - df["Low"],
        (df["High"] - pc).abs(),
        (df["Low"] - pc).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Pine ta.atr(n) = rma(tr, n)."""
    return rma(true_range(df), n)


def rsi(close: pd.Series, n: int) -> pd.Series:
    """Pine ta.rsi(close, n) — 상승분/하락분을 RMA로 평활."""
    diff = close.diff()
    up = diff.clip(lower=0.0)
    down = (-diff).clip(lower=0.0)
    ru, rd = rma(up, n), rma(down, n)
    out = pd.Series(np.nan, index=close.index, dtype="float64")
    mask = ru.notna() & rd.notna()
    # rd == 0 이면 RSI 100, ru == 0 이면 0 (Pine 동작과 동일)
    rs = ru.where(rd != 0) / rd.where(rd != 0)
    out[mask] = 100.0 - 100.0 / (1.0 + rs[mask])
    out[mask & (rd == 0)] = 100.0
    out[mask & (ru == 0)] = 0.0
    return out


def barssince(cond: pd.Series) -> pd.Series:
    """Pine ta.barssince — 마지막 True 이후 경과 봉 수. 당봉 True면 0.

    한 번도 True가 없었으면 NaN (Pine의 na 와 동일한 취급).
    """
    c = cond.fillna(False).astype(bool).to_numpy()
    idx = np.arange(len(c))
    last = np.where(c, idx, -1)
    last = np.maximum.accumulate(last)
    out = np.where(last >= 0, idx - last, np.nan).astype("float64")
    return pd.Series(out, index=cond.index)


# ══════════════════════════════════════════════════════════════
# f_logic() 이식 — 일봉 트랙
# ══════════════════════════════════════════════════════════════

def compute_pbo(df: pd.DataFrame, cfg: PBOConfig | None = None) -> pd.DataFrame:
    """OHLCV 일봉을 받아 WUC/CC/Setup 시리즈와 중간 계산값을 반환.

    반환 컬럼:
      wuc, cc, setup, setup_best  (bool)
      condA~condE                 (bool, 셋업 세부 진단용)
      zone_high, close_pos, atr20, sma20, bars_since_cc ...
    """
    cfg = cfg or PBOConfig()
    d = df.copy()
    o, h, l, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]

    dollar_vol = c * v
    sma20 = c.rolling(20, min_periods=20).mean()
    sma50 = c.rolling(50, min_periods=50).mean()
    sma100 = c.rolling(100, min_periods=100).mean()
    sma200 = c.rolling(200, min_periods=200).mean()

    atr1 = atr(d, 1)     # alpha=1 → 당봉 TR
    atr5 = atr(d, 5)
    atr20 = atr(d, 20)
    atr50 = atr(d, 50)

    av50 = v.rolling(50, min_periods=50).mean()

    # ── WUC ─────────────────────────────────────────────
    # (volume > av50*1.5) and (close>open) and ((close-open)/open > 0.03 or low > high[1])
    gap_up = l > h.shift(1)
    wuc = (v > av50 * cfg.vol_mult_ppc) & (c > o) & (((c - o) / o > 0.03) | gap_up)

    # ── CC ──────────────────────────────────────────────
    advol_cc = (dollar_vol.rolling(30, min_periods=30).mean() / cfg.advol_divisor) > cfg.min_advol_cc
    no_sma20_under50_20 = (sma20 - sma50).rolling(21, min_periods=21).min() >= 0
    above_major = (c > sma100) | (c > sma200)

    atr_contract = (
        (atr1 < atr5 * 0.5) | (atr1 < atr20 * 0.5) | (atr1 < atr50 * 0.5)
    ) | (
        ((c < h.shift(1)) & (c > l.shift(1)))
        & ((atr1 < atr5 * 0.75) | (atr1 < atr20 * 0.75) | (atr1 < atr50 * 0.75))
    )
    final_contract = (atr_contract & (v < av50 * 0.6)) if cfg.use_vdu_filter else atr_contract

    natr50 = 100.0 * atr50 / c
    pgo50 = (c - sma50) / atr50.replace(0, np.nan)
    pgo20 = (c - sma20) / atr20.replace(0, np.nan)
    rsi7 = rsi(c, 7)

    cc = (
        advol_cc
        & no_sma20_under50_20
        & ~((c < sma50) & (sma50 < sma50.shift(20)))
        & above_major
        & (natr50 > cfg.natr_thresh_cc)
        & (c > sma50 - atr20)
        & final_contract
        & ((pgo50 < 2.5) | (pgo20 < 2.5))
        & (rsi7.shift(1) < 60)
    )

    # ── Setup Candle (체크리스트 A~E) ────────────────────
    # A. 위치: 20SMA 위 + 구조 유지
    cond_a = (c > sma20) & no_sma20_under50_20

    # B. 직전 흐름: CC가 최근 ccWindow봉 이내에 선행
    bs_cc = barssince(cc)
    cond_b = bs_cc <= cfg.cc_window

    # C. 트리거: 직전 N봉 고가(컨제션 존 상단)를 종가로 통과
    zone_high = h.rolling(cfg.zone_len, min_periods=cfg.zone_len).max().shift(1)
    cond_c = c > zone_high

    # D. 캔들 퀄리티: 수요 우위 + 종가가 상단부 마감
    candle_range = h - l
    close_pos = np.where(candle_range > 0, (c - l) / candle_range.replace(0, np.nan), 1.0)
    close_pos = pd.Series(close_pos, index=d.index).fillna(1.0)
    positive_candle = (c > o) | (gap_up & (c > c.shift(1)))
    cond_d = positive_candle & (close_pos >= cfg.close_pos_min)

    # E. 리스크라인: 스탑폭이 ATR20 대비 과하지 않을 것
    cond_e = (c - l) <= atr20 * cfg.max_stop_atr

    setup = cond_a & cond_b & cond_c & cond_d & cond_e

    # 거래량은 '조건'이 아니라 '등급' (필독 글 원칙)
    vol_confirmed = v > av50 * cfg.vol_confirm_mult
    setup_best = setup & vol_confirmed

    out = pd.DataFrame({
        "wuc": wuc.fillna(False),
        "cc": cc.fillna(False),
        "setup": setup.fillna(False),
        "setup_best": setup_best.fillna(False),
        "condA": cond_a.fillna(False),
        "condB": cond_b.fillna(False),
        "condC": cond_c.fillna(False),
        "condD": cond_d.fillna(False),
        "condE": cond_e.fillna(False),
        "vol_confirmed": vol_confirmed.fillna(False),
        "bars_since_cc": bs_cc,
        "zone_high": zone_high,
        "close_pos": close_pos,
        "atr20": atr20,
        "sma20": sma20,
        "natr50": natr50,
        "rsi7": rsi7,
    }, index=d.index)
    return out


def latest_signal(df: pd.DataFrame, cfg: PBOConfig | None = None,
                  lookback: int = 20) -> dict:
    """최신 봉 기준 요약 + 최근 lookback봉 내 신호 발생 이력.

    Setup이 떴을 때의 진입/스탑/1R은 Pine 6절과 동일하게
    entry=셋업봉 종가, stop=셋업봉 저가, t1r=entry+(entry-stop).
    """
    r = compute_pbo(df, cfg)
    if r.empty:
        return {}

    last = r.iloc[-1]
    c, lo = float(df["Close"].iloc[-1]), float(df["Low"].iloc[-1])

    def recent(col):
        s = r[col].tail(lookback)
        hits = [i for i, val in enumerate(s.to_numpy()) if bool(val)]
        return (len(s) - 1 - hits[-1]) if hits else None  # 몇 봉 전인지

    out = {
        "setup_today": bool(last["setup"]),
        "setup_best_today": bool(last["setup_best"]),
        "wuc_today": bool(last["wuc"]),
        "cc_today": bool(last["cc"]),
        "cond": {k: bool(last[f"cond{k}"]) for k in "ABCDE"},
        "vol_confirmed": bool(last["vol_confirmed"]),
        "bars_since_cc": (None if pd.isna(last["bars_since_cc"])
                          else int(last["bars_since_cc"])),
        "zone_high": (None if pd.isna(last["zone_high"]) else float(last["zone_high"])),
        "close_pos": float(last["close_pos"]),
        "last_setup_bars_ago": recent("setup"),
        "last_wuc_bars_ago": recent("wuc"),
        "last_cc_bars_ago": recent("cc"),
    }
    if out["setup_today"]:
        out["entry"], out["stop"] = c, lo
        out["t1r"] = c + (c - lo)
        out["grade"] = "BEST" if out["setup_best_today"] else "GOOD"
    return out
