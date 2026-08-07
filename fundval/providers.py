from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from .factor_fit import DEFAULT_FACTOR_UNIVERSE, FactorPoint
from .valuation import Holding, LatestNav, OfficialEstimate, Quote


STATIC_DAY_CACHE_TTL = timedelta(days=1)
PUBLISHED_NAV_CACHE_TTL = timedelta(hours=12)
TRACKING_INDEX_BY_FUND_CODE = {
    "001595": ("sz399986", "中证银行"),
    "005693": ("sz399967", "中证军工"),
    "012414": ("sz399997", "中证白酒"),
    "011103": ("sh931151", "中证光伏产业"),
}
TARGET_ETF_BY_FUND_CODE = {
    "001595": ("515290", "天弘中证银行ETF"),
    "005693": ("512680", "广发中证军工ETF"),
    "011036": ("516150", "嘉实中证稀土产业ETF"),
}
TRACKING_INDEX_BY_FUND_NAME = (
    ("中证银行", "sz399986", "中证银行"),
    ("中证军工", "sz399967", "中证军工"),
    ("中证白酒", "sz399997", "中证白酒"),
    ("中证光伏产业", "sh931151", "中证光伏产业"),
    ("光伏产业", "sh931151", "中证光伏产业"),
)
TARGET_ETF_BY_FUND_NAME = (
    ("天弘中证银行ETF联接", "515290", "天弘中证银行ETF"),
    ("广发中证军工ETF联接", "512680", "广发中证军工ETF"),
    ("嘉实中证稀土产业ETF联接", "516150", "嘉实中证稀土产业ETF"),
)
QDII_BENCHMARK_BY_FUND_CODE = {
    "539001": {
        "benchmark_symbol": "QQQ",
        "benchmark_name": "纳斯达克100/QQQ",
        "fx_symbol": "USDCNYC",
    },
}
QDII_BENCHMARK_BY_FUND_NAME = (
    ("纳斯达克100", "QQQ", "纳斯达克100/QQQ", "USDCNYC"),
    ("纳指100", "QQQ", "纳斯达克100/QQQ", "USDCNYC"),
)


