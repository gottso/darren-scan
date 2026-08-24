# -*- coding: utf-8 -*-
"""
종목명 → 티커 해석
================================================================================
"삼성전자", "Nvidia" 처럼 이름으로 입력해도 티커를 찾아준다.

2단 구조:
  1) 로컬 CSV 우선 — 저장소에 이미 있는 종목 목록을 먼저 뒤진다.
     kr_tickers.csv / us_etfs.csv / kr_etfs.csv / darren_*_watchlist_*.csv
     빠르고, 네트워크가 필요 없고, 내 유니버스 안의 종목을 정확히 집는다.
  2) 야후 검색 API 폴백 — 로컬에 없으면 Yahoo Finance search 엔드포인트를 쓴다.
     유니버스 밖 종목(예: 스캔에 안 걸린 소형주)도 찾을 수 있다.

매칭 우선순위: 완전일치 → 앞부분 일치 → 부분 포함
동점이면 로컬 CSV 등장 순서(대개 거래대금 순)를 따른다.

주의: 이름 검색은 본질적으로 애매할 수 있다. 후보가 여럿이면 1순위를 쓰되
      대안 목록을 함께 반환해서, 호출부가 사용자에게 보여줄 수 있게 한다.
"""
import os
import glob
import re

import pandas as pd

try:
    import requests
except ImportError:  # 로컬 CSV만으로도 동작하도록
    requests = None


TICKER_COLS = ["티커", "ticker", "Ticker", "종목코드", "symbol", "Symbol", "code", "단축코드"]
NAME_COLS = ["종목명", "name", "Name", "이름", "회사명", "company", "한글종목명"]

# 로컬 후보 파일 (앞쪽이 우선)
LOCAL_SOURCES = {
    "KR": ["kr_tickers.csv", "kr_etfs.csv", "darren_kr_watchlist_*.csv"],
    "US": ["us_etfs.csv", "darren_us_watchlist_*.csv"],
}

YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"

