from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from typing import Any

from .valuation import Holding, LatestNav, OfficialEstimate, Quote


class AkshareProvider:
    def __init__(self, cache_ttl_seconds: int = 300):
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._cache: dict[str, tuple[datetime, Any]] = {}

    def get_fund_name(self, code: str) -> str | None:
        df = self._cached("fund_names", self._fetch_fund_names, timedelta(hours=12))
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
            return OfficialEstimate(
                nav=nav,
                growth_pct=growth,
                estimate_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        return None

    def get_latest_nav(self, code: str) -> LatestNav | None:
        ak = self._ak()
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
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
        ak = self._ak()
        df = ak.fund_open_fund_info_em(symbol=code, indicator="\u5355\u4f4d\u51c0\u503c\u8d70\u52bf")
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

    def get_holdings(self, code: str) -> list[Holding]:
        ak = self._ak()
        current_year = datetime.now().year
        frames = []
        for year in ("", *range(current_year, current_year - 5, -1)):
            try:
                frame = ak.fund_portfolio_hold_em(symbol=code, date=str(year))
                if frame is not None and not frame.empty:
                    frames.append(frame)
            except Exception:
                continue
        if not frames:
            return self._get_eastmoney_holdings(code)
        for df in frames:
            holdings = self._holdings_from_dataframe(df)
            if holdings:
                return holdings
        return self._get_eastmoney_holdings(code)

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
            timeout=15,
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
        dates = self._cached("trade_dates", self._fetch_trade_dates, timedelta(hours=12))
        return date_key in dates

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

    def _cached(self, key: str, loader, ttl: timedelta | None = None):
        now = datetime.now()
        effective_ttl = ttl or self.cache_ttl
        cached = self._cache.get(key)
        if cached and now - cached[0] < effective_ttl:
            return cached[1]
        value = loader()
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


def _first_present(row, names: tuple[str, ...]):
    for name in names:
        if name in row and row.get(name) is not None:
            return row.get(name)
    return None


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
        result[code] = Quote(
            code=code,
            name=str(_first_present(row, ("名称", "name", "证券简称")) or code),
            change_pct=change_pct,
        )
    return result


def _tencent_symbol(code: str) -> str:
    normalized = _normalize_stock_code(code)
    if any(ch.isalpha() for ch in normalized):
        return f"us{normalized}"
    if len(normalized) == 5:
        return f"hk{normalized}"
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
        result[code] = Quote(code=code, name=fields[1] or code, change_pct=change_pct)
    return result


def _parse_tencent_change_pct(fields: list[str]) -> float | None:
    for index, field in enumerate(fields):
        if re.fullmatch(r"\d{14}", field) or re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", field):
            if index + 2 < len(fields):
                return _parse_float(fields[index + 2])
    for index in (32, 31):
        if index < len(fields):
            value = _parse_float(fields[index])
            if value is not None:
                return value
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
