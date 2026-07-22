#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
주식서포터 데이터 갱신 스크립트
- 종목_야후티커_매핑.csv 의 132개 yahoo_ticker에 대해 야후 파이낸스에서
  최근 종가 + RSI(14, Wilder's smoothing)를 계산
- 주식서포터.html 안의 SEED_STOCKS 배열을 최신 값으로 교체

사용법:
  ./venv/bin/python3 update_stock_data.py                     # 가격/RSI/차트 등 갱신 (5분 주기 크론용, 펀더멘털은 캐시 유지)
  ./venv/bin/python3 update_stock_data.py --full-fundamentals # 위 내용 + 전체 종목 펀더멘털(PER/PEG/EPS 등) 전체 갱신 (하루 1회 크론용)

(같은 폴더의 venv 사용 권장: yfinance 설치되어 있음)
"""

import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "종목_야후티커_매핑.csv"
HTML_PATH = BASE_DIR / "주식서포터.html"
RSI_PERIOD = 14
FEAR_GREED_URL = "https://feargreedmeter.com"
BACKTEST_YEARS = 20
HIST_FETCH_PERIOD = "1y"  # 200일 이동평균 계산을 위해 1년치 수집 (아래 SR_LOOKBACK_DAYS로 기존 6개월 기준 계산은 그대로 유지)
HIST_POINTS = 60  # 차트에 저장할 최근 거래일 수
SR_LOOKBACK_DAYS = 126  # 지지선/저항선 계산에 사용할 거래일 수 (기존 6개월 조회 기간과 동일하게 유지)


def round_price(val, market):
    return round(val) if market == "KR" else round(val, 2)


def load_mapping():
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("yahoo_ticker"):
                continue
            rows.append(row)
    return rows


def load_existing_seed(html_text):
    """기존 HTML에서 SEED_STOCKS 배열을 파싱해 fallback 값으로 사용"""
    m = re.search(r"var SEED_STOCKS = (\[.*\]);", html_text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return {item["ticker"]: item for item in data}


def load_existing_fear_greed(html_text):
    """기존 HTML에서 FEAR_GREED_DATA 를 파싱해 fallback 값으로 사용"""
    m = re.search(r"var FEAR_GREED_DATA = (\{.*?\});", html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def fetch_fear_greed(fallback):
    """feargreedmeter.com 페이지에 서버사이드 렌더링된 JSON-LD에서 주식시장 피어앤그리드 지수를 가져옴"""
    try:
        res = requests.get(FEAR_GREED_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        m = re.search(
            r'"dateModified":"([^"]+)","item":\{"@type":"QuantitativeValue","name":"Stock Market Fear and Greed Index","value":(\d+),"unitText":"([^"]+)"\}',
            res.text,
        )
        if not m:
            raise ValueError("JSON-LD 데이터를 찾지 못함")
        timestamp, value, label = m.group(1), m.group(2), m.group(3)
        return {
            "value": int(value),
            "label": label,
            "timestamp": timestamp,
        }
    except Exception as e:
        print(f"  ⚠️ 피어앤그리드 지수 조회 실패, 이전 값 유지: {e}")
        return fallback


def load_existing_technical_data(html_text):
    """기존 HTML에서 TECHNICAL_DATA(차트/지지·저항선) 를 파싱해 fallback 값으로 사용"""
    m = re.search(r"var TECHNICAL_DATA = (\{.*?\});", html_text)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def calc_support_resistance(closes, current_price, num_levels=2, window=3, tol_pct=0.02):
    """단순 피벗 포인트 클러스터링으로 지지선/저항선 산출
    - window 크기만큼 좌우보다 높거나(고점)/낮은(저점) 지점을 피벗으로 추출
    - 인접한 피벗끼리 tol_pct 이내면 하나의 구간으로 묶어 평균값 사용
    - 현재가 기준 위/아래에서 각각 가까운 순으로 num_levels개 선택
    """
    vals = closes.tolist()
    n = len(vals)
    pivot_highs, pivot_lows = [], []
    for i in range(window, n - window):
        seg = vals[i - window:i + window + 1]
        if vals[i] == max(seg):
            pivot_highs.append(vals[i])
        if vals[i] == min(seg):
            pivot_lows.append(vals[i])

    def cluster(levels):
        if not levels:
            return []
        levels = sorted(levels)
        clusters = [[levels[0]]]
        for v in levels[1:]:
            if abs(v - clusters[-1][-1]) / clusters[-1][-1] <= tol_pct:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [(sum(c) / len(c), len(c)) for c in clusters]

    high_clusters = cluster(pivot_highs)
    low_clusters = cluster(pivot_lows)

    res_candidates = sorted([c for c in high_clusters if c[0] > current_price], key=lambda c: c[0] - current_price)
    if not res_candidates:
        res_candidates = sorted(high_clusters, key=lambda c: -c[1])
    resistance = sorted(c[0] for c in res_candidates[:num_levels])

    sup_candidates = sorted([c for c in low_clusters if c[0] < current_price], key=lambda c: current_price - c[0])
    if not sup_candidates:
        sup_candidates = sorted(low_clusters, key=lambda c: -c[1])
    support = sorted((c[0] for c in sup_candidates[:num_levels]), reverse=True)

    return support, resistance


def calc_channel(closes):
    """선형회귀 추세선 기준 가격채널(상승채널/하락채널/횡보) 산출
    - 최근 구간 종가에 선형회귀선을 적합시키고, 추세선 대비 최대/최소 잔차만큼
      위·아래로 평행 이동한 두 개의 선을 채널 상단/하단으로 사용
    """
    vals = closes.tolist()
    n = len(vals)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(vals) / n
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((xs[i] - mean_x) * (vals[i] - mean_y) for i in range(n)) / den if den else 0
    intercept = mean_y - slope * mean_x

    residuals = [vals[i] - (slope * xs[i] + intercept) for i in range(n)]
    upper_offset = max(residuals)
    lower_offset = min(residuals)

    upper = [intercept + upper_offset, slope * (n - 1) + intercept + upper_offset]
    lower = [intercept + lower_offset, slope * (n - 1) + intercept + lower_offset]

    slope_pct = (slope * n) / mean_y if mean_y else 0  # 구간 전체 기울기를 평균가 대비 비율로
    if slope_pct > 0.03:
        direction = "up"
    elif slope_pct < -0.03:
        direction = "down"
    else:
        direction = "flat"

    return {"upper": upper, "lower": lower, "direction": direction}


def load_existing_historical_returns(html_text):
    """기존 HTML에서 HISTORICAL_RETURNS 를 파싱해 fallback 값으로 사용"""
    m = re.search(r"var HISTORICAL_RETURNS = (\{.*?\});", html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def fetch_historical_return(ticker, fallback_key, fallback):
    """지난 BACKTEST_YEARS 년간의 연평균 성장률(CAGR)과 일간 변동성을 계산"""
    try:
        hist = yf.Ticker(ticker).history(period="max", interval="1d", auto_adjust=True)
        cutoff = hist.index.max() - pd.DateOffset(years=BACKTEST_YEARS)
        hist = hist[hist.index >= cutoff]
        closes = hist["Close"].dropna()
        if len(closes) < 252:
            raise ValueError("데이터 부족")
        years = (closes.index[-1] - closes.index[0]).days / 365.25
        cagr = (closes.iloc[-1] / closes.iloc[0]) ** (1 / years) - 1
        daily_vol = float(closes.pct_change().dropna().std())
        return {
            "cagr": round(float(cagr), 4),
            "vol": round(daily_vol, 5),
            "years": round(years, 1),
            "asOf": str(closes.index[-1].date()),
        }
    except Exception as e:
        print(f"  ⚠️ '{ticker}' 과거 수익률 조회 실패, 이전 값 유지: {e}")
        return fallback.get(fallback_key) if fallback else None


def load_existing_exchange_rate(html_text):
    """기존 HTML에서 EXCHANGE_RATE 를 파싱해 fallback 값으로 사용"""
    m = re.search(r"var EXCHANGE_RATE = (\{.*?\});", html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def fetch_exchange_rate(fallback):
    """야후 파이낸스에서 원/달러 환율(1달러 = ?원)을 가져옴"""
    try:
        hist = yf.Ticker("KRW=X").history(period="5d", interval="1d", auto_adjust=True)
        closes = hist["Close"].dropna()
        if closes.empty:
            raise ValueError("환율 데이터 없음")
        return {"usdKrw": round(float(closes.iloc[-1]), 2), "asOf": str(closes.index[-1].date())}
    except Exception as e:
        print(f"  ⚠️ 원/달러 환율 조회 실패, 이전 값 유지: {e}")
        return fallback


def load_existing_market_summary(html_text):
    """기존 HTML에서 MARKET_SUMMARY 를 파싱해 fallback 값으로 사용"""
    m = re.search(r"var MARKET_SUMMARY = (\{.*?\});", html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _fetch_price_change(ticker):
    """최근 종가와 전일 대비 변동률(%)을 반환"""
    hist = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True)
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        raise ValueError(f"{ticker} 데이터 부족")
    price = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    change_pct = (price / prev - 1) * 100 if prev else 0.0
    return round(price, 2), round(change_pct, 2)


def _fetch_hist_closes(ticker, days=30):
    """최근 N거래일 종가 배열을 반환 (차트용)"""
    hist = yf.Ticker(ticker).history(period="2mo", interval="1d", auto_adjust=True)
    closes = hist["Close"].dropna().tail(days)
    return [round(float(v), 2) for v in closes.tolist()]


def fetch_market_summary(fallback):
    """VIX·유가·원달러 환율·주요지수(S&P500/나스닥/코스피/니케이)·미국 시장금리를 조회"""
    result = {"asOf": str(date.today())}
    specs = [
        ("vix", "^VIX"),
        ("oil", "CL=F"),
        ("usdKrw", "KRW=X"),
    ]
    for key, ticker in specs:
        try:
            price, change_pct = _fetch_price_change(ticker)
            try:
                hist = _fetch_hist_closes(ticker)
            except Exception:
                hist = fallback.get(key, {}).get("hist") if fallback else None
            result[key] = {"price": price, "changePct": change_pct, "hist": hist}
        except Exception as e:
            print(f"  ⚠️ '{ticker}' 조회 실패, 이전 값 유지: {e}")
            if fallback and fallback.get(key):
                result[key] = fallback[key]

    index_specs = [
        ("sp500", "^GSPC"),
        ("nasdaq", "^IXIC"),
        ("kospi", "^KS11"),
        ("nikkei", "^N225"),
    ]
    indices = {}
    for key, ticker in index_specs:
        try:
            price, change_pct = _fetch_price_change(ticker)
            try:
                hist = _fetch_hist_closes(ticker)
            except Exception:
                hist = fallback.get("indices", {}).get(key, {}).get("hist") if fallback else None
            indices[key] = {"price": price, "changePct": change_pct, "hist": hist}
        except Exception as e:
            print(f"  ⚠️ '{ticker}' 조회 실패, 이전 값 유지: {e}")
            if fallback and fallback.get("indices", {}).get(key):
                indices[key] = fallback["indices"][key]
    result["indices"] = indices

    try:
        tnx_closes = yf.Ticker("^TNX").history(period="2mo", interval="1d", auto_adjust=True)["Close"].dropna()
        y10 = float(tnx_closes.iloc[-1])
        y10_prev = float(tnx_closes.iloc[-2]) if len(tnx_closes) >= 2 else y10
        change_pct = round((y10 - y10_prev) / y10_prev * 100, 2) if y10_prev else None
        hist = [round(float(v), 2) for v in tnx_closes.tail(30).tolist()]
        result["usRate"] = {"value": round(y10, 2), "label": "10년물 국채", "changePct": change_pct, "hist": hist}
    except Exception as e:
        print(f"  ⚠️ 미국 시장금리 조회 실패, 이전 값 유지: {e}")
        if fallback and fallback.get("usRate"):
            result["usRate"] = fallback["usRate"]

    return result


ETF_NAME_MARKERS = ("ETF", "Trust", "TIGER", "KODEX", "ACE ", "PLUS ", "iShares", "SPDR", "1Q ", "SOL")
ETF_TOP_N = 7


def is_likely_etf(name):
    return any(marker in name for marker in ETF_NAME_MARKERS)


def load_existing_etf_holdings(html_text):
    """기존 HTML에서 ETF_HOLDINGS 를 파싱해 fallback 값으로 사용"""
    m = re.search(r"var ETF_HOLDINGS = (\{.*?\});", html_text)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


NAVER_ETF_URL = "https://finance.naver.com/item/main.naver?code={code}"
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"


def fetch_naver_realtime(kr_code):
    """네이버금융 실시간 시세 API에서 국내 종목의 현재가/등락률을 가져옴.
    야후 파이낸스는 국내(KRX) 종목의 장중 데이터가 지연되거나 전일 종가로
    멈춰있는 경우가 많아, 국내 종목은 이 값으로 가격/등락률을 덮어쓴다."""
    try:
        res = requests.get(
            NAVER_REALTIME_URL.format(code=kr_code),
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        res.raise_for_status()
        d = res.json()["datas"][0]
        price = float(d["closePriceRaw"])
        pct = abs(float(d["fluctuationsRatioRaw"]))
        dir_code = d["compareToPreviousPrice"]["code"]
        if dir_code in ("4", "5"):
            pct = -pct
        elif dir_code == "3":
            pct = 0.0
        return {"price": price, "changePct": pct}
    except Exception as e:
        print(f"  ⚠️ 네이버 실시간 시세({kr_code}) 조회 실패: {e}")
        return None


def fetch_all_naver_realtime(mapping_rows):
    """매핑 파일의 국내(KR) 종목 전체에 대해 네이버 실시간 시세를 조회
    (original_ticker는 종목명(한글)인 경우가 많아, yahoo_ticker에서 6자리 코드를 뽑아 조회한다)"""
    result = {}
    for row in mapping_rows:
        if row["market"].strip() != "KR":
            continue
        yahoo_ticker = row["yahoo_ticker"].strip()
        if yahoo_ticker.startswith("^"):
            continue  # 지수(코스피/코스닥)는 개별종목 시세 API 대상이 아니므로 제외
        code = re.sub(r"\.(KS|KQ)$", "", yahoo_ticker)
        q = fetch_naver_realtime(code)
        if q is not None:
            result[row["original_ticker"].strip()] = q
    return result


NAVER_WORLDSTOCK_SEARCH_URL = "https://ac.stock.naver.com/ac?q={query}&target=stock"
NAVER_WORLDSTOCK_URL = "https://api.stock.naver.com/stock/{code}/basic"


def fetch_naver_worldstock_quote(yahoo_ticker):
    """네이버금융 해외증시 검색 API로 정확한 종목 코드(reutersCode)를 찾은 뒤
    실시간 가격/등락률을 가져옴. 표시용 데이터로 쓰지는 않고, 야후 파이낸스 데이터와
    교차검증(더블체크)해 티커 오류/상장폐지 등으로 데이터가 크게 어긋나는 경우를
    조기에 잡아내는 용도로만 사용한다."""
    try:
        res = requests.get(
            NAVER_WORLDSTOCK_SEARCH_URL.format(query=yahoo_ticker),
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        res.raise_for_status()
        items = res.json().get("items", [])
        match = next(
            (it for it in items if it.get("code") == yahoo_ticker and it.get("nationCode") == "USA"),
            None,
        )
        if match is None:
            return None
        reuters_code = match["reutersCode"]
    except Exception:
        return None

    try:
        res = requests.get(
            NAVER_WORLDSTOCK_URL.format(code=reuters_code),
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        res.raise_for_status()
        d = res.json()
        close_price = d.get("closePrice")
        fluct_ratio = d.get("fluctuationsRatio")
        if not close_price or fluct_ratio is None:
            return None
        price = float(str(close_price).replace(",", ""))
        pct = float(fluct_ratio)
        if d.get("compareToPreviousPrice", {}).get("code") in ("4", "5"):
            pct = -abs(pct)
        return {"price": price, "changePct": pct}
    except Exception:
        return None


def fetch_all_naver_worldstock_quotes(mapping_rows):
    """매핑 파일의 미국(US) 종목 전체에 대해 네이버 해외증시 실시간 시세를 조회
    (야후 데이터와의 더블체크용)"""
    us_tickers = [
        r["yahoo_ticker"].strip()
        for r in mapping_rows
        if r["market"].strip() == "US" and r["yahoo_ticker"].strip()
    ]
    result = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_ticker = {
            executor.submit(fetch_naver_worldstock_quote, t): t for t in us_tickers
        }
        for future in as_completed(future_to_ticker):
            t = future_to_ticker[future]
            q = future.result()
            if q is not None:
                result[t] = q
    return result


def double_check_us_prices(mapping_rows, seed_stocks, naver_world_quotes, threshold_pct=3.0):
    """미국 종목의 야후 데이터를 네이버 해외증시 시세와 비교해 가격 차이가 크면 경고 로그를 남김.
    표시 데이터는 그대로 야후 기준을 사용하고, 이상 감지(티커 오류 등) 용도로만 사용한다."""
    mismatches = []
    for row, stock in zip(mapping_rows, seed_stocks):
        if row["market"].strip() != "US":
            continue
        yahoo_ticker = row["yahoo_ticker"].strip()
        naver_q = naver_world_quotes.get(yahoo_ticker)
        if naver_q is None or not stock.get("price"):
            continue
        diff_pct = abs(stock["price"] - naver_q["price"]) / naver_q["price"] * 100
        if diff_pct > threshold_pct:
            mismatches.append((stock["ticker"], yahoo_ticker, stock["price"], naver_q["price"], diff_pct))
    if mismatches:
        print(f"  🚨 네이버 더블체크 불일치 {len(mismatches)}건 발견 (티커 오류/상장폐지 등 확인 필요):")
        for ticker, yahoo_ticker, yahoo_price, naver_price, diff_pct in mismatches:
            print(f"     - '{ticker}'({yahoo_ticker}) 야후={yahoo_price} vs 네이버={naver_price} (차이 {diff_pct:.1f}%)")
    else:
        print(f"  ✅ 네이버 더블체크: {len(naver_world_quotes)}개 미국 종목 야후 데이터와 큰 차이 없음")
    return mismatches


def load_existing_fundamentals(html_text):
    """기존 HTML에서 FUNDAMENTALS_DATA(PER/PEG/EPS/실적일/섹터) 를 파싱해 fallback 값으로 사용"""
    m = re.search(r"var FUNDAMENTALS_DATA = (\{.*?\});", html_text)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


NO_FUNDAMENTALS_PREFIXES = ("^", "BTC-", "ETH-")  # 지수/암호화폐는 PER 등의 개념이 없음


def _earnings_timing(earnings_ts):
    """실적발표 시각(UTC epoch)을 미국 동부시간 기준 장전(BMO)/장후(AMC)로 근사 판정.
    거래소 정규장(09:30~16:00 ET) 이전이면 장전, 이후면 장후, 그 사이는 시간 미정으로 표기."""
    if not earnings_ts:
        return None
    try:
        import zoneinfo
        dt_et = datetime.fromtimestamp(earnings_ts, tz=zoneinfo.ZoneInfo("America/New_York"))
        minutes = dt_et.hour * 60 + dt_et.minute
        if minutes < 9 * 60 + 30:
            return "BMO"
        if minutes >= 16 * 60:
            return "AMC"
        return "UNKNOWN"
    except Exception:
        return None


def fetch_one_fundamentals(yahoo_ticker):
    """개별 종목의 PER/PEG/포워드PER/EPS/다음 실적발표일/섹터/배당/애널리스트 컨센서스/
    부채비율/FCF/시가총액 을 야후 파이낸스에서 조회 (ETF/지수/암호화폐 등 해당 데이터가 없는 종목은 None 반환)"""
    try:
        info = yf.Ticker(yahoo_ticker).get_info()
    except Exception:
        return None
    per = info.get("trailingPE")
    peg = info.get("trailingPegRatio")
    if peg is None:
        peg = info.get("pegRatio")
    forward_per = info.get("forwardPE")
    eps = info.get("trailingEps")
    sector = info.get("sector")
    earnings_ts = info.get("earningsTimestampStart") or info.get("earningsTimestamp")
    next_earnings = None
    if earnings_ts:
        try:
            next_earnings = datetime.fromtimestamp(earnings_ts, tz=timezone.utc).date().isoformat()
        except Exception:
            next_earnings = None
    earnings_timing = _earnings_timing(earnings_ts)

    dividend_yield = info.get("dividendYield")  # 야후에서 이미 % 단위 숫자로 제공 (예: 2.6 = 2.6%)
    ex_dividend_ts = info.get("exDividendDate")
    ex_dividend_date = None
    if ex_dividend_ts:
        try:
            ex_dividend_date = datetime.fromtimestamp(ex_dividend_ts, tz=timezone.utc).date().isoformat()
        except Exception:
            ex_dividend_date = None

    target_mean = info.get("targetMeanPrice")
    target_high = info.get("targetHighPrice")
    target_low = info.get("targetLowPrice")
    num_analysts = info.get("numberOfAnalystOpinions")
    recommendation = info.get("recommendationKey")
    debt_to_equity = info.get("debtToEquity")
    free_cashflow = info.get("freeCashflow")
    market_cap = info.get("marketCap")

    if per is None and peg is None and eps is None and sector is None:
        return None
    return {
        "per": round(per, 2) if isinstance(per, (int, float)) else None,
        "peg": round(peg, 2) if isinstance(peg, (int, float)) else None,
        "forwardPer": round(forward_per, 2) if isinstance(forward_per, (int, float)) else None,
        "eps": round(eps, 2) if isinstance(eps, (int, float)) else None,
        "sector": sector,
        "nextEarnings": next_earnings,
        "earningsTiming": earnings_timing,
        "dividendYield": round(dividend_yield, 2) if isinstance(dividend_yield, (int, float)) else None,
        "exDividendDate": ex_dividend_date,
        "targetMeanPrice": round(target_mean, 2) if isinstance(target_mean, (int, float)) else None,
        "targetHighPrice": round(target_high, 2) if isinstance(target_high, (int, float)) else None,
        "targetLowPrice": round(target_low, 2) if isinstance(target_low, (int, float)) else None,
        "numberOfAnalystOpinions": num_analysts if isinstance(num_analysts, int) else None,
        "recommendationKey": recommendation if recommendation and recommendation != "none" else None,
        "debtToEquity": round(debt_to_equity, 1) if isinstance(debt_to_equity, (int, float)) else None,
        "freeCashflow": int(free_cashflow) if isinstance(free_cashflow, (int, float)) else None,
        "marketCap": int(market_cap) if isinstance(market_cap, (int, float)) else None,
    }


def fetch_fundamentals(mapping_rows, fallback, full=False):
    """PER/PEG/EPS/다음 실적발표일/섹터 데이터를 조회.
    실시간성이 필요 없는 데이터라, 하루 한 번 전용 스케줄(--full-fundamentals)에서만
    전체 종목을 훑고, 5분마다 도는 일반 실행에서는 아예 조회하지 않는다
    (캐시된 값을 그대로 유지해 야후 rate-limit 부담과 실행 시간을 줄인다)."""
    if not full:
        return dict(fallback)

    today = date.today().isoformat()
    result = dict(fallback)

    candidates = []
    for row in mapping_rows:
        yahoo_ticker = row["yahoo_ticker"].strip()
        original_ticker = row["original_ticker"].strip()
        if not yahoo_ticker or yahoo_ticker.startswith(NO_FUNDAMENTALS_PREFIXES):
            continue
        cached = fallback.get(original_ticker)
        as_of = cached.get("asOf") if cached else None
        if as_of == today:
            continue  # 오늘 이미 갱신됨
        candidates.append((original_ticker, yahoo_ticker, as_of))

    # 한 번도 조회 안 된 종목 최우선, 그다음 오래된 순으로 조회 (--full-fundamentals 실행에서는 전부 조회)
    candidates.sort(key=lambda c: (c[2] is not None, c[2] or ""))
    to_fetch = candidates
    if not to_fetch:
        return result

    print(f"펀더멘털(PER/PEG/EPS/실적일/섹터) 데이터 조회 중... ({len(to_fetch)}개 티커)")
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_ticker = {
            executor.submit(fetch_one_fundamentals, yahoo_ticker): (original_ticker, yahoo_ticker)
            for original_ticker, yahoo_ticker, _ in to_fetch
        }
        for future in as_completed(future_to_ticker):
            original_ticker, yahoo_ticker = future_to_ticker[future]
            try:
                data = future.result()
            except Exception as e:
                data = None
                print(f"  ⚠️ '{original_ticker}'({yahoo_ticker}) 펀더멘털 조회 실패: {e}")
            if data is not None:
                data["asOf"] = today
                result[original_ticker] = data
    return result


# 국내지수를 그대로 추종해 구성종목 비중이 사실상 동일한 것으로 볼 수 있는 경우,
# 야후 파이낸스에 데이터가 있는 해외 ETF로 대체(근사)한다.
# 133690/418660(나스닥100+레버리지), 381170/465610(빅테크TOP+레버리지)은
# 프런트엔드의 EXPOSURE_MERGE_GROUPS(주식서포터.html)에서 베타 가중 합산 노출로
# 별도 처리하므로 여기서는 매핑하지 않는다.
BENCHMARK_PROXY_MAP = {}


def fetch_naver_etf_holdings(kr_code):
    """네이버금융에서 국내 상장 ETF의 구성종목(비중 포함)을 가져옴.
    국내 실물자산을 직접 보유하는 ETF(예: KODEX 200)만 비중이 공개되며,
    해외지수를 추종하는 피더형/스왑형 ETF는 네이버에서도 비중이 제공되지 않아 빈 리스트를 반환한다."""
    try:
        res = requests.get(
            NAVER_ETF_URL.format(code=kr_code),
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        res.raise_for_status()
        holdings = []
        for block in re.findall(r"<tr>(.*?)</tr>", res.text, re.S):
            m_name = re.search(r'<a href="/item/main\.naver\?code=\d{6}">([^<]+)</a>', block)
            m_pct = re.search(r'class="per">\s*([\d.]+)%', block)
            if m_name and m_pct:
                name = m_name.group(1).strip()
                holdings.append({"symbol": name, "name": name, "weight": round(float(m_pct.group(1)) / 100, 4)})
        holdings.sort(key=lambda h: -h["weight"])
        return holdings[:ETF_TOP_N]
    except Exception as e:
        print(f"  ⚠️ 네이버 ETF({kr_code}) 구성종목 조회 실패: {e}")
        return []


def fetch_etf_holdings(mapping_rows, fallback):
    """ETF로 추정되는 종목의 구성종목 상위 ETF_TOP_N개(비중 포함)를 가져옴
    1) 야후 파이낸스 시도 (해외 상장 ETF 위주로 데이터 있음)
    2) 실패 시 국내 ETF는 네이버금융 시도 (실물 보유 ETF만 비중 공개됨)
    3) 그래도 없으면 같은 지수를 추종하는 해외 ETF로 근사(BENCHMARK_PROXY_MAP)
    4) 그래도 없으면 이전 값 유지"""
    result = {}
    for row in mapping_rows:
        original_ticker = row["original_ticker"].strip()
        yahoo_ticker = row["yahoo_ticker"].strip()
        name = row["name"].strip()
        market = row["market"].strip()
        if not is_likely_etf(name):
            continue
        try:
            top = yf.Ticker(yahoo_ticker).funds_data.top_holdings
            if top is None or top.empty:
                raise ValueError("구성종목 데이터 없음")
            top = top.head(ETF_TOP_N)
            holdings = [
                {"symbol": str(sym), "name": str(r["Name"]), "weight": round(float(r["Holding Percent"]), 4)}
                for sym, r in top.iterrows()
            ]
            result[original_ticker] = holdings
            print(f"  📦 '{original_ticker}' ETF 구성종목 TOP{len(holdings)} 조회 완료 (야후)")
            continue
        except Exception as e:
            yahoo_error = e

        if market == "KR":
            naver_holdings = fetch_naver_etf_holdings(original_ticker)
            if naver_holdings:
                result[original_ticker] = naver_holdings
                print(f"  📦 '{original_ticker}' ETF 구성종목 TOP{len(naver_holdings)} 조회 완료 (네이버)")
                continue

        proxy_ticker = BENCHMARK_PROXY_MAP.get(original_ticker)
        if proxy_ticker:
            try:
                top = yf.Ticker(proxy_ticker).funds_data.top_holdings.head(ETF_TOP_N)
                holdings = [
                    {"symbol": str(sym), "name": str(r["Name"]), "weight": round(float(r["Holding Percent"]), 4)}
                    for sym, r in top.iterrows()
                ]
                result[original_ticker] = holdings
                print(f"  📦 '{original_ticker}' ETF 구성종목 TOP{len(holdings)} 조회 완료 ('{proxy_ticker}' 추종지수 근사)")
                continue
            except Exception:
                pass

        if original_ticker in fallback:
            result[original_ticker] = fallback[original_ticker]
        print(f"  ⚠️ '{original_ticker}' ETF 구성종목 조회 실패, 이전 값 유지: {yahoo_error}")
    return result


def calc_rsi(close_series, period=RSI_PERIOD):
    """Wilder's smoothed RSI (RSI.py 기존 방식과 동일)"""
    delta = close_series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    roll_up = up.ewm(com=period - 1, adjust=False).mean()
    roll_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = roll_up / roll_down
    rsi = 100.0 - (100.0 / (1.0 + rs))
    val = rsi.dropna()
    if val.empty:
        return None
    return float(val.iloc[-1])


