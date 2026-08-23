# -*- coding: utf-8 -*-
"""
개별 종목 분석 — 차트 + 데런 프레임워크 지표 카드
================================================================================
텔레그램에 티커를 입력하면 Cloudflare Worker가 이 워크플로우를 실행시킨다.
일봉 차트 이미지와 함께, 데런 프레임워크에서 "객관적으로 계산 가능한" 지표를
정리한 카드를 보낸다.

【설계 원칙 — 중요】
계산 로직은 전부 darren_core.py 를 그대로 재사용한다. 여기서 필터나 지표를
다시 구현하지 않는다. 스크리너와 분석 결과가 조용히 어긋나는 것을 막기 위함
(이 프로젝트에서 실제로 정렬 로직을 두 벌 만들었다가 순서가 깨진 적이 있음).

【계산하는 것】
  · 7가지 스캔 조건 통과 여부 (apply_filters — 스크리너와 동일)
  · 정배열, 20SMA 이격(ATR 배수), NATR(50), 거래대금, 거래대금 레벨업
  · 압축도(atr5/atr20), 거래량 마름(VDU), RS(63일, 벤치마크 대비), 52주 고점 근접도
  · 티어(A/B/C) — 단, 단일 종목이라 RS 백분위는 유니버스가 필요하므로
    최근 스캔 CSV에 그 티커가 있으면 거기서 가져오고, 없으면 백분위 미산출로 표시
  · 섹터 컨플루언스 2층: (a) 섹터 ETF 상태  (b) 최근 스캔의 동일 섹터 통과 수
  · 지수 상태(참고용): SPY/QQQ/IWM 또는 KOSPI/KOSDAQ의 20·50SMA 대비 + NATR

【계산하지 않는 것 — 결과 카드에 명시해서 보낸다】
  · 셋업 캔들 판정 (DPBO_Sys Pine 소스를 이식하면 추가 가능 — 현재 미구현)
  · 베이스 퀄리티(굿/보통), 어닝 임박 여부
  · 데런 공식 시장 신호등 (재량 판단이라 기계화 불가 — 별도 확인 필요)

입력(환경변수):
  TICKER          : 종목 티커 (US: NVDA / KR: 005930 또는 005930.KS)
  MARKET          : "US" 또는 "KR"
  DARREN_TG_TOKEN : 텔레그램 봇 토큰
  DARREN_TG_CHAT  : 텔레그램 chat_id
"""
import os
import sys
import io
import glob
import datetime as dt

import numpy as np
import pandas as pd
import yfinance as yf
import mplfinance as mpf
import requests

import darren_core as dc


TICKER_RAW = os.environ.get("TICKER", "").strip().upper()
MARKET = os.environ.get("MARKET", "US").strip().upper()
TG_TOKEN = os.environ.get("DARREN_TG_TOKEN", "")
TG_CHAT = os.environ.get("DARREN_TG_CHAT", "")

CFG = dc.CFG_US_STOCK if MARKET == "US" else dc.CFG_KR_STOCK

# 지수 상태(참고용)에 쓰는 심볼
INDEX_SYMBOLS = {
    "US": [("SPY", "S&P500"), ("QQQ", "나스닥100"), ("IWM", "러셀2000")],
    "KR": [("^KS11", "코스피"), ("^KQ11", "코스닥")],
}

# yfinance sector → 미국 섹터 ETF (섹터 자체가 도는지 확인용)
SECTOR_ETF = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Communication Services": "XLC",
}

FILTER_LABEL = {
    "0.MIN_PRICE": "최소주가",
    "1.advol": "거래대금",
    "2.정배열21": "정배열(21봉)",
    "3.NATR": "변동성 NATR",
    "4.하락추세": "하락추세 배제",
    "5.깊은붕괴": "깊은붕괴 배제",
    "6.장기추세": "장기추세",
    "7.이중붕괴16": "이중붕괴(16봉)",
}
ALL_FILTERS = ["1.advol", "2.정배열21", "3.NATR", "4.하락추세",
               "5.깊은붕괴", "6.장기추세", "7.이중붕괴16"]


# ══════════════════════════════════════════════════════════════
# 텔레그램
# ══════════════════════════════════════════════════════════════

def tg_text(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[전송 생략]\n" + text)
        return True
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"전송 실패: {e}")
        return False


