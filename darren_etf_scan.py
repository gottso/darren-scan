#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
darren_etf_scan.py
==================
US / KR ETF에 Darren 7필터 + 티어 랭킹을 적용한다.

주식 스캔과의 차이
  - NATR(50) 임계값 1.0% (주식은 2.0%). ETF는 구조적으로 변동성이 낮음
  - 인버스 계열 제외 (곱버스 포함) — 롱 온리 추세추종 전제
  - 레버리지 2x/3x 포함하되 RS를 배수로 나눠 정규화 (SOXL/TQQQ 독식 방지)
  - Tier A 압축 기준 0.85 (주식 0.75). ETF는 바스켓이라 변동성 수축이 얕음
  - 섹터 대신 ETF 카테고리로 그룹핑

[변경 이력]
  - yahoo_ok=False (영숫자 KRX 코드) 자동 제외. Yahoo가 지원하지 않는 심볼
  - 벤치마크 로드 재시도 + 폴백 티커. 이전엔 레이트리밋 시 조용히 0.0%로 넘어가
    RS가 '초과수익'이 아니라 '절대수익률'로 바뀌는 침묵 실패가 있었음
  - yfinance 로그 억제 (상장폐지 심볼 경고 스팸 제거)

사전 준비
  python make_etf_tickers.py --market us
  python make_etf_tickers.py --market kr --kr-csv "KRX에서받은.csv"

사용법
  python darren_etf_scan.py --market us
  python darren_etf_scan.py --market kr --advol 30 --sweep --min-tier C
  python darren_etf_scan.py --market us --category 반도체 크립토
  python darren_etf_scan.py --market both --csv scan --json etf_payload.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import pandas as pd
import yfinance as yf

from darren_core import (
    CFG_KR_ETF,
    CFG_US_ETF,
    ScanConfig,
    apply_filters,
    assign_tiers,
    benchmark_ret63,
    compute_metrics,
    normalize_ohlcv,
    sector_heat,
)

# 상장폐지 심볼 경고가 수백 줄씩 쏟아지므로 억제
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

BATCH_SIZE = 100
HISTORY_PERIOD = "2y"      # 200SMA + 63일 RS 계산에 충분
SLEEP_BETWEEN_BATCH = 1.0

BENCH_RETRIES = 3
BENCH_BACKOFF = 5.0
# 1차 실패 시 대체 심볼. KOSPI 지수가 막히면 KODEX 200으로 대신함
BENCH_FALLBACK = {
    "^KS11": ["069500.KS", "^KS200"],
    "SPY": ["VOO", "IVV", "^GSPC"],
}

SWEEP_CONTRACTION = [0.75, 0.80, 0.85, 0.90, 0.95, 1.00]
SWEEP_RS = [0.0, 30.0, 50.0, 70.0]
SWEEP_EXT = [1.5, 2.0, 2.5, 3.0]

GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[1m", "\033[0m"
)
TIER_COLOR = {"A": GREEN, "B": YELLOW, "C": DIM}


# ══════════════════════════════════════════════════════════════
# 유니버스
# ══════════════════════════════════════════════════════════════


def load_universe(
    market: str, datadir: str, include_inverse: bool,
    categories: list[str] | None, limit: int | None, seed: int,
) -> pd.DataFrame:
    fname = "us_etfs.csv" if market == "us" else "kr_etfs.csv"
    path = os.path.join(datadir, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} 없음. 먼저 make_etf_tickers.py 로 생성하세요.")

    df = pd.read_csv(path, dtype={"ticker": str, "name": str, "category": str})
    df["leverage"] = pd.to_numeric(df["leverage"], errors="coerce").fillna(1.0)
    df["inverse"] = df["inverse"].astype(str).str.lower().isin(["true", "1", "y", "yes"])
    total = len(df)

    # Yahoo 미지원 심볼(영숫자 KRX 코드) 제외. 컬럼이 없는 구버전 CSV는 통과
    if "yahoo_ok" in df.columns:
        ok = df["yahoo_ok"].astype(str).str.lower().isin(["true", "1", "y", "yes"])
        n = int((~ok).sum())
        if n:
            print(f"{DIM}  Yahoo 미지원 심볼 {n}개 제외{RESET}")
        df = df[ok].copy()

    if not include_inverse:
        n = len(df)
        df = df[~df["inverse"]].copy()
        print(f"{DIM}  인버스 {n - len(df)}개 제외{RESET}")

    if categories:
        n = len(df)
        df = df[df["category"].isin(categories)].copy()
        print(f"{DIM}  카테고리 필터 {categories} → {len(df)}개 (제외 {n - len(df)}){RESET}")

    # limit은 반드시 '랜덤 샘플'. head()로 자르면 알파벳 앞쪽 소형 ETF만 걸려서
    # 거래대금 필터에 전멸하는 잘못된 표본이 됨
    if limit and limit < len(df):
        df = df.sample(n=limit, random_state=seed)
        print(f"{YELLOW}  랜덤 샘플 {limit}개 (seed={seed}) — 전체 아님{RESET}")

    print(f"  대상 {len(df)}개 / 원본 {total}개")
    return df.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════
# 데이터
# ══════════════════════════════════════════════════════════════


def download_batch(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    """yfinance 배치 다운로드 → {ticker: OHLCV DataFrame}."""
    out: dict[str, pd.DataFrame] = {}
    try:
        raw = yf.download(
            tickers, period=period, interval="1d", group_by="ticker",
            auto_adjust=False, progress=False, threads=True,
        )
    except Exception as e:
        print(f"{RED}  배치 실패: {e}{RESET}", file=sys.stderr)
        return out

    if raw is None or len(raw) == 0:
        return out

    if len(tickers) == 1:
        d = normalize_ohlcv(raw)
        if not d.empty:
            out[tickers[0]] = d
        return out

    for t in tickers:
        try:
            sub = raw[t]
        except (KeyError, TypeError):
            continue
        d = normalize_ohlcv(sub)
        if not d.empty:
            out[t] = d
    return out


def _try_bench(symbol: str, asof: str | None) -> float | None:
    """단일 심볼로 63일 수익률 시도. 실패하면 None."""
    try:
        b = yf.download(symbol, period=HISTORY_PERIOD, interval="1d",
                        auto_adjust=False, progress=False, threads=False)
        b = normalize_ohlcv(b)
        if asof:
            b = b.loc[:asof]
        if len(b) < 64:
            return None
        return benchmark_ret63(b)
    except Exception:
        return None


def get_benchmark_ret(cfg: ScanConfig, asof: str | None) -> tuple[float, bool]:
    """
    벤치마크 63일 수익률을 (값, 성공여부)로 반환.
    실패 시 (0.0, False) — 호출부에서 반드시 경고를 띄워야 한다.
    RS가 '벤치마크 대비 초과수익'이 아니라 '절대수익률'로 바뀌기 때문.
    """
    candidates = [cfg.benchmark] + BENCH_FALLBACK.get(cfg.benchmark, [])
    for sym in candidates:
        for attempt in range(BENCH_RETRIES):
            r = _try_bench(sym, asof)
            if r is not None:
                tag = "" if sym == cfg.benchmark else f" {YELLOW}(대체: {cfg.benchmark} 실패){RESET}"
                print(f"{DIM}  벤치마크 {sym} 3M 수익률 {r*100:+.1f}%{RESET}{tag}")
                return r, True
            if attempt < BENCH_RETRIES - 1:
                wait = BENCH_BACKOFF * (attempt + 1)
                print(f"{DIM}  벤치마크 {sym} 재시도 {attempt+1}/{BENCH_RETRIES} "
                      f"({wait:.0f}초 대기)...{RESET}")
                time.sleep(wait)
    return 0.0, False


def warn_no_benchmark(cfg: ScanConfig) -> None:
    print(f"\n{RED}{BOLD}  ⚠ 벤치마크({cfg.benchmark}) 로드 실패{RESET}")
    print(f"{RED}    RS가 '벤치마크 대비 초과수익'이 아니라 '절대수익률'로 계산됩니다.")
    print(f"    상대강도 판정과 Tier A/B의 RS 조건이 무의미해집니다.")
    print(f"    Yahoo 레이트리밋일 가능성이 높으니 몇 분 뒤 다시 실행하세요.{RESET}\n")


# ══════════════════════════════════════════════════════════════
# 스캔
# ══════════════════════════════════════════════════════════════


def scan_market(
    market: str, cfg: ScanConfig, datadir: str, asof: str | None,
    include_inverse: bool, categories: list[str] | None,
    limit: int | None, seed: int,
) -> tuple[pd.DataFrame, bool]:
    """필터 통과 종목의 원지표와 벤치마크 성공여부를 반환 (티어 부여 전)."""
    print(f"\n{BOLD}{'='*78}{RESET}")
    print(f"{BOLD}[{cfg.name}]{RESET} advol>{cfg.advol_min}{cfg.unit_label} / "
          f"NATR>{cfg.natr_min}% / asof={asof or '최신'}")
    print(f"{DIM}  Tier A 조건: 압축≤{cfg.tier_a_contraction_max} / "
          f"이격 {cfg.tier_a_ext_min}~{cfg.tier_a_ext_max} / "
          f"레벨업≥{cfg.tier_a_liq_min} / RS≥{cfg.tier_a_rs_pct_min}%{RESET}")
    print("=" * 78)

    uni = load_universe(market, datadir, include_inverse, categories, limit, seed)
    if uni.empty:
        return pd.DataFrame(), True

    bench_ret, bench_ok = get_benchmark_ret(cfg, asof)
    if not bench_ok:
        warn_no_benchmark(cfg)

    meta = uni.set_index("ticker").to_dict("index")
    tickers = uni["ticker"].tolist()

    rows: list[dict] = []
    fail_counter: dict[str, int] = {}
    only_advol = 0
    n_data = 0

    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i : i + BATCH_SIZE]
        print(f"  {i+1:>5}-{min(i+BATCH_SIZE, len(tickers)):<5} 다운로드... "
              f"(통과 {len(rows)})", end="\r")
        data = download_batch(chunk, HISTORY_PERIOD)
        n_data += len(data)

        for t, df in data.items():
            if asof:
                df = df.loc[:asof]
            if len(df) < 60:
                continue

            fr = apply_filters(df, cfg)
            if not fr.passed:
                for f in fr.failed_names:
                    fail_counter[f] = fail_counter.get(f, 0) + 1
                if fr.failed_names == ["1.advol"]:
                    only_advol += 1
                continue

            m = meta.get(t, {})
            lev = float(m.get("leverage", 1.0) or 1.0)
            met = compute_metrics(df, cfg, bench_ret63=bench_ret, leverage=lev)
            if not met:
                continue

            rows.append({
                "ticker": t,
                "name": str(m.get("name", ""))[:44],
                "category": m.get("category", "기타"),
                "leverage": lev,
                **met,
            })

        if i + BATCH_SIZE < len(tickers):
            time.sleep(SLEEP_BETWEEN_BATCH)

    print(" " * 70, end="\r")
    print(f"  데이터 확보 {n_data}/{len(tickers)}개 → 필터 통과 {GREEN}{len(rows)}{RESET}개")
    if fail_counter:
        top = sorted(fail_counter.items(), key=lambda x: -x[1])[:8]
        print(f"{DIM}  탈락 사유(중복집계): " + ", ".join(f"{k}={v}" for k, v in top) + RESET)
        print(f"{DIM}  거래대금만으로 탈락(순수 유동성 부족): {only_advol}개{RESET}")

    return pd.DataFrame(rows), bench_ok


# ══════════════════════════════════════════════════════════════
# 출력
# ══════════════════════════════════════════════════════════════


def _sweep_line(raw: pd.DataFrame, cfg: ScanConfig, field: str, value: float) -> str:
    c = ScanConfig(**dict(cfg.__dict__))
    setattr(c, field, value)
    res = assign_tiers(raw, c)
    cnt = res["tier"].value_counts()
    names = res[res["tier"] == "A"]["ticker"].head(6).tolist()
    a = cnt.get("A", 0)
    mark = GREEN if 3 <= a <= 15 else ""
    return (f"  {mark}{value:<8.2f}{a:>7}{cnt.get('B',0):>7}{cnt.get('C',0):>7}"
            f"{RESET}   {', '.join(names)}")


def print_sweep(raw: pd.DataFrame, cfg: ScanConfig) -> None:
    """티어 임계값 3종의 민감도를 각각 비교. 어느 조건이 병목인지 드러남."""
    blocks = [
        ("압축 상한 (tier_a_contraction_max)", "tier_a_contraction_max", SWEEP_CONTRACTION),
        ("RS 하한  (tier_a_rs_pct_min)",       "tier_a_rs_pct_min",      SWEEP_RS),
        ("이격 상한 (tier_a_ext_max)",          "tier_a_ext_max",         SWEEP_EXT),
    ]
    for title, field, values in blocks:
        print(f"\n{BOLD}── 스윕: {title} ──{RESET}")
        print(f"{DIM}  {'값':<8}{'TierA':>7}{'TierB':>7}{'TierC':>7}   상위 A 종목{RESET}")
        print(f"{DIM}  {'-'*70}{RESET}")
        for v in values:
            print(_sweep_line(raw, cfg, field, v))
    print(f"{DIM}\n  ※ 값을 크게 바꿔도 TierA가 거의 안 변하면 그 조건은 병목이 아닙니다.{RESET}")


def print_result(res: pd.DataFrame, min_tier: str, top: int) -> None:
    if res.empty:
        print(f"{YELLOW}  통과 종목 없음{RESET}")
        return

    order = {"A": 0, "B": 1, "C": 2}
    view = res[res["tier"].map(order) <= order[min_tier]].head(top)

    print(f"\n{BOLD}── 카테고리 히트 ──{RESET}")
    heat = sector_heat(res, "category")
    for cat, r in heat.iterrows():
        bar = "█" * min(int(r["A"]), 20)
        print(f"  {cat:<14} {r['label']:<12} {GREEN}{bar}{RESET}")

    if view.empty:
        print(f"\n{YELLOW}  Tier {min_tier} 이상 종목 없음 — --min-tier C 로 확인해보세요{RESET}")
        return

    print(f"\n{BOLD}── 종목 리스트 (Tier {min_tier} 이상, 상위 {len(view)}) ──{RESET}")
    hdr = (f"  {'TIER':<5}{'SCORE':>6}  {'TICKER':<12}{'CAT':<14}{'LEV':>4}"
           f"{'종가':>11}{'압축':>7}{'VDU':>7}{'이격':>7}{'레벨업':>7}{'RS%':>6}{'고점':>6}")
    print(f"{DIM}{hdr}{RESET}")
    print(f"{DIM}  {'-'*97}{RESET}")

    for _, r in view.iterrows():
        c = TIER_COLOR.get(r["tier"], "")
        lev = f"{r['leverage']:.0f}x" if r["leverage"] > 1 else "-"
        print(
            f"  {c}{r['tier']:<5}{RESET}{r['score']:>6.1f}  {BOLD}{r['ticker']:<12}{RESET}"
            f"{str(r['category']):<14}{lev:>4}"
            f"{r['close']:>11,.0f}{r['contraction']:>7.2f}{r['vdu']:>7.2f}"
            f"{r['ext']:>7.2f}{r['liq_ratio']:>7.2f}{r['rs_pct']:>6.0f}"
            f"{r['near_high']:>6.2f}"
        )
        if str(r["name"]):
            print(f"{DIM}        {r['name']}{RESET}")

    print(f"\n{DIM}  압축=atr5/atr20(낮을수록↑)  VDU=vol5/vol50(낮을수록↑)")
    print(f"  이격=(종가-20SMA)/atr20 (0~2가 스위트스팟)  레벨업=advol20/advol60{RESET}")


def to_payload(res: pd.DataFrame, market: str, bench_ok: bool) -> dict:
    """텔레그램 드릴다운용 페이로드. 기존 섹터 메뉴 구조와 호환."""
    base = {
        "market": market,
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "benchmark_ok": bench_ok,
    }
    if res.empty:
        return {**base, "menu": [], "categories": {}, "tickers": []}

    cats: dict[str, list] = {}
    for _, r in res.iterrows():
        cats.setdefault(str(r["category"]), []).append({
            "ticker": r["ticker"], "name": r["name"], "tier": r["tier"],
            "score": float(r["score"]), "lev": float(r["leverage"]),
        })
    heat = sector_heat(res, "category")
    return {
        **base,
        "menu": [
            {"category": c, "label": heat.loc[c, "label"], "count": int(heat.loc[c, "total"])}
            for c in heat.index
        ],
        "categories": cats,
        "tickers": res["ticker"].tolist(),
    }


# ══════════════════════════════════════════════════════════════


def main() -> int:
    p = argparse.ArgumentParser(description="Darren ETF 스캔 + 티어 랭킹")
    p.add_argument("--market", choices=["us", "kr", "both"], default="both")
    p.add_argument("--datadir", default=".", help="us_etfs.csv / kr_etfs.csv 위치")
    p.add_argument("--asof", default=None, help="기준일 YYYY-MM-DD (과거 재현)")
    p.add_argument("--advol", type=float, default=None, help="거래대금 임계값 오버라이드")
    p.add_argument("--natr", type=float, default=None, help="NATR 임계값 오버라이드")
    p.add_argument("--tier-contraction", type=float, default=None, help="Tier A 압축 상한")
    p.add_argument("--tier-ext-max", type=float, default=None, help="Tier A 이격 상한")
    p.add_argument("--tier-liq", type=float, default=None, help="Tier A 레벨업 하한")
    p.add_argument("--tier-rs", type=float, default=None, help="Tier A RS 백분위 하한")
    p.add_argument("--sweep", action="store_true", help="티어 임계값 민감도 비교")
    p.add_argument("--min-tier", choices=["A", "B", "C"], default="B")
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--category", nargs="+", default=None, help="특정 카테고리만 스캔")
    p.add_argument("--include-inverse", action="store_true", help="인버스 포함(기본 제외)")
    p.add_argument("--limit", type=int, default=None, help="랜덤 샘플 개수(테스트용)")
    p.add_argument("--seed", type=int, default=42, help="랜덤 샘플 시드")
    p.add_argument("--csv", default=None, help="결과 CSV 저장 경로 프리픽스")
    p.add_argument("--json", default=None, help="텔레그램 페이로드 JSON 저장 경로")
    args = p.parse_args()

    targets = []
    if args.market in ("us", "both"):
        targets.append(("us", CFG_US_ETF))
    if args.market in ("kr", "both"):
        targets.append(("kr", CFG_KR_ETF))

    payloads = {}
    for mkt, base_cfg in targets:
        cfg = ScanConfig(**dict(base_cfg.__dict__))
        if args.advol is not None:
            cfg.advol_min = args.advol
        if args.natr is not None:
            cfg.natr_min = args.natr
        if args.tier_contraction is not None:
            cfg.tier_a_contraction_max = args.tier_contraction
        if args.tier_ext_max is not None:
            cfg.tier_a_ext_max = args.tier_ext_max
        if args.tier_liq is not None:
            cfg.tier_a_liq_min = args.tier_liq
        if args.tier_rs is not None:
            cfg.tier_a_rs_pct_min = args.tier_rs

        try:
            raw, bench_ok = scan_market(mkt, cfg, args.datadir, args.asof,
                                        args.include_inverse, args.category,
                                        args.limit, args.seed)
        except FileNotFoundError as e:
            print(f"{RED}{e}{RESET}", file=sys.stderr)
            continue

        if raw.empty:
            print(f"{YELLOW}  통과 종목 없음{RESET}")
            payloads[mkt] = to_payload(pd.DataFrame(), mkt, bench_ok)
            continue

        if args.sweep:
            print_sweep(raw, cfg)

        res = assign_tiers(raw, cfg)
        print_result(res, args.min_tier, args.top)
        if not bench_ok:
            warn_no_benchmark(cfg)

        if args.csv:
            path = f"{args.csv}_{mkt}_etf.csv"
            res.to_csv(path, index=False, encoding="utf-8-sig")
            print(f"\n  CSV 저장 → {path}")

        payloads[mkt] = to_payload(res, mkt, bench_ok)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payloads, f, ensure_ascii=False, indent=2)
        print(f"  JSON 저장 → {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