def calc_ma_cross(closes):
    """5/20일(초단기), 20/60일(단중기), 50/200일(장기, 전통적 골든/데드크로스) 3단계
    이동평균과 크로스 상태를 계산.
    signal: 현재 단기MA가 장기MA 위(golden)/아래(dead)인지
    crossedRecently: 최근 10거래일 안에 실제 교차(부호 반전)가 발생했는지 여부"""
    tiers = [("short", 5, 20), ("mid", 20, 60), ("long", 50, 200)]
    result = {}
    for key, short_p, long_p in tiers:
        if len(closes) < long_p + 2:
            result[key] = None
            continue
        ma_short = closes.rolling(short_p).mean()
        ma_long = closes.rolling(long_p).mean()
        diff = (ma_short - ma_long).dropna()
        if len(diff) < 2:
            result[key] = None
            continue
        signal = "golden" if diff.iloc[-1] > 0 else ("dead" if diff.iloc[-1] < 0 else "flat")
        recent = diff.tail(10)
        signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in recent]
        nonzero = [s for s in signs if s != 0]
        crossed_recently = len(nonzero) >= 2 and nonzero[0] != nonzero[-1]
        result[key] = {
            "shortMa": float(ma_short.iloc[-1]),
            "longMa": float(ma_long.iloc[-1]),
            "signal": signal,
            "crossedRecently": crossed_recently,
        }
    return result