# 미국 거래소 코드 화이트리스트.
# 심볼 정규식으로 거르면 SMSN.IL(런던), NVDA.MX(멕시코) 같은 해외 상장이
# BRK.B 와 구분되지 않고 통과한다. 거래소 코드로 거르는 게 정확하다.
US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "PCX", "ASE", "BTS", "NYS", "NAS", "AMX"}
# 한국 거래소 코드 (KSC=코스피, KOE=코스닥)
KR_EXCHANGES = {"KSC", "KOE"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ══════════════════════════════════════════════════════════════
# 공통
# ══════════════════════════════════════════════════════════════

def _find_col(df, cands):
    for c in cands:
        if c in df.columns:
            return c
    return None


def norm(s):
    """비교용 정규화 — 공백/기호 제거 후 소문자."""
    return re.sub(r"[\s\-_.,'\"()&]", "", str(s)).lower()


def is_ticker_like(text, market):
    """티커 형태인지 (이름 검색을 건너뛸지 판단)."""
    t = str(text).strip().upper()
    if market == "KR":
        return bool(re.fullmatch(r"\d{4,6}(\.(KS|KQ))?", t))
    return bool(re.fullmatch(r"[A-Z]{1,5}(\.[A-Z]{1,2})?", t))


def normalize_kr_code(code):
    c = str(code).strip().upper()
    if c.endswith(".0"):
        c = c[:-2]
    core = re.sub(r"\.(KS|KQ)$", "", c)
    suf = c[len(core):]
    if core.isdigit():
        core = core.zfill(6)
    return core + suf


# ══════════════════════════════════════════════════════════════
# 1) 로컬 CSV 검색
# ══════════════════════════════════════════════════════════════

def _load_local(market):
    """로컬 CSV들을 (ticker, name, source) 목록으로 모은다."""
    rows = []
    for pattern in LOCAL_SOURCES.get(market, []):
        for path in sorted(glob.glob(pattern)):
            try:
                df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
            except Exception:
                continue
            tcol, ncol = _find_col(df, TICKER_COLS), _find_col(df, NAME_COLS)
            if not tcol or not ncol:
                continue
            for _, r in df.iterrows():
                t, n = r.get(tcol), r.get(ncol)
                if pd.isna(t) or pd.isna(n):
                    continue
                t = str(t).strip()
                if market == "KR":
                    t = normalize_kr_code(t)
                rows.append((t.upper(), str(n).strip(), os.path.basename(path)))
    return rows


def search_local(query, market):
    """로컬 CSV에서 이름으로 검색. [(ticker, name, source), ...] 반환.

    완전일치가 하나라도 있으면 완전일치만 돌려준다.
    ("삼성전자"를 찾았는데 대안으로 '삼성'이 든 ETF들이 딸려 나오면 오히려 방해)
    """
    q = norm(query)
    if not q:
        return []
    rows = _load_local(market)
    exact, starts, contains = [], [], []
    seen = set()
    for t, n, src in rows:
        if t in seen:
            continue
        nn = norm(n)
        if nn == q:
            exact.append((t, n, src)); seen.add(t)
        elif nn.startswith(q):
            starts.append((t, n, src)); seen.add(t)
        elif q in nn:
            contains.append((t, n, src)); seen.add(t)
    if exact:
        return exact
    return starts + contains


# ══════════════════════════════════════════════════════════════
# 2) 야후 검색 폴백
# ══════════════════════════════════════════════════════════════

def search_yahoo(query, market, timeout=10):
    """Yahoo Finance search API. [(symbol, name, exchange), ...] 반환."""
    if requests is None:
        return []
    try:
        res = requests.get(
            YAHOO_SEARCH,
            params={"q": query, "quotesCount": 10, "newsCount": 0,
                    "enableFuzzyQuery": "false"},
            headers={"User-Agent": UA}, timeout=timeout,
        )
        if res.status_code != 200:
            print(f"야후 검색 실패({res.status_code})")
            return []
        quotes = res.json().get("quotes", []) or []
    except Exception as e:
        print(f"야후 검색 예외: {e}")
        return []

    out = []
    for q in quotes:
        sym = str(q.get("symbol") or "").strip().upper()
        if not sym:
            continue
        qt = str(q.get("quoteType") or "").upper()
        if qt not in ("EQUITY", "ETF", ""):     # 지수/선물/암호화폐 제외
            continue
        name = q.get("shortname") or q.get("longname") or ""
        exch = str(q.get("exchange") or "")
        is_kr = sym.endswith((".KS", ".KQ"))
        if market == "KR":
            if not (is_kr or exch.upper() in KR_EXCHANGES):
                continue
        else:
            # 거래소 코드가 화이트리스트에 있어야 미국 상장으로 인정.
            # 코드가 비어 있으면 접미사 없는 심볼만 허용(보수적 폴백).
            if exch:
                if exch.upper() not in US_EXCHANGES:
                    continue
            elif "." in sym:
                continue
        out.append((sym, str(name).strip(), exch))
    return out


# ══════════════════════════════════════════════════════════════
# 통합 진입점
# ══════════════════════════════════════════════════════════════

def resolve(query, market):
    """이름 또는 티커를 받아 (symbol, display_name, alternatives, source) 반환.

    찾지 못하면 (None, None, [], None).
    alternatives 는 [(symbol, name), ...] — 사용자에게 다른 후보를 보여줄 때 사용.

    우선순위:
      1) 로컬 CSV 완전일치   — 내 유니버스 안의 종목을 정확히 집는다
      2) 야후 검색           — 유니버스 밖 종목
      3) 로컬 CSV 부분일치   — 마지막 수단

    2와 3의 순서가 중요하다. US는 주식 이름 목록이 없고 ETF 목록만 있어서,
    부분일치를 먼저 쓰면 "Apple" 이 AAPL 이 아니라 'T-Rex 2X Long Apple ETF'
    같은 파생 ETF로 잡힌다. 야후를 먼저 태워야 회사 본체가 나온다.
    """
    raw = str(query).strip()
    if not raw:
        return None, None, [], None

    # 티커 형태면 그대로 사용 (이름 검색 불필요)
    if is_ticker_like(raw, market):
        sym = raw.upper()
        if market == "KR":
            sym = normalize_kr_code(sym)
        return sym, None, [], "ticker"

    q = norm(raw)
    local = search_local(raw, market)

    # 1) 로컬 완전일치
    exact = [h for h in local if norm(h[1]) == q]
    if exact:
        best = exact[0]
        return best[0], best[1], [(t, n) for t, n, _ in exact[1:6]], f"local:{best[2]}"

    # 2) 야후 검색
    hits = search_yahoo(raw, market)
    if hits:
        best = hits[0]
        return best[0], best[1], [(s, n) for s, n, _ in hits[1:6]], "yahoo"

    # 3) 로컬 부분일치
    if local:
        best = local[0]
        return best[0], best[1], [(t, n) for t, n, _ in local[1:6]], f"local:{best[2]}"

    return None, None, [], None
