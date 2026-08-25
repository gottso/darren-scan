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


# 티커 컬럼 후보. yf_ticker 를 맨 앞에 두는 게 중요하다 —
# kr_tickers.csv 는 yf_ticker(005930.KS)와 code(005930)를 둘 다 갖고 있는데,
# code 를 쓰면 코스닥 종목에서 .KS 를 먼저 시도했다 실패하고 .KQ 로 재시도하게 된다.
# yf_ticker 는 접미사가 이미 정확해서 다운로드 한 번으로 끝난다.
TICKER_COLS = ["yf_ticker", "티커", "ticker", "Ticker", "종목코드",
               "symbol", "Symbol", "code", "단축코드"]
NAME_COLS = ["종목명", "name", "Name", "이름", "회사명", "company", "한글종목명"]

# 로컬 후보 파일 (앞쪽이 우선)
LOCAL_SOURCES = {
    "KR": ["kr_tickers.csv", "kr_etfs.csv", "darren_kr_watchlist_*.csv"],
    "US": ["us_etfs.csv", "darren_us_watchlist_*.csv"],
}

YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"

# 한글 발음 → 공식 표기 별칭.
# kr_tickers.csv 는 2,625종목 중 352개가 영문 표기(NAVER, POSCO홀딩스, LG전자 …)라
# 사람들이 흔히 치는 한글 발음으로는 로컬 검색이 실패한다. 야후가 받아주긴 하지만
# 로컬에서 잡히면 더 빠르고 정확하므로, 자주 쓰는 것만 매핑해 둔다.
# 값은 '앞부분 치환'에 쓰인다 ("네이버" → "NAVER", "엘지전자" → "LG전자").
KR_ALIASES = {
    "네이버": "NAVER",
    "포스코": "POSCO",
    "엘지": "LG",
    "에스케이": "SK",
    "케이티": "KT",
    "씨제이": "CJ",
    "지에스": "GS",
    "에이치디": "HD",
    "에이치디현대": "HD현대",
    "디엘": "DL",
    "디비": "DB",
    "에이치엘": "HL",
    "오씨아이": "OCI",
    "제이더블유": "JW",
    "케이비": "KB",
    "비지에프": "BGF",
    "에이치엘비": "HLB",
    "한온": "한온",
}

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


def expand_aliases(query, market):
    """검색어의 한글 발음 별칭을 공식 표기로 바꾼 변형들을 반환.

    "네이버"      → ["네이버", "NAVER"]
    "엘지전자"     → ["엘지전자", "LG전자"]
    원본을 항상 첫 번째로 둬서, 별칭 없이 맞는 경우를 우선한다.
    """
    variants = [query]
    if market != "KR":
        return variants
    q = str(query).strip()
    for ko, en in KR_ALIASES.items():
        if q.startswith(ko):
            cand = en + q[len(ko):]
            if cand not in variants:
                variants.append(cand)
    return variants


def search_local(query, market):
    """로컬 CSV에서 이름으로 검색. [(ticker, name, source), ...] 반환.

    한글 발음 별칭도 함께 시도하고(“네이버” → “NAVER”), 모든 변형의 결과를
    합산한 뒤 완전일치 → 앞부분 일치 → 부분 포함 순으로 정렬한다.

    변형별로 먼저 끝내지 않고 합산하는 게 중요하다. 예를 들어 "케이티"는
    원본으로 '케이티알파'가 앞부분 일치로 걸리지만, 별칭 "KT"로는 'KT'가
    완전일치한다. 합산해야 완전일치인 KT가 이긴다.

    완전일치가 하나라도 있으면 완전일치만 돌려준다.
    ("삼성전자"를 찾았는데 '삼성'이 든 ETF들이 대안으로 딸려오면 방해되므로)
    """
    rows = _load_local(market)
    if not rows:
        return []

    exact, starts, contains = [], [], []
    seen_exact, seen_partial = set(), set()

    for variant in expand_aliases(query, market):
        q = norm(variant)
        if not q:
            continue
        for t, n, src in rows:
            nn = norm(n)
            if nn == q:
                if t not in seen_exact:
                    exact.append((t, n, src)); seen_exact.add(t)
            elif t in seen_partial:
                continue
            elif nn.startswith(q):
                starts.append((t, n, src)); seen_partial.add(t)
            elif q in nn:
                contains.append((t, n, src)); seen_partial.add(t)

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

    local = search_local(raw, market)

    # 1) 로컬 완전일치 (별칭 변형도 완전일치로 인정)
    qs = {norm(v) for v in expand_aliases(raw, market)}
    exact = [h for h in local if norm(h[1]) in qs]
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