def build_ticker_result(closes, extended_price=None):
    """closes: 최근 1년치 일봉 종가(200일 이동평균 계산용). extended_price가 주어지면(프리/애프터마켓 실시간가)
    이를 현재가로 쓰고, 아직 정규장 당일 봉이 생성되기 전(프리마켓)인지 여부를 날짜 비교로
    판단해 등락률 기준(전일 종가)을 올바르게 잡는다."""
    is_today = False
    if extended_price is not None and len(closes) >= 1:
        try:
            last_date = closes.index[-1]
            now_local = pd.Timestamp.now(tz=last_date.tz) if last_date.tz is not None else pd.Timestamp.now()
            is_today = last_date.date() == now_local.date()
        except Exception:
            is_today = False

    if extended_price is not None and len(closes) >= (2 if is_today else 1):
        price = extended_price
        prev_close = float(closes.iloc[-2]) if is_today else float(closes.iloc[-1])
    else:
        price = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else None
    change_pct = ((price - prev_close) / prev_close * 100) if prev_close else None
    rsi = calc_rsi(closes)
    sr_closes = closes.tail(SR_LOOKBACK_DAYS)
    support, resistance = calc_support_resistance(sr_closes, price)
    hist_closes = closes.tail(HIST_POINTS)
    hist = [float(v) for v in hist_closes]
    channel = calc_channel(hist_closes)
    ma_cross = calc_ma_cross(closes)
    return {
        "price": price,
        "changePct": change_pct,
        "rsi": rsi,
        "hist": hist,
        "support": support,
        "resistance": resistance,
        "channel": channel,
        "maCross": ma_cross,
    }