def tg_photo(png, caption):
    if not TG_TOKEN or not TG_CHAT:
        print("[사진 전송 생략]\n" + caption)
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT, "caption": caption[:1024]},
            files={"photo": ("chart.png", png, "image/png")}, timeout=30)
        if r.status_code != 200:
            print(f"사진 전송 실패({r.status_code}): {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"사진 전송 실패: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# 데이터
# ══════════════════════════════════════════════════════════════

def download(symbol, period="2y"):
    """야후에서 일봉을 받아 정규화. 실패 시 None."""
    try:
        df = yf.download(symbol, period=period, interval="1d",
                         progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df is None or df.empty:
            return None
        return dc.normalize_ohlcv(df)
    except Exception as e:
        print(f"{symbol} 다운로드 실패: {e}")
        return None


def resolve_symbol():
    """KR은 .KS → .KQ 순서로 실재하는 심볼을 찾는다."""
    if MARKET != "KR":
        return TICKER_RAW, download(TICKER_RAW)
    if TICKER_RAW.endswith((".KS", ".KQ")):
        return TICKER_RAW, download(TICKER_RAW)
    code = TICKER_RAW.zfill(6) if TICKER_RAW.isdigit() else TICKER_RAW
    for suf in (".KS", ".KQ"):
        sym = f"{code}{suf}"
        df = download(sym)
        if df is not None and len(df) >= 60:
            return sym, df
    return None, None


def get_sector(symbol):
    try:
        info = yf.Ticker(symbol).info
        return info.get("sector"), info.get("shortName") or info.get("longName")
    except Exception:
        return None, None


# ══════════════════════════════════════════════════════════════
# 최근 스캔 결과에서 맥락 가져오기
# ══════════════════════════════════════════════════════════════

def load_last_scan():
    """가장 최근 스캔 CSV와 그 날짜. RS 백분위/티어/섹터 통과수의 출처.

    주의: 이 데이터는 지난 주말 스캔 시점의 값이다. 오늘 실시간으로 계산한
    지표와 시점이 다르므로, 출력할 때 반드시 날짜를 같이 보여준다.
    """
    files = sorted(glob.glob(f"darren_{MARKET.lower()}_watchlist_*.csv"))
    if not files:
        return None, None
    path = files[-1]
    # 파일명 darren_us_watchlist_20260822.csv 에서 날짜 추출
    stamp = os.path.basename(path).replace(".csv", "").split("_")[-1]
    try:
        date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) == 8 else stamp
    except Exception:
        date = stamp
    try:
        return pd.read_csv(path, encoding="utf-8-sig"), date
    except Exception as e:
        print(f"스캔 CSV 읽기 실패: {e}")
        return None, None


def find_col(df, cands):
    for c in cands:
        if c in df.columns:
            return c
    return None


def scan_context(scan_df, symbol):
    """최근 스캔에서 이 종목의 티어/RS백분위 + 같은 섹터 통과 종목 수."""
    out = {"in_scan": False, "tier": None, "rs_pct": None,
           "sector": None, "sector_peers": None, "scan_total": None}
    if scan_df is None or scan_df.empty:
        return out

    tcol = find_col(scan_df, ["티커", "ticker", "Ticker", "종목코드"])
    if not tcol:
        return out
    out["scan_total"] = len(scan_df)

    code = symbol.replace(".KS", "").replace(".KQ", "")
    norm = scan_df[tcol].astype(str).str.upper().str.replace(r"\.(KS|KQ)$", "", regex=True)
    if MARKET == "KR":
        norm = norm.str.replace(r"\.0$", "", regex=True).str.zfill(6)
    hit = scan_df[norm == code]
    if hit.empty:
        return out

    out["in_scan"] = True
    row = hit.iloc[0]
    for k, cands in (("tier", ["tier", "티어"]), ("rs_pct", ["rs_pct"])):
        c = find_col(scan_df, cands)
        if c and pd.notna(row.get(c)):
            out[k] = row[c]

    scol = find_col(scan_df, ["sector", "섹터", "업종"])
    if scol and pd.notna(row.get(scol)):
        out["sector"] = str(row[scol])
        out["sector_peers"] = int((scan_df[scol].astype(str) == out["sector"]).sum())
    return out


# ══════════════════════════════════════════════════════════════
# 지수 상태 (참고용)
# ══════════════════════════════════════════════════════════════

def trend_line(symbol, label):
    df = download(symbol, period="1y")
    if df is None or len(df) < 60:
        return f"  {label}: 조회 실패"
    close = df["Close"]
    px = dc._last(close)
    s20, s50 = dc._last(dc.sma(close, 20)), dc._last(dc.sma(close, 50))
    n50 = dc._last(dc.natr(df, 50, CFG.atr_method))
    m20 = "위" if px > s20 else "아래"
    m50 = "위" if px > s50 else "아래"
    icon = "🟢" if (px > s20 and px > s50) else ("🔴" if (px < s20 and px < s50) else "🟡")
    return f"  {icon} {label}: 20SMA {m20} · 50SMA {m50} · NATR {n50:.1f}%"


# ══════════════════════════════════════════════════════════════
# 차트
# ══════════════════════════════════════════════════════════════

def make_chart(df, symbol):
    d = df.tail(140).copy()
    d["SMA20"] = dc.sma(df["Close"], 20).tail(140)
    d["SMA50"] = dc.sma(df["Close"], 50).tail(140)

    aps = [
        mpf.make_addplot(d["SMA20"], color="#2196F3", width=1.2),
        mpf.make_addplot(d["SMA50"], color="#FF9800", width=1.2),
    ]
    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350",
                               edge="inherit", wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="yahoo", marketcolors=mc,
                               gridstyle=":", gridcolor="#e0e0e0")
    buf = io.BytesIO()
    # 제목은 영문으로 — GitHub Actions 러너에 한글 폰트가 없어서
    # 한글을 넣으면 이미지에서 네모(두부)로 깨진다. 한글 설명은 캡션(텍스트)에서 처리.
    mpf.plot(d, type="candle", style=style, addplot=aps, volume=True,
             title=f"{symbol}  Daily  {dt.date.today():%Y-%m-%d}",
             figsize=(11, 7), panel_ratios=(3, 1), tight_layout=True,
             savefig=dict(fname=buf, dpi=150, bbox_inches="tight"))
    buf.seek(0)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════