class AkshareProvider:
    def __init__(self, cache_ttl_seconds: int = 300):
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._cache: dict[str, tuple[datetime, Any]] = {}
        self._cache_lock = Lock()
        self._cache_key_locks: dict[str, Lock] = {}
        self.factor_universe = list(DEFAULT_FACTOR_UNIVERSE)

    def get_fund_name(self, code: str) -> str | None:
        df = self._cached(
            "fund_names",
            self._fetch_fund_names,
            STATIC_DAY_CACHE_TTL,
            current_day_only=True,
        )
        if df is None or df.empty:
            return None
        matched = df[df["基金代码"].astype(str).str.zfill(6) == code]
        if matched.empty:
            return None
        return str(matched.iloc[0].get("基金简称") or "")

    def get_official_estimate(self, code: str) -> OfficialEstimate | None:
        for symbol in ("全部", "指数型", "ETF联接", "LOF", "场内交易基金"):
            df = self._cached(f"official:{symbol}", lambda s=symbol: self._fetch_estimates(s))
            if df is None or df.empty or "基金代码" not in df.columns:
                continue
            matched = df[df["基金代码"].astype(str).str.zfill(6) == code]
            if matched.empty:
                continue
            row = matched.iloc[0]
            nav = _parse_float(row.get("交易日-估算数据-估算值"))
            growth = _parse_float(row.get("交易日-估算数据-估算增长率"))
            if nav is None or growth is None:
                continue
            estimate_time = _extract_datetime_text(
                row,
                ("估算时间", "更新时间", "更新日期", "时间", "date", "datetime", "update_time"),
            )
            trade_date = _extract_date_text(
                row,
                ("估算日期", "交易日期", "净值日期", "日期", "date", "trade_date"),
            ) or _date_key(estimate_time)
            return OfficialEstimate(
                nav=nav,
                growth_pct=growth,
                estimate_time=estimate_time,
                trade_date=trade_date,
            )
        return None

    def get_latest_nav(self, code: str) -> LatestNav | None:
        latest = _latest_nav_from_points(self._eastmoney_nav_points(code))
        if latest is not None:
            return latest
        return None
        df = self._fund_nav_frame(code)
        if df is None or df.empty:
            return None
        row = df.iloc[-1]
        nav = _parse_float(_first_present(row, ("单位净值", "净值", "NAV")))
        if nav is None:
            return None
        return LatestNav(nav=nav, date=str(_first_present(row, ("净值日期", "日期", "date")) or ""))

    def get_nav_by_date(self, code: str, nav_date: str) -> LatestNav | None:
        target_date = str(nav_date or "").strip()[:10]
        if not target_date:
            return None
        for point in reversed(self._eastmoney_nav_points(code)):
            if point.date == target_date:
                return LatestNav(nav=point.value, date=point.date)
        df = self._fund_nav_frame(code)
        if df is None or df.empty:
            return None
        nav_columns = ("\u5355\u4f4d\u51c0\u503c", "\u51c0\u503c", "NAV")
        date_columns = ("\u51c0\u503c\u65e5\u671f", "\u65e5\u671f", "date")
        for _, row in df.iterrows():
            row_date = str(_first_present(row, date_columns) or "").strip()[:10]
            if row_date != target_date:
                continue
            nav = _parse_float(_first_present(row, nav_columns))
            if nav is None:
                return None
            return LatestNav(nav=nav, date=row_date)
        return None

    def get_nav_history(self, code: str, limit: int = 120) -> list[FactorPoint]:
        points = self._eastmoney_nav_points(code)
        if points:
            return points[-max(2, int(limit or 120)) :]
        df = self._fund_nav_frame(code)
        return _points_from_dataframe(
            df,
            date_names=("净值日期", "日期", "date"),
            value_names=("单位净值", "净值", "NAV"),
            limit=limit,
        )

    def _eastmoney_nav_points(self, code: str) -> list[FactorPoint]:
        try:
            return self._cached(
                f"eastmoney_nav_points:{code}",
                lambda current=code: self._fetch_eastmoney_nav_points(current),
                PUBLISHED_NAV_CACHE_TTL,
                current_day_only=True,
            ) or []
        except Exception:
            return []

    def _fetch_eastmoney_nav_points(self, code: str) -> list[FactorPoint]:
        return _eastmoney_nav_points_from_text(self._fetch_eastmoney_nav_text(code))

    def _fetch_eastmoney_nav_text(self, code: str) -> str:
        import requests

        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js?v={int(datetime.now().timestamp() * 1000)}"
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        response.raise_for_status()
        return response.text

    def get_holdings(self, code: str) -> list[Holding]:
        return self._cached(
            f"holdings:{code}",
            lambda current=code: self._fetch_holdings(current),
            STATIC_DAY_CACHE_TTL,
            current_day_only=True,
        ) or []

    def _fetch_holdings(self, code: str) -> list[Holding]:
        ak = self._ak()
        current_year = datetime.now().year
        try:
            latest_frame = ak.fund_portfolio_hold_em(symbol=code, date="")
            if latest_frame is not None and not latest_frame.empty:
                holdings = self._holdings_from_dataframe(latest_frame)
                if holdings:
                    return holdings
        except Exception:
            pass

        eastmoney_available, holdings = self._try_eastmoney_holdings(code)
        if eastmoney_available:
            return holdings

        for year in range(current_year, current_year - 5, -1):
            try:
                frame = ak.fund_portfolio_hold_em(symbol=code, date=str(year))
            except Exception:
                continue
            if frame is None or frame.empty:
                continue
            holdings = self._holdings_from_dataframe(frame)
            if holdings:
                return holdings
        return []

    def _fund_nav_frame(self, code: str):
        return self._cached(
            f"fund_nav:{code}",
            lambda current=code: self._ak().fund_open_fund_info_em(
                symbol=current,
                indicator="单位净值走势",
            ),
            PUBLISHED_NAV_CACHE_TTL,
            current_day_only=True,
        )

    def _holdings_from_dataframe(self, df) -> list[Holding]:
        holdings: list[Holding] = []
        for _, row in df.head(10).iterrows():
            stock_code = str(_first_present(row, ("股票代码", "代码", "证券代码")) or "").strip()
            name = str(_first_present(row, ("股票名称", "名称", "证券名称")) or stock_code)
            weight = _parse_float(_first_present(row, ("占净值比例", "持仓占比", "占比", "比例")))
            if stock_code and weight is not None:
                holdings.append(Holding(name=name, code=_normalize_stock_code(stock_code), weight_pct=weight))
        return holdings

    def _get_eastmoney_holdings(self, code: str) -> list[Holding]:
        text = self._fetch_eastmoney_holdings_html(code)
        if not text:
            return []
        content = _extract_apidata_content(text)
        try:
            from bs4 import BeautifulSoup
        except Exception:
            return []
        soup = BeautifulSoup(content, "html.parser")
        table = soup.find("table")
        if table is None:
            return []
        headers = [_compact_text(cell.get_text("", strip=True)) for cell in table.find_all("th")]
        code_index = _find_header_index(headers, "股票代码", 1)
        name_index = _find_header_index(headers, "股票名称", 2)
        weight_index = _find_header_index(headers, "占净值", 4)
        holdings: list[Holding] = []
        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) <= max(code_index, name_index, weight_index):
                continue
            stock_code = _normalize_stock_code(cells[code_index].get_text("", strip=True))
            name = cells[name_index].get_text("", strip=True)
            weight = _parse_float(cells[weight_index].get_text("", strip=True))
            if stock_code and name and weight is not None:
                holdings.append(Holding(name=name, code=stock_code, weight_pct=weight))
        return holdings[:10]

    def _try_eastmoney_holdings(self, code: str) -> tuple[bool, list[Holding]]:
        try:
            return True, self._get_eastmoney_holdings(code)
        except Exception:
            return False, []

    def _fetch_eastmoney_holdings_html(self, code: str) -> str:
        import requests

        url = (
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            f"?type=jjcc&code={code}&topline=10&year=&month=&rt={datetime.now().timestamp()}"
        )
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": f"https://fundf10.eastmoney.com/ccmx_{code}.html",
            },
            timeout=5,
        )
        response.raise_for_status()
        return response.text

    def get_quotes(self, stock_codes: list[str]) -> dict[str, Quote]:
        codes = {_normalize_stock_code(code) for code in stock_codes if code}
        if not codes:
            return {}
        result = self._get_tencent_quotes(codes)
        missing_codes = codes - set(result)
        if not missing_codes:
            return result
        ak = self._ak()
        for loader in (ak.stock_zh_a_spot_em, getattr(ak, "stock_hk_spot_em", None)):
            if loader is None:
                continue
            try:
                df = loader()
            except Exception:
                continue
            result.update(_quotes_from_dataframe(df, missing_codes))
            missing_codes = codes - set(result)
            if not missing_codes:
                break
        return result

    def get_historical_quotes(
        self,
        stock_codes: list[str],
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Quote]:
        start_key = _date_key(from_date)
        end_key = _date_key(to_date)
        codes = [_normalize_stock_code(code) for code in stock_codes if code]
        if not codes or not start_key or not end_key or start_key >= end_key:
            return {}

        def load_quote(code: str):
            return code, self._historical_quote(code, start_key, end_key)

        worker_count = min(4, len(codes))
        if worker_count <= 1:
            loaded = [load_quote(code) for code in codes]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                loaded = list(executor.map(load_quote, codes))
        return {code: quote for code, quote in loaded if quote is not None}

    def get_factor_histories(self, limit: int = 120) -> dict[str, list[FactorPoint]]:
        result: dict[str, list[FactorPoint]] = {}
        row_limit = max(2, int(limit or 120))
        factors = list(self.factor_universe)

        def load_factor(factor):
            points = self._cached(
                f"factor_history:{factor.code}",
                lambda current=factor.code: self._fetch_factor_history(current),
                STATIC_DAY_CACHE_TTL,
                current_day_only=True,
            )
            return factor.code, points[-row_limit:] if points else None

        worker_count = min(4, len(factors))
        if worker_count <= 1:
            loaded = [load_factor(factor) for factor in factors]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                loaded = list(executor.map(load_factor, factors))
        for code, points in loaded:
            if points:
                result[code] = points
        return result

    def get_factor_quotes(self, factor_codes: list[str]) -> dict[str, Quote]:
        requested = {str(code or "").strip() for code in factor_codes if str(code or "").strip()}
        if not requested:
            return {}
        names = {factor.code: factor.name for factor in self.factor_universe}
        result = self._cached(
            "factor_quotes",
            self._fetch_factor_quotes,
            timedelta(seconds=60),
        )
        quotes = {code: quote for code, quote in (result or {}).items() if code in requested}
        missing = requested - set(quotes)
        for code in missing:
            fallback = self._factor_quote_from_history(code, names.get(code, code))
            if fallback is not None:
                quotes[code] = fallback
        return quotes

    def get_tracking_index_quote(self, code: str, fund_name: str | None = None) -> Quote | None:
        target_etf = _target_etf_for_fund(code, fund_name)
        if target_etf is not None:
            etf_code, etf_name = target_etf
            quote = self._cached(
                f"target_etf_quote:{etf_code}",
                lambda current_code=etf_code, current_name=etf_name: self._fetch_target_etf_quote(
                    current_code,
                    current_name,
                ),
                timedelta(seconds=60),
            )
            if quote is not None:
                return quote

        index = _tracking_index_for_fund(code, fund_name)
        if index is None:
            return None
        index_code, index_name = index
        return self._cached(
            f"tracking_index_quote:{index_code}",
            lambda current_code=index_code, current_name=index_name: self._fetch_tracking_index_quote(
                current_code,
                current_name,
            ),
            timedelta(seconds=60),
        )

    def _fetch_tracking_index_quote(self, index_code: str, index_name: str) -> Quote | None:
        try:
            quote = _index_quote_from_dataframe(self._ak().stock_zh_index_spot_em(), index_code, index_name)
        except Exception:
            quote = None
        if quote is not None:
            return quote
        return self._factor_quote_from_history(index_code, index_name)

    def _fetch_target_etf_quote(self, etf_code: str, etf_name: str) -> Quote | None:
        quote = self._get_tencent_quotes({_normalize_stock_code(etf_code)}).get(_normalize_stock_code(etf_code))
        if quote is None:
            return None
        return Quote(
            code=_normalize_stock_code(etf_code),
            name=quote.name or etf_name,
            change_pct=quote.change_pct,
            quote_time=quote.quote_time,
            trade_date=quote.trade_date,
        )

    def get_qdii_benchmark_quote(
        self,
        code: str,
        fund_name: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict | None:
        benchmark = _qdii_benchmark_for_fund(code, fund_name)
        start_key = _date_key(from_date)
        end_key = _date_key(to_date)
        if benchmark is None or not start_key or not end_key or start_key >= end_key:
            return None

        benchmark_symbol = benchmark["benchmark_symbol"]
        fx_symbol = benchmark["fx_symbol"]
        benchmark_points = self._cached(
            f"qdii_benchmark_history:{benchmark_symbol}",
            lambda current=benchmark_symbol: self._fetch_us_symbol_history(current),
            STATIC_DAY_CACHE_TTL,
            current_day_only=True,
        ) or []
        fx_points = self._cached(
            f"fx_history:{fx_symbol}",
            lambda current=fx_symbol: self._fetch_fx_history(current),
            STATIC_DAY_CACHE_TTL,
            current_day_only=True,
        ) or []
        benchmark_start = _point_on_or_before(benchmark_points, start_key)
        benchmark_end = _point_on_or_before(benchmark_points, end_key)
        fx_start = _point_on_or_before(fx_points, start_key)
        fx_end = _point_on_or_before(fx_points, end_key)
        if benchmark_start is None or benchmark_end is None or fx_start is None or fx_end is None:
            return None
        if benchmark_start.value <= 0 or fx_start.value <= 0:
            return None
        benchmark_ratio = benchmark_end.value / benchmark_start.value
        fx_ratio = fx_end.value / fx_start.value
        return {
            "source": "qdii_benchmark",
            "benchmark_symbol": benchmark_symbol,
            "benchmark_name": benchmark["benchmark_name"],
            "fx_symbol": fx_symbol,
            "benchmark_growth_pct": round((benchmark_ratio - 1) * 100.0, 4),
            "fx_growth_pct": round((fx_ratio - 1) * 100.0, 4),
            "change_pct": round((benchmark_ratio * fx_ratio - 1) * 100.0, 4),
            "benchmark_start_date": benchmark_start.date,
            "benchmark_end_date": benchmark_end.date,
            "fx_start_date": fx_start.date,
            "fx_end_date": fx_end.date,
        }

    def _fetch_us_symbol_history(self, symbol: str) -> list[FactorPoint]:
        ak = self._ak()
        df = ak.stock_us_daily(symbol=symbol)
        return _points_from_dataframe(
            df,
            date_names=("date", "日期", "trade_date"),
            value_names=("close", "收盘", "收盘价", "最新价"),
        )

    def _fetch_hk_symbol_history(self, symbol: str) -> list[FactorPoint]:
        df = self._ak().stock_hk_daily(symbol=symbol)
        return _points_from_dataframe(
            df,
            date_names=("date", "日期", "trade_date"),
            value_names=("close", "收盘", "收盘价", "最新价"),
        )

    def _fetch_a_symbol_history(self, symbol: str) -> list[FactorPoint]:
        df = self._ak().stock_zh_a_hist(symbol=symbol, period="daily", adjust="")
        return _points_from_dataframe(
            df,
            date_names=("日期", "date", "trade_date"),
            value_names=("收盘", "close", "收盘价", "最新价"),
        )

    def _fetch_fx_history(self, symbol: str) -> list[FactorPoint]:
        df = self._ak().forex_hist_em(symbol=symbol)
        return _points_from_dataframe(
            df,
            date_names=("日期", "date", "trade_date"),
            value_names=("最新价", "close", "收盘", "收盘价"),
        )

    def _get_tencent_quotes(self, codes: set[str]) -> dict[str, Quote]:
        query = ",".join(_tencent_symbol(code) for code in sorted(codes))
        if not query:
            return {}
        try:
            text = self._fetch_tencent_quote_text(query)
        except Exception:
            return {}
        return _parse_tencent_quotes(text, codes)

    def _fetch_tencent_quote_text(self, query: str) -> str:
        import requests

        response = requests.get(
            f"https://qt.gtimg.cn/q={query}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        return response.text

    def health(self) -> dict:
        try:
            self._ak()
            return {"akshare": "available"}
        except Exception as exc:
            return {"akshare": "unavailable", "reason": str(exc)}

    def is_trading_day(self, current) -> bool:
        date_key = str(current)[:10]
        dates = self._cached(
            "trade_dates",
            self._fetch_trade_dates,
            STATIC_DAY_CACHE_TTL,
            current_day_only=True,
        )
        return date_key in dates

    def latest_trading_day(self, current, include_current: bool = True) -> str | None:
        date_key = str(current)[:10]
        dates = self._cached(
            "trade_dates",
            self._fetch_trade_dates,
            STATIC_DAY_CACHE_TTL,
            current_day_only=True,
        )
        if not dates:
            return None
        candidates = [value for value in dates if value <= date_key] if include_current else [value for value in dates if value < date_key]
        return max(candidates) if candidates else None

    def _fetch_fund_names(self):
        return self._ak().fund_name_em()

    def _fetch_trade_dates(self) -> set[str]:
        df = self._ak().tool_trade_date_hist_sina()
        if df is None or df.empty or "trade_date" not in df.columns:
            return set()
        return {str(value)[:10] for value in df["trade_date"].tolist()}

    def _fetch_estimates(self, symbol: str):
        ak = self._ak()
        for function_name in ("fund_value_estimation_em", "fund_em_value_estimation"):
            function = getattr(ak, function_name, None)
            if function is not None:
                return function(symbol=symbol)
        raise AttributeError("akshare fund value estimation API is unavailable")

    def _fetch_factor_history(self, symbol: str) -> list[FactorPoint]:
        ak = self._ak()
        df = ak.stock_zh_index_daily(symbol=symbol)
        return _points_from_dataframe(
            df,
            date_names=("date", "日期", "trade_date"),
            value_names=("close", "收盘", "收盘价"),
        )

    def _historical_quote(self, code: str, from_date: str, to_date: str) -> Quote | None:
        try:
            points = self._cached(
                f"stock_history:{code}",
                lambda current=code: self._fetch_stock_history(current),
                STATIC_DAY_CACHE_TTL,
                current_day_only=True,
            ) or []
        except Exception:
            return None
        start = _point_on_or_before(points, from_date)
        end = _point_on_or_before(points, to_date)
        if start is None or end is None or start.value <= 0 or start.date >= end.date:
            return None
        return Quote(
            code=code,
            name=code,
            change_pct=(end.value / start.value - 1) * 100.0,
            trade_date=end.date,
        )

    def _fetch_stock_history(self, code: str) -> list[FactorPoint]:
        if any(ch.isalpha() for ch in code):
            return self._fetch_us_symbol_history(code)
        if len(code) == 5:
            return self._fetch_hk_symbol_history(code)
        return self._fetch_a_symbol_history(code)

    def _fetch_factor_quotes(self) -> dict[str, Quote]:
        ak = self._ak()
        df = ak.stock_zh_index_spot_em()
        return _index_quotes_from_dataframe(df, self.factor_universe)

    def _factor_quote_from_history(self, code: str, name: str) -> Quote | None:
        points = self._cached(
            f"factor_history:{code}",
            lambda current=code: self._fetch_factor_history(current),
            STATIC_DAY_CACHE_TTL,
            current_day_only=True,
        )
        if points is None or len(points) < 2:
            return None
        previous = points[-2]
        latest = points[-1]
        if previous.value <= 0:
            return None
        return Quote(
            code=code,
            name=name,
            change_pct=(latest.value / previous.value - 1) * 100.0,
            trade_date=latest.date,
        )

    def _cached(self, key: str, loader, ttl: timedelta | None = None, current_day_only: bool = False):
        now = datetime.now()
        effective_ttl = ttl or self.cache_ttl
        with self._cache_lock:
            cached = self._cache.get(key)
            key_lock = self._cache_key_locks.get(key)
            if key_lock is None:
                key_lock = Lock()
                self._cache_key_locks[key] = key_lock
        if cached and _is_cache_fresh(cached[0], now, effective_ttl, current_day_only):
            return cached[1]
        with key_lock:
            now = datetime.now()
            with self._cache_lock:
                cached = self._cache.get(key)
            if cached and _is_cache_fresh(cached[0], now, effective_ttl, current_day_only):
                return cached[1]
            value = loader()
            with self._cache_lock:
                self._cache[key] = (now, value)
            return value

    @staticmethod
    def _ak():
        import akshare as ak

        return ak


def _parse_float(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text or text in {"-", "--", "---", "nan", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_cache_fresh(cached_at: datetime, now: datetime, ttl: timedelta, current_day_only: bool) -> bool:
    if current_day_only and cached_at.date() != now.date():
        return False
    return now - cached_at < ttl


def _first_present(row, names: tuple[str, ...]):
    for name in names:
        if name in row and row.get(name) is not None:
            return row.get(name)
    return None


def _extract_datetime_text(row, preferred_names: tuple[str, ...]) -> str | None:
    for value in _candidate_values(row, preferred_names):
        normalized = _normalize_datetime_text(value)
        if normalized:
            return normalized
    return None


def _extract_date_text(row, preferred_names: tuple[str, ...]) -> str | None:
    for value in _candidate_values(row, preferred_names):
        date_key = _date_key(value)
        if date_key:
            return date_key
    return None


def _candidate_values(row, preferred_names: tuple[str, ...]):
    seen = set()
    for name in preferred_names:
        if name in row:
            seen.add(name)
            yield row.get(name)
    for name in getattr(row, "index", []):
        if name in seen:
            continue
        compact = _compact_text(str(name)).lower()
        if any(marker in compact for marker in ("日期", "时间", "date", "time")):
            yield row.get(name)


def _normalize_datetime_text(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    compact_digits = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", text)
    if compact_digits:
        year, month, day, hour, minute, second = compact_digits.groups()
        return f"{year}-{month}-{day} {hour}:{minute}:{second}"
    match = re.search(
        r"(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})(?:[ T](?P<time>\d{1,2}:\d{2}(?::\d{2})?))?",
        text,
    )
    if not match:
        return None
    date_part = _date_key(match.group("date"))
    if not date_part:
        return None
    time_part = match.group("time")
    if not time_part:
        return date_part
    if len(time_part.split(":")) == 2:
        time_part = f"{time_part}:00"
    return f"{date_part} {time_part}"


def _date_key(value) -> str:
    text = str(value or "").strip()
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _normalize_stock_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if "." in raw:
        left, right = raw.split(".", 1)
        raw = left if any(ch.isalpha() for ch in left) else right
    token = re.sub(r"[^0-9A-Z]", "", raw)
    if any(ch.isalpha() for ch in token):
        return token
    digits = "".join(ch for ch in token if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _quotes_from_dataframe(df, target_codes: set[str]) -> dict[str, Quote]:
    if df is None or df.empty:
        return {}
    result: dict[str, Quote] = {}
    for _, row in df.iterrows():
        code = _normalize_stock_code(str(_first_present(row, ("代码", "symbol", "证券代码")) or ""))
        if code not in target_codes:
            continue
        change_pct = _parse_float(_first_present(row, ("涨跌幅", "涨跌幅%", "change_pct", "最新涨跌幅")))
        if change_pct is None:
            continue
        quote_time = _extract_datetime_text(
            row,
            ("时间", "日期", "更新时间", "最新交易日", "trade_date", "date", "time"),
        )
        trade_date = _extract_date_text(
            row,
            ("日期", "最新交易日", "交易日期", "trade_date", "date"),
        ) or _date_key(quote_time)
        result[code] = Quote(
            code=code,
            name=str(_first_present(row, ("名称", "name", "证券简称")) or code),
            change_pct=change_pct,
            quote_time=quote_time,
            trade_date=trade_date,
        )
    return result


def _tracking_index_for_fund(code: str, fund_name: str | None) -> tuple[str, str] | None:
    fund_code = _normalize_stock_code(code)
    if fund_code in TRACKING_INDEX_BY_FUND_CODE:
        return TRACKING_INDEX_BY_FUND_CODE[fund_code]
    name = str(fund_name or "")
    if not any(marker in name for marker in ("ETF联接", "ETF聯接", "指数", "指數", "LOF")):
        return None
    for marker, index_code, index_name in TRACKING_INDEX_BY_FUND_NAME:
        if marker in name:
            return index_code, index_name
    return None


def _target_etf_for_fund(code: str, fund_name: str | None) -> tuple[str, str] | None:
    fund_code = _normalize_stock_code(code)
    if fund_code in TARGET_ETF_BY_FUND_CODE:
        return TARGET_ETF_BY_FUND_CODE[fund_code]
    name = str(fund_name or "")
    if "ETF联接" not in name and "ETF聯接" not in name:
        return None
    for marker, etf_code, etf_name in TARGET_ETF_BY_FUND_NAME:
        if marker in name:
            return etf_code, etf_name
    return None


def _qdii_benchmark_for_fund(code: str, fund_name: str | None) -> dict | None:
    fund_code = _normalize_stock_code(code)
    if fund_code in QDII_BENCHMARK_BY_FUND_CODE:
        return QDII_BENCHMARK_BY_FUND_CODE[fund_code]
    name = str(fund_name or "")
    upper_name = name.upper()
    if "QDII" not in upper_name:
        return None
    for marker, benchmark_symbol, benchmark_name, fx_symbol in QDII_BENCHMARK_BY_FUND_NAME:
        if marker in name:
            return {
                "benchmark_symbol": benchmark_symbol,
                "benchmark_name": benchmark_name,
                "fx_symbol": fx_symbol,
            }
    return None


def _point_on_or_before(points: list[FactorPoint], date_key: str) -> FactorPoint | None:
    for point in reversed(points):
        if point.date <= date_key:
            return point
    return None


def _latest_nav_from_points(points: list[FactorPoint]) -> LatestNav | None:
    if not points:
        return None
    latest = points[-1]
    return LatestNav(nav=latest.value, date=latest.date)


def _eastmoney_nav_points_from_text(text: str) -> list[FactorPoint]:
    match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(?P<trend>\[.*?\]);", text or "", re.S)
    if not match:
        return []
    try:
        rows = json.loads(match.group("trend"))
    except json.JSONDecodeError:
        return []
    points: list[FactorPoint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_key = _eastmoney_timestamp_date(row.get("x"))
        value = _parse_float(row.get("y"))
        if date_key and value is not None and value > 0:
            points.append(FactorPoint(date=date_key, value=value))
    points.sort(key=lambda item: item.date)
    return points


def _eastmoney_timestamp_date(value) -> str:
    try:
        timestamp_ms = float(value)
    except (TypeError, ValueError):
        return ""
    return (datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def _points_from_dataframe(
    df,
    date_names: tuple[str, ...],
    value_names: tuple[str, ...],
    limit: int | None = None,
) -> list[FactorPoint]:
    if df is None or df.empty:
        return []
    points: list[FactorPoint] = []
    for _, row in df.iterrows():
        date = _date_key(_first_present(row, date_names))
        value = _parse_float(_first_present(row, value_names))
        if date and value is not None and value > 0:
            points.append(FactorPoint(date=date, value=value))
    points.sort(key=lambda item: item.date)
    if limit is not None:
        return points[-max(2, int(limit or 2)) :]
    return points


def _index_quotes_from_dataframe(df, factor_universe) -> dict[str, Quote]:
    if df is None or df.empty:
        return {}
    symbol_by_digits = {_index_digits(factor.code): factor.code for factor in factor_universe}
    names = {factor.code: factor.name for factor in factor_universe}
    result: dict[str, Quote] = {}
    for _, row in df.iterrows():
        digits = _index_digits(str(_first_present(row, ("代码", "symbol", "指数代码")) or ""))
        code = symbol_by_digits.get(digits)
        if not code:
            continue
        change_pct = _parse_float(_first_present(row, ("涨跌幅", "涨跌幅%", "change_pct")))
        if change_pct is None:
            continue
        quote_time = _extract_datetime_text(
            row,
            ("时间", "日期", "更新时间", "最新交易日", "trade_date", "date", "time"),
        )
        result[code] = Quote(
            code=code,
            name=str(_first_present(row, ("名称", "name", "指数简称")) or names.get(code) or code),
            change_pct=change_pct,
            quote_time=quote_time,
            trade_date=_date_key(quote_time),
        )
    return result


def _index_quote_from_dataframe(df, code: str, name: str) -> Quote | None:
    if df is None or df.empty:
        return None
    target_digits = _index_digits(code)
    for _, row in df.iterrows():
        digits = _index_digits(str(_first_present(row, ("代码", "symbol", "指数代码")) or ""))
        row_name = str(_first_present(row, ("名称", "name", "指数简称")) or "")
        if digits != target_digits and row_name != name:
            continue
        change_pct = _parse_float(_first_present(row, ("涨跌幅", "涨跌幅%", "change_pct")))
        if change_pct is None:
            return None
        quote_time = _extract_datetime_text(
            row,
            ("时间", "日期", "更新时间", "最新交易日", "trade_date", "date", "time"),
        )
        return Quote(
            code=code,
            name=row_name or name or code,
            change_pct=change_pct,
            quote_time=quote_time,
            trade_date=_extract_date_text(
                row,
                ("日期", "最新交易日", "交易日期", "trade_date", "date"),
            )
            or _date_key(quote_time),
        )
    return None


def _index_digits(code: str) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _tencent_symbol(code: str) -> str:
    normalized = _normalize_stock_code(code)
    if any(ch.isalpha() for ch in normalized):
        return f"us{normalized}"
    if len(normalized) == 5:
        return f"hk{normalized}"
    if normalized.startswith(("4", "8", "920")):
        return f"bj{normalized}"
    if normalized.startswith(("5", "6", "9")):
        return f"sh{normalized}"
    return f"sz{normalized}"


def _parse_tencent_quotes(text: str, target_codes: set[str]) -> dict[str, Quote]:
    result: dict[str, Quote] = {}
    for match in re.finditer(r'v_[a-z]{2}[A-Za-z0-9]+="(?P<body>[^"]*)"', text or ""):
        fields = match.group("body").split("~")
        if len(fields) < 4:
            continue
        code = _normalize_stock_code(fields[2])
        if code not in target_codes:
            continue
        change_pct = _parse_tencent_change_pct(fields)
        if change_pct is None:
            continue
        quote_time = _parse_tencent_quote_time(fields)
        result[code] = Quote(
            code=code,
            name=fields[1] or code,
            change_pct=change_pct,
            quote_time=quote_time,
            trade_date=_date_key(quote_time),
        )
    return result


def _parse_tencent_change_pct(fields: list[str]) -> float | None:
    for index, field in enumerate(fields):
        if _normalize_datetime_text(field):
            if index + 2 < len(fields):
                return _parse_float(fields[index + 2])
    for index in (32, 31):
        if index < len(fields):
            value = _parse_float(fields[index])
            if value is not None:
                return value
    return None


def _parse_tencent_quote_time(fields: list[str]) -> str | None:
    for field in fields:
        normalized = _normalize_datetime_text(field)
        if normalized:
            return normalized
    return None


def _extract_apidata_content(text: str) -> str:
    match = re.search(r'content:"(?P<content>.*)",arryear:', text, re.S)
    content = match.group("content") if match else text
    content = content.replace(r"\/", "/").replace(r"\"", '"')
    return html.unescape(content)


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _find_header_index(headers: list[str], marker: str, fallback: int) -> int:
    for index, header in enumerate(headers):
        if marker in header:
            return index
    return fallback