def fetch_extended_prices(yahoo_tickers, max_attempts=3):
    """1분봉 + prepost=True 로 프리마켓/애프터마켓을 포함한 가장 최근 실시간 체결가를 가져옴.
    (일봉 데이터는 prepost 옵션이 적용되지 않아 정규장 시간외 가격 변동이 반영되지 않는다)
    대량 티커를 한 번에 스레드로 조회하면 야후 쪽 rate-limit으로 일부 티커가 간헐적으로
    누락되는 경우가 있어(TypeError 등), 누락된 티커만 모아 최대 max_attempts번 재시도한다."""
    result = {}
    remaining = list(yahoo_tickers)
    for attempt in range(max_attempts):
        if not remaining:
            break
        try:
            data = yf.download(
                remaining, period="1d", interval="1m",
                group_by="ticker", threads=True, progress=False,
                auto_adjust=True, prepost=True,
            )
        except Exception as e:
            print(f"  ⚠️ 프리/애프터마켓 실시간가 조회 실패(시도 {attempt + 1}/{max_attempts}): {e}")
            continue
        newly_failed = []
        for t in remaining:
            try:
                closes = (data["Close"] if len(remaining) == 1 else data[t]["Close"]).dropna()
                if not closes.empty:
                    result[t] = float(closes.iloc[-1])
                else:
                    newly_failed.append(t)
            except Exception:
                newly_failed.append(t)
        remaining = newly_failed
    if remaining:
        print(f"  ⚠️ 프리/애프터마켓 실시간가 조회 실패({len(remaining)}개, 일봉 종가로 대체): {', '.join(remaining)}")
    return result