def fmt(v, spec=".2f", dash="—"):
    return dash if v is None or (isinstance(v, float) and np.isnan(v)) else format(v, spec)


def main():
    if not TICKER_RAW:
        tg_text("⚠️ 티커가 비어 있습니다.")
        sys.exit(1)

    symbol, df = resolve_symbol()
    if symbol is None or df is None or len(df) < 60:
        tg_text(f"⚠️ '{TICKER_RAW}' 데이터를 찾지 못했습니다.\n"
                f"티커를 확인해주세요. (KR은 6자리 코드, 최소 60거래일 필요)")
        sys.exit(1)

    # ── 1) 7개 필터 (스크리너와 완전히 동일한 로직) ──
    fr = dc.apply_filters(df, CFG)
    failed = set(fr.failed_names)

    # ── 2) 랭킹 지표 ──
    bench_df = download(CFG.benchmark, period="1y")
    bench_ret = dc.benchmark_ret63(bench_df) if bench_df is not None else 0.0
    bench_ok = bench_df is not None
    m = dc.compute_metrics(df, CFG, bench_ret63=bench_ret)

    # ── 3) 최근 스캔 맥락 ──
    scan_df, scan_date = load_last_scan()
    ctx = scan_context(scan_df, symbol)

    # ── 4) 섹터 ──
    sector, name = get_sector(symbol)
    sector = sector or ctx.get("sector")

    # ── 차트 먼저 전송 ──
    try:
        tg_photo(make_chart(df, symbol),
                 f"📊 {symbol}" + (f" · {name}" if name else ""))
    except Exception as e:
        print(f"차트 생성 실패(분석은 계속): {e}")

    # ── 분석 카드 ──
    L = []
    L.append(f"🔎 {symbol} 분석" + (f" · {name}" if name else ""))
    L.append(f"({dt.date.today():%Y-%m-%d} · {MARKET})")
    L.append("")

    # 스캔 조건
    L.append(f"【스캔 7조건】 {'✅ 전부 통과' if fr.passed else f'❌ {len(failed)}개 실패'}")
    for f in ALL_FILTERS:
        L.append(f"  {'✅' if f not in failed else '❌'} {FILTER_LABEL[f]}")
    if "0.MIN_PRICE" in failed:
        L.append(f"  ❌ {FILTER_LABEL['0.MIN_PRICE']}")
    L.append("")

    # 가격 · 추세
    d = fr.detail
    px, s20, s50 = d.get("close"), d.get("sma20"), d.get("sma50")
    gap20 = (px / s20 - 1) * 100 if s20 else float("nan")
    L.append("【가격 · 추세】")
    L.append(f"  종가 {fmt(px, ',.2f')}")
    L.append(f"  20SMA {fmt(s20, ',.2f')} ({gap20:+.1f}%)")
    L.append(f"  50SMA {fmt(s50, ',.2f')}")
    L.append(f"  20SMA 이격 {fmt(m.get('ext'))} ATR "
             f"(스위트스팟 {dc.EXT_SWEET_SPOT}, 확장 기준 {CFG.tier_b_ext_min}↑)")
    L.append(f"  52주 고점 대비 {fmt((m.get('near_high') or float('nan')) * 100, '.1f')}%")
    L.append("")

    # 압축 · 거래량
    L.append("【압축 · 거래량】")
    L.append(f"  압축도(atr5/atr20) {fmt(m.get('contraction'))} "
             f"(Tier A 기준 {CFG.tier_a_contraction_max} 이하)")
    L.append(f"  거래량 마름 VDU {fmt(m.get('vdu'))} (1.0 미만이면 마름)")
    L.append(f"  거래대금 레벨업 {fmt(m.get('liq_ratio'))} "
             f"(Tier A 기준 {CFG.tier_a_liq_min} 이상)")
    L.append(f"  advol20 {fmt(m.get('advol20'), ',.0f')}{CFG.unit_label} · "
             f"advol60 {fmt(m.get('advol60'), ',.0f')}{CFG.unit_label}")
    L.append(f"  NATR(50) {fmt(d.get('natr50'))}%")
    L.append("")

    # 상대강도
    L.append("【상대강도】")
    if bench_ok:
        L.append(f"  63일 수익률 {fmt((m.get('ret63') or float('nan')) * 100, '+.1f')}%")
        L.append(f"  벤치마크({CFG.benchmark}) {bench_ret * 100:+.1f}%")
        L.append(f"  RS {fmt((m.get('rs') or float('nan')) * 100, '+.1f')}%p")
    else:
        L.append(f"  ⚠️ 벤치마크({CFG.benchmark}) 조회 실패 — RS 판정 무효")
    if ctx["rs_pct"] is not None:
        L.append(f"  RS 백분위 {ctx['rs_pct']} (스캔 {scan_date} 기준)")
    else:
        L.append("  RS 백분위 — (유니버스 필요, 최근 스캔에 없는 종목)")
    L.append("")

    # 섹터 컨플루언스
    L.append("【섹터 컨플루언스】")
    L.append(f"  섹터: {sector or '—'}")
    etf = SECTOR_ETF.get(sector) if MARKET == "US" else None
    if etf:
        L.append(trend_line(etf, f"섹터 ETF {etf}"))
    elif MARKET == "KR":
        L.append("  섹터 ETF: KR은 매핑이 불명확해 생략")
    if ctx["sector_peers"]:
        L.append(f"  같은 섹터 스캔 통과 {ctx['sector_peers']}종목"
                 f" / 전체 {ctx['scan_total']} (스캔 {scan_date})")
        if ctx["sector_peers"] >= 5:
            L.append("  🔥 동일 섹터 다수 통과 — 컨플루언스 확인 가치 있음")
    else:
        L.append("  같은 섹터 통과 수 — (최근 스캔 CSV 없음)")
    L.append("")

    # 티어
    L.append("【티어】")
    if ctx["in_scan"] and ctx["tier"]:
        L.append(f"  {ctx['tier']} (스캔 {scan_date} 기준)")
        L.append("  ※ 스캔 시점 값입니다. 위 실시간 지표와 다를 수 있습니다.")
    else:
        L.append("  — 티어는 유니버스 백분위가 필요해 단일 조회로는 산출 불가")
        L.append("     (주말 스캔 통과 종목이면 그때 티어가 표시됩니다)")
    L.append("")

    # 지수 상태
    L.append("【지수 상태 · 참고용】")
    for sym, lbl in INDEX_SYMBOLS.get(MARKET, []):
        L.append(trend_line(sym, lbl))
    L.append("  ※ 데런 공식 시장 신호등이 아닙니다. 신호등은 리더십 확장/팔로스루/")
    L.append("     브레이크아웃 실패율 등 재량 판단이라 기계화하지 않았습니다.")
    L.append("")

    # 자동 판정하지 않는 항목
    L.append("【봇이 판정하지 않는 것】")
    L.append("  · 셋업 캔들 — DPBO_Sys Pine 소스 이식 시 추가 가능 (현재 미구현)")
    L.append("  · 베이스 퀄리티, 어닝 임박, 시장 신호등")
    L.append("  · 위 지표는 후보 압축용이며 매수 신호가 아닙니다.")
    L.append("    진입 판단은 차트를 직접 보고 하세요.")

    text = "\n".join(L)
    # 텔레그램 4096자 제한 대비 분할
    if len(text) > 3900:
        cut = text[:3900].rsplit("\n", 1)[0]
        tg_text(cut)
        tg_text(text[len(cut):].lstrip("\n"))
    else:
        tg_text(text)

    print(f"{symbol} 분석 전송 완료 (통과={fr.passed}, 실패={sorted(failed)})")


if __name__ == "__main__":
    main()