def fetch_all(yahoo_tickers):
    """배치로 다운로드 (실패 종목은 개별 재시도)"""
    print(f"야후 파이낸스에서 {len(yahoo_tickers)}개 티커 데이터 수집 중...")
    data = yf.download(
        yahoo_tickers, period=HIST_FETCH_PERIOD, interval="1d",
        group_by="ticker", threads=True, progress=False,
        auto_adjust=True,
    )
    print("프리마켓/애프터마켓 실시간 가격 조회 중...")
    extended_prices = fetch_extended_prices(yahoo_tickers)

    results = {}
    for t in yahoo_tickers:
        try:
            if len(yahoo_tickers) == 1:
                closes = data["Close"].dropna()
            else:
                closes = data[t]["Close"].dropna()
            if closes.empty:
                results[t] = None
                continue
            results[t] = build_ticker_result(closes, extended_prices.get(t))
        except Exception:
            results[t] = None

    # 실패한 것들 개별 재시도
    failed = [t for t, v in results.items() if v is None]
    for t in failed:
        try:
            hist = yf.Ticker(t).history(period=HIST_FETCH_PERIOD, interval="1d", auto_adjust=True)
            closes = hist["Close"].dropna()
            if closes.empty:
                print(f"  ⚠️ '{t}' 데이터 없음")
                continue
            results[t] = build_ticker_result(closes, extended_prices.get(t))
        except Exception as e:
            print(f"  ⚠️ '{t}' 조회 실패: {e}")
    return results


def build_seed_stocks(mapping_rows, fetched, fallback_map, naver_quotes=None):
    naver_quotes = naver_quotes or {}
    seed = []
    for i, row in enumerate(mapping_rows, start=1):
        yahoo_ticker = row["yahoo_ticker"].strip()
        original_ticker = row["original_ticker"].strip()
        name = row["name"].strip()
        market = row["market"].strip()
        theme = row["theme"].strip()
        fair_price = float(row["fairPrice"]) if row["fairPrice"].strip() else None
        target_price = float(row["targetPrice"]) if row["targetPrice"].strip() else None

        fetched_val = fetched.get(yahoo_ticker)
        fallback = fallback_map.get(original_ticker)

        if fetched_val is not None and fetched_val.get("rsi") is not None:
            price = fetched_val["price"]
            rsi = fetched_val["rsi"]
            change_pct = fetched_val.get("changePct")
        elif fallback is not None:
            print(f"  ⚠️ '{original_ticker}'({yahoo_ticker}) 최신 데이터 없어 이전 값 유지")
            price = fallback["price"]
            rsi = fallback["rsi"]
            change_pct = fallback.get("changePct")
        elif fair_price is not None:
            print(f"  🚨 '{original_ticker}'({yahoo_ticker}) 데이터 없음, fairPrice로 대체")
            price = fair_price
            rsi = 50
            change_pct = None
        else:
            print(f"  🚨 '{original_ticker}'({yahoo_ticker}) 데이터 없음, 가격을 알 수 없습니다")
            price = 0
            rsi = 50
            change_pct = None

        if market == "KR" and original_ticker in naver_quotes:
            price = naver_quotes[original_ticker]["price"]
            change_pct = naver_quotes[original_ticker]["changePct"]

        price = round_price(price, market)
        rsi = int(round(max(0, min(100, rsi))))
        change_pct = round(change_pct, 2) if change_pct is not None else None

        seed.append({
            "no": i,
            "ticker": original_ticker,
            "name": name,
            "market": market,
            "theme": theme,
            "targetPrice": target_price,
            "fairPrice": fair_price,
            "price": price,
            "changePct": change_pct,
            "rsi": rsi,
            "note": ""
        })
    return seed


def build_technical_data(mapping_rows, fetched, fallback):
    """종목별 차트용 최근 종가(hist)와 지지선/저항선을 정리"""
    tech = {}
    for row in mapping_rows:
        yahoo_ticker = row["yahoo_ticker"].strip()
        original_ticker = row["original_ticker"].strip()
        market = row["market"].strip()
        fetched_val = fetched.get(yahoo_ticker)
        if fetched_val is not None and fetched_val.get("hist"):
            channel = fetched_val["channel"]
            ma_cross = fetched_val.get("maCross") or {}
            ma_out = {}
            for key, tier in ma_cross.items():
                if tier is None:
                    ma_out[key] = None
                else:
                    ma_out[key] = {
                        "shortMa": round_price(tier["shortMa"], market),
                        "longMa": round_price(tier["longMa"], market),
                        "signal": tier["signal"],
                        "crossedRecently": tier["crossedRecently"],
                    }
            tech[original_ticker] = {
                "hist": [round_price(v, market) for v in fetched_val["hist"]],
                "support": [round_price(v, market) for v in fetched_val["support"]],
                "resistance": [round_price(v, market) for v in fetched_val["resistance"]],
                "channel": {
                    "upper": [round_price(v, market) for v in channel["upper"]],
                    "lower": [round_price(v, market) for v in channel["lower"]],
                    "direction": channel["direction"],
                },
                "ma": ma_out,
            }
        elif original_ticker in fallback:
            tech[original_ticker] = fallback[original_ticker]
    return tech


def main():
    full_fundamentals = "--full-fundamentals" in sys.argv

    mapping_rows = load_mapping()
    yahoo_tickers = [r["yahoo_ticker"].strip() for r in mapping_rows if r["yahoo_ticker"].strip()]

    html_text = HTML_PATH.read_text(encoding="utf-8")
    fallback_map = load_existing_seed(html_text)
    fear_greed_fallback = load_existing_fear_greed(html_text)
    historical_fallback = load_existing_historical_returns(html_text)
    technical_fallback = load_existing_technical_data(html_text)
    exchange_rate_fallback = load_existing_exchange_rate(html_text)
    market_summary_fallback = load_existing_market_summary(html_text)
    etf_holdings_fallback = load_existing_etf_holdings(html_text)
    fundamentals_fallback = load_existing_fundamentals(html_text)

    fetched = fetch_all(yahoo_tickers)
    print("네이버금융 국내 종목 실시간 시세 조회 중...")
    naver_quotes = fetch_all_naver_realtime(mapping_rows)
    seed_stocks = build_seed_stocks(mapping_rows, fetched, fallback_map, naver_quotes)
    print("네이버금융 해외증시 실시간 시세 조회 중 (야후 데이터 더블체크용)...")
    naver_world_quotes = fetch_all_naver_worldstock_quotes(mapping_rows)
    double_check_us_prices(mapping_rows, seed_stocks, naver_world_quotes)
    technical_data = build_technical_data(mapping_rows, fetched, technical_fallback)
    fear_greed = fetch_fear_greed(fear_greed_fallback)
    exchange_rate = fetch_exchange_rate(exchange_rate_fallback)
    print("시장 요약(VIX·유가·원달러·주요지수·미국시장금리) 조회 중...")
    market_summary = fetch_market_summary(market_summary_fallback)
    print("ETF 구성종목(TOP7) 조회 중...")
    etf_holdings = fetch_etf_holdings(mapping_rows, etf_holdings_fallback)

    fundamentals = fetch_fundamentals(mapping_rows, fundamentals_fallback, full=full_fundamentals)

    print("나스닥100(QQQ) / 금(IAU) 지난 20년 연평균 수익률 계산 중...")
    historical_returns = {
        "nasdaq": fetch_historical_return("QQQ", "nasdaq", historical_fallback),
        "gold": fetch_historical_return("IAU", "gold", historical_fallback),
    }

    seed_json = json.dumps(seed_stocks, ensure_ascii=False)
    new_line = f"var SEED_STOCKS = {seed_json};"
    new_html, n = re.subn(
        r"var SEED_STOCKS = \[.*\];",
        lambda _m: new_line,
        html_text,
        count=1,
    )
    if n == 0:
        print("🚨 HTML에서 SEED_STOCKS 를 찾지 못했습니다. 수정하지 않았습니다.")
        sys.exit(1)

    if fear_greed is not None:
        fg_json = json.dumps(fear_greed, ensure_ascii=False)
        fg_line = f"var FEAR_GREED_DATA = {fg_json};"
        new_html, fg_n = re.subn(
            r"var FEAR_GREED_DATA = \{.*?\};",
            lambda _m: fg_line,
            new_html,
            count=1,
        )
        if fg_n == 0:
            print("🚨 HTML에서 FEAR_GREED_DATA 를 찾지 못했습니다. 피어앤그리드 지수는 갱신하지 않았습니다.")

    if historical_returns["nasdaq"] is not None and historical_returns["gold"] is not None:
        hr_json = json.dumps(historical_returns, ensure_ascii=False)
        hr_line = f"var HISTORICAL_RETURNS = {hr_json};"
        new_html, hr_n = re.subn(
            r"var HISTORICAL_RETURNS = \{.*?\};",
            lambda _m: hr_line,
            new_html,
            count=1,
        )
        if hr_n == 0:
            print("🚨 HTML에서 HISTORICAL_RETURNS 를 찾지 못했습니다. 과거 수익률은 갱신하지 않았습니다.")

    if technical_data:
        td_json = json.dumps(technical_data, ensure_ascii=False)
        td_line = f"var TECHNICAL_DATA = {td_json};"
        new_html, td_n = re.subn(
            r"var TECHNICAL_DATA = \{.*?\};",
            lambda _m: td_line,
            new_html,
            count=1,
        )
        if td_n == 0:
            print("🚨 HTML에서 TECHNICAL_DATA 를 찾지 못했습니다. 차트/지지·저항선은 갱신하지 않았습니다.")

    if exchange_rate is not None:
        er_json = json.dumps(exchange_rate, ensure_ascii=False)
        er_line = f"var EXCHANGE_RATE = {er_json};"
        new_html, er_n = re.subn(
            r"var EXCHANGE_RATE = \{.*?\};",
            lambda _m: er_line,
            new_html,
            count=1,
        )
        if er_n == 0:
            print("🚨 HTML에서 EXCHANGE_RATE 를 찾지 못했습니다. 환율은 갱신하지 않았습니다.")

    if market_summary is not None:
        ms_json = json.dumps(market_summary, ensure_ascii=False)
        ms_line = f"var MARKET_SUMMARY = {ms_json};"
        new_html, ms_n = re.subn(
            r"var MARKET_SUMMARY = \{.*?\};",
            lambda _m: ms_line,
            new_html,
            count=1,
        )
        if ms_n == 0:
            print("🚨 HTML에서 MARKET_SUMMARY 를 찾지 못했습니다. 시장 요약은 갱신하지 않았습니다.")

    if etf_holdings:
        eh_json = json.dumps(etf_holdings, ensure_ascii=False)
        eh_line = f"var ETF_HOLDINGS = {eh_json};"
        new_html, eh_n = re.subn(
            r"var ETF_HOLDINGS = \{.*?\};",
            lambda _m: eh_line,
            new_html,
            count=1,
        )
        if eh_n == 0:
            print("🚨 HTML에서 ETF_HOLDINGS 를 찾지 못했습니다. ETF 구성종목은 갱신하지 않았습니다.")

    if fundamentals:
        fd_json = json.dumps(fundamentals, ensure_ascii=False)
        fd_line = f"var FUNDAMENTALS_DATA = {fd_json};"
        new_html, fd_n = re.subn(
            r"var FUNDAMENTALS_DATA = \{.*?\};",
            lambda _m: fd_line,
            new_html,
            count=1,
        )
        if fd_n == 0:
            print("🚨 HTML에서 FUNDAMENTALS_DATA 를 찾지 못했습니다. 펀더멘털 데이터는 갱신하지 않았습니다.")

    HTML_PATH.write_text(new_html, encoding="utf-8")
    ok_count = sum(1 for t in yahoo_tickers if fetched.get(t) is not None)
    print(f"\n✅ 완료: {ok_count}/{len(yahoo_tickers)}개 종목 최신 데이터 반영, {HTML_PATH} 저장됨")
    if fear_greed is not None:
        print(f"   피어앤그리드 지수: {fear_greed['value']} ({fear_greed['label']})")
    if historical_returns["nasdaq"] is not None:
        print(f"   나스닥100(QQQ) {BACKTEST_YEARS}년 CAGR: {historical_returns['nasdaq']['cagr']*100:.2f}%")
    if historical_returns["gold"] is not None:
        print(f"   금(IAU) {BACKTEST_YEARS}년 CAGR: {historical_returns['gold']['cagr']*100:.2f}%")
    print(f"   차트/지지·저항선 데이터: {len(technical_data)}개 종목")
    if exchange_rate is not None:
        print(f"   원/달러 환율: {exchange_rate['usdKrw']}원 ({exchange_rate['asOf']} 기준)")
    if market_summary is not None:
        print(f"   시장 요약: VIX {market_summary.get('vix', {}).get('price')}, "
              f"미국시장금리(10년물) {market_summary.get('usRate', {}).get('value')}%")
    print(f"   ETF 구성종목 데이터: {len(etf_holdings)}개 ETF")
    kr_count = sum(1 for r in mapping_rows if r["market"].strip() == "KR")
    print(f"   네이버 실시간 시세: {len(naver_quotes)}/{kr_count}개 국내 종목")
    us_count = sum(1 for r in mapping_rows if r["market"].strip() == "US")
    print(f"   네이버 해외증시 더블체크: {len(naver_world_quotes)}/{us_count}개 미국 종목")
    fundamentals_count = sum(1 for r in mapping_rows if not r["yahoo_ticker"].strip().startswith(NO_FUNDAMENTALS_PREFIXES))
    fundamentals_note = "" if full_fundamentals else " (캐시 유지, --full-fundamentals 실행 시에만 갱신)"
    print(f"   펀더멘털(PER/PEG/EPS/실적일/섹터) 데이터: {len(fundamentals)}/{fundamentals_count}개 종목{fundamentals_note}")


if __name__ == "__main__":
    main()
