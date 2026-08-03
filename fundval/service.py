from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Protocol

from .store import WatchFund, WatchlistStore, normalize_fund_code
from .valuation import (
    Holding,
    LatestNav,
    OfficialEstimate,
    Quote,
    apply_reconciliation_adjustment,
    build_reconciliation,
    calculate_holding_estimate,
    calibrate_confidence,
)


SNAPSHOT_REPAIR_INTERVAL = timedelta(minutes=10)
RECONCILIATION_RETRY_INTERVAL = timedelta(minutes=30)


class FundDataProvider(Protocol):
    def get_fund_name(self, code: str) -> str | None: ...

    def get_official_estimate(self, code: str) -> OfficialEstimate | None: ...

    def get_latest_nav(self, code: str) -> LatestNav | None: ...

    def get_nav_by_date(self, code: str, nav_date: str) -> LatestNav | None: ...

    def get_holdings(self, code: str) -> list[Holding]: ...

    def get_quotes(self, stock_codes: list[str]) -> dict[str, Quote]: ...

    def health(self) -> dict: ...


class FundValuationService:
    def __init__(
        self,
        store: WatchlistStore,
        provider: FundDataProvider,
        min_coverage_pct: float = 35.0,
    ):
        self.store = store
        self.provider = provider
        self.min_coverage_pct = min_coverage_pct
        self._last_reconciliation_attempt_at: datetime | None = None

    def add_fund(self, code: str, alias: str | None = None) -> WatchFund:
        fund_code = normalize_fund_code(code)
        name = self._safe_call(lambda: self.provider.get_fund_name(fund_code))
        return self.store.add_fund(fund_code, alias=alias, name=name)

    def list_funds(self) -> list[dict]:
        return [fund.to_dict() for fund in self.store.list_funds()]

    def delete_fund(self, code: str) -> bool:
        return self.store.delete_fund(code)

    def estimate_watchlist(
        self,
        now: datetime | None = None,
        use_snapshot_cache: bool = True,
    ) -> list[dict]:
        current = now or datetime.now()
        context = self._valuation_context(current)
        funds = self.store.list_funds()
        if use_snapshot_cache and self._should_use_previous_close_snapshot(context):
            return self._snapshot_estimates_for_funds(funds, context, current)
        return [self.estimate_fund(fund.code, now=current, use_snapshot_cache=use_snapshot_cache) for fund in funds]

    def estimate_fund(
        self,
        code: str,
        now: datetime | None = None,
        use_snapshot_cache: bool = True,
    ) -> dict:
        current = now or datetime.now()
        context = self._valuation_context(current)
        fund_code = normalize_fund_code(code)
        if use_snapshot_cache and self._should_use_previous_close_snapshot(context):
            fund = self.store.get_fund(fund_code) or WatchFund(code=fund_code)
            return self._snapshot_estimate_for_fund(fund, context, current)
        name = self._safe_call(lambda: self.provider.get_fund_name(fund_code))
        official = self._safe_call(lambda: self.provider.get_official_estimate(fund_code))
        if official is not None:
            latest_nav = self._safe_call(lambda: self.provider.get_latest_nav(fund_code))
            latest_nav_value = round(float(latest_nav.nav), 6) if latest_nav is not None and latest_nav.nav > 0 else None
            result = {
                "code": fund_code,
                "name": name or f"基金{fund_code}",
                "status": "estimated",
                "source": "official",
                "estimate_nav": round(float(official.nav), 6),
                "estimate_growth_pct": round(float(official.growth_pct), 4),
                "coverage_pct": 100.0,
                "confidence": 90.0,
                "reason": None,
                "latest_nav": latest_nav_value,
                "latest_nav_date": latest_nav.date if latest_nav_value is not None else None,
                "estimate_time": official.estimate_time,
                "contributions": [],
            }
            self._attach_valuation_context(
                result,
                context,
                trade_date=_date_key(official.trade_date) or _date_key(official.estimate_time),
            )
            return self._with_calibrated_confidence(fund_code, result)

        latest_nav = self._safe_call(lambda: self.provider.get_latest_nav(fund_code))
        actual_nav_result = self._actual_nav_estimate(fund_code, name or "", latest_nav, context)
        if actual_nav_result is not None:
            return self._with_calibrated_confidence(fund_code, actual_nav_result)

        holdings = self._safe_call(lambda: self.provider.get_holdings(fund_code)) or []
        quotes = self._safe_call(lambda: self.provider.get_quotes([h.code for h in holdings])) or {}
        result = calculate_holding_estimate(
            latest_nav=latest_nav,
            holdings=holdings,
            quotes=quotes,
            min_coverage_pct=self._coverage_floor(name or ""),
        ).to_dict()
        result.update(
            {
                "code": fund_code,
                "name": name or f"基金{fund_code}",
                "estimate_time": None,
            }
        )
        self._attach_valuation_context(result, context, trade_date=_quote_trade_date(quotes))
        return self._with_calibrated_confidence(fund_code, result)

    def create_snapshot(
        self,
        snapshot_key: str | None = None,
        captured_at: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        captured_time = now or datetime.now()
        valuation_time = now or _datetime_or_none(snapshot_key) or captured_time
        key = snapshot_key or self._default_snapshot_key(valuation_time)
        captured = captured_at or captured_time.strftime("%Y-%m-%d %H:%M:%S")
        valuations = self.estimate_watchlist(now=valuation_time, use_snapshot_cache=False)
        return self.store.save_snapshot(key, captured, valuations)

    def create_due_snapshot(self, now: datetime | None = None) -> dict | None:
        current = now or datetime.now()
        if not self._is_trading_day(current):
            return None
        if current.hour < 15:
            return None
        key = current.strftime("%Y-%m-%d 15:00")
        if self.store.has_snapshot(key):
            if not self._should_repair_snapshot(key, current):
                return None
        return self.create_snapshot(snapshot_key=key, now=current)

    def trading_status(self, now: datetime | None = None) -> dict:
        current = now or datetime.now()
        context = self._valuation_context(current)
        return {
            "date": current.strftime("%Y-%m-%d"),
            "now": current.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": context["trade_date"],
            "market_phase": context["market_phase"],
            "is_final": context["is_final"],
            "is_trading_day": context["is_trading_day"],
            "is_refresh_window": context["is_trading_day"] and self._is_refresh_window(current),
        }

    def list_snapshots(self) -> list[dict]:
        return self.store.list_snapshots()

    def get_snapshot(self, snapshot_key: str) -> list[dict]:
        return self.store.get_snapshot(snapshot_key)

    def reconcile_snapshots(self, now: datetime | None = None) -> dict:
        current = now or datetime.now()
        reconciled_at = current.strftime("%Y-%m-%d %H:%M:%S")
        rows = self.store.list_unreconciled_valuations()
        items = []
        skipped = 0
        for snapshot in rows:
            actual_nav = self._actual_nav_for_snapshot(snapshot)
            if actual_nav is None:
                skipped += 1
                continue
            try:
                reconciliation = build_reconciliation(
                    snapshot,
                    actual_nav=actual_nav.nav,
                    actual_nav_date=actual_nav.date or snapshot.get("snapshot_date") or "",
                    reconciled_at=reconciled_at,
                )
            except ValueError:
                skipped += 1
                continue
            items.append(self.store.save_reconciliation(reconciliation))
        return {
            "checked": len(rows),
            "reconciled": len(items),
            "skipped": skipped,
            "items": items,
        }

    def reconcile_due_snapshots(self, now: datetime | None = None) -> dict | None:
        current = now or datetime.now()
        if (
            self._last_reconciliation_attempt_at is not None
            and current - self._last_reconciliation_attempt_at < RECONCILIATION_RETRY_INTERVAL
        ):
            return None
        self._last_reconciliation_attempt_at = current
        return self.reconcile_snapshots(now=current)

    def list_reconciliations(self, limit: int = 50) -> list[dict]:
        return self.store.list_reconciliations(limit=limit)

    def health(self) -> dict:
        return self._safe_call(self.provider.health) or {"provider": "unavailable"}

    def _with_calibrated_confidence(self, fund_code: str, result: dict) -> dict:
        if result.get("status") != "estimated" or not isinstance(result.get("confidence"), (int, float)):
            return result
        profile = self.store.get_reconciliation_profile(fund_code, source=result.get("source"))
        apply_reconciliation_adjustment(result, profile)
        calibrated = calibrate_confidence(float(result["confidence"]), profile)
        if profile.get("sample_count", 0) >= 5:
            result["base_confidence"] = result["confidence"]
            result["confidence"] = calibrated
            result["confidence_profile"] = profile
        return result

    def _actual_nav_estimate(
        self,
        fund_code: str,
        fund_name: str,
        latest_nav: LatestNav | None,
        context: dict,
    ) -> dict | None:
        if latest_nav is None or latest_nav.nav <= 0:
            return None
        latest_nav_date = _date_key(latest_nav.date)
        trade_date = context["trade_date"]
        if not context["is_final"] or latest_nav_date != trade_date:
            return None

        previous_nav = self._previous_nav(fund_code, trade_date)
        growth_pct = None
        if previous_nav is not None and previous_nav.nav > 0:
            growth_pct = (latest_nav.nav / previous_nav.nav - 1) * 100

        result = {
            "code": fund_code,
            "name": fund_name or f"基金{fund_code}",
            "status": "estimated",
            "source": "nav",
            "estimate_nav": round(float(latest_nav.nav), 6),
            "estimate_growth_pct": round(growth_pct, 4) if growth_pct is not None else None,
            "coverage_pct": 100.0,
            "confidence": 100.0,
            "reason": None,
            "latest_nav": round(float(previous_nav.nav), 6) if previous_nav is not None else None,
            "latest_nav_date": previous_nav.date if previous_nav is not None else None,
            "actual_nav": round(float(latest_nav.nav), 6),
            "actual_nav_date": latest_nav.date,
            "estimate_time": None,
            "contributions": [],
        }
        self._attach_valuation_context(result, context, trade_date=latest_nav_date)
        return result

    def _actual_nav_for_snapshot(self, snapshot: dict) -> LatestNav | None:
        snapshot_date = _date_key(snapshot.get("snapshot_date"))
        if not snapshot_date:
            return None
        fund_code = normalize_fund_code(snapshot.get("code") or "")
        by_date = getattr(self.provider, "get_nav_by_date", None)
        if callable(by_date):
            actual = self._safe_call(lambda: by_date(fund_code, snapshot_date))
            if actual is not None and actual.nav > 0 and _date_key(actual.date) == snapshot_date:
                return actual
        latest = self._safe_call(lambda: self.provider.get_latest_nav(fund_code))
        if latest is not None and latest.nav > 0 and _date_key(latest.date) == snapshot_date:
            return latest
        return None

    def _should_repair_snapshot(self, snapshot_key: str, current: datetime) -> bool:
        rows = self.store.get_snapshot(snapshot_key)
        if not rows or not _snapshot_has_incomplete(rows):
            return False
        captured_at = _snapshot_captured_at(self.store.list_snapshots(), snapshot_key)
        if captured_at is None:
            return True
        return current - captured_at >= SNAPSHOT_REPAIR_INTERVAL

    def _coverage_floor(self, fund_name: str) -> float:
        normalized = fund_name.upper()
        if "ETF联接" in fund_name or "ETF聯接" in fund_name:
            return 0.1
        if "QDII" in normalized:
            return 2.0
        return self.min_coverage_pct

    def _valuation_context(self, current: datetime) -> dict:
        date_key = current.strftime("%Y-%m-%d")
        is_trading_day = self._is_trading_day(current)
        minutes = current.hour * 60 + current.minute
        if is_trading_day and minutes < 9 * 60:
            return {
                "trade_date": self._latest_trading_date(current.date(), include_current=False),
                "market_phase": "pre_market",
                "is_final": True,
                "is_trading_day": is_trading_day,
            }
        if is_trading_day and minutes < 15 * 60:
            return {
                "trade_date": date_key,
                "market_phase": "trading",
                "is_final": False,
                "is_trading_day": is_trading_day,
            }
        if is_trading_day:
            return {
                "trade_date": date_key,
                "market_phase": "closed",
                "is_final": True,
                "is_trading_day": is_trading_day,
            }
        return {
            "trade_date": self._latest_trading_date(current.date(), include_current=False),
            "market_phase": "non_trading",
            "is_final": True,
            "is_trading_day": is_trading_day,
        }

    def _attach_valuation_context(
        self,
        result: dict,
        context: dict,
        trade_date: str | None = None,
    ) -> None:
        effective_trade_date = self._normalize_trade_date(_date_key(trade_date), context)
        result["trade_date"] = effective_trade_date
        result["market_phase"] = context["market_phase"]
        result["is_final"] = context["is_final"]
        if effective_trade_date and effective_trade_date < context["trade_date"]:
            result["market_phase"] = "closed"
            result["is_final"] = True

    def _default_snapshot_key(self, current: datetime) -> str:
        context = self._valuation_context(current)
        if context["is_final"] and context["trade_date"]:
            return f"{context['trade_date']} 15:00"
        return current.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _should_use_previous_close_snapshot(context: dict) -> bool:
        return not context["is_trading_day"] and bool(context["trade_date"])

    @staticmethod
    def _previous_close_snapshot_key(context: dict) -> str:
        return f"{context['trade_date']} 15:00"

    def _snapshot_estimates_for_funds(
        self,
        funds: list[WatchFund],
        context: dict,
        current: datetime,
    ) -> list[dict]:
        snapshot_key = self._previous_close_snapshot_key(context)
        rows_by_code = self._snapshot_rows_by_code(snapshot_key)
        return [
            self._snapshot_result_for_fund(fund, rows_by_code[fund.code], context, snapshot_key)
            if fund.code in rows_by_code and _is_usable_snapshot_row(rows_by_code[fund.code])
            else self._calculate_snapshot_fallback(fund, current)
            for fund in funds
        ]

    def _snapshot_estimate_for_fund(self, fund: WatchFund, context: dict, current: datetime) -> dict:
        snapshot_key = self._previous_close_snapshot_key(context)
        rows_by_code = self._snapshot_rows_by_code(snapshot_key)
        if fund.code not in rows_by_code or not _is_usable_snapshot_row(rows_by_code[fund.code]):
            return self._calculate_snapshot_fallback(fund, current)
        return self._snapshot_result_for_fund(fund, rows_by_code[fund.code], context, snapshot_key)

    def _calculate_snapshot_fallback(self, fund: WatchFund, current: datetime) -> dict:
        result = self.estimate_fund(fund.code, now=current, use_snapshot_cache=False)
        result["alias"] = fund.alias
        if fund.name and not result.get("name"):
            result["name"] = fund.name
        return result

    def _snapshot_rows_by_code(self, snapshot_key: str) -> dict[str, dict]:
        rows_by_code: dict[str, dict] = {}
        for row in self.store.get_snapshot(snapshot_key):
            try:
                fund_code = normalize_fund_code(row.get("code") or "")
            except ValueError:
                continue
            rows_by_code[fund_code] = row
        return rows_by_code

    def _snapshot_result_for_fund(
        self,
        fund: WatchFund,
        row: dict | None,
        context: dict,
        snapshot_key: str,
    ) -> dict:
        if row is None:
            return self._missing_snapshot_result(fund, context, snapshot_key)
        result = dict(row)
        result["code"] = fund.code
        result["alias"] = fund.alias
        result["name"] = result.get("name") or fund.name or f"基金{fund.code}"
        result["snapshot_key"] = snapshot_key
        result["snapshot_date"] = context["trade_date"]
        result["trade_date"] = _date_key(result.get("trade_date")) or context["trade_date"]
        result["market_phase"] = context["market_phase"]
        result["is_final"] = True
        return result

    def _missing_snapshot_result(self, fund: WatchFund, context: dict, snapshot_key: str) -> dict:
        return {
            "code": fund.code,
            "alias": fund.alias,
            "name": fund.name or f"基金{fund.code}",
            "status": "unavailable",
            "source": "snapshot",
            "estimate_nav": None,
            "estimate_growth_pct": None,
            "coverage_pct": 0.0,
            "confidence": 0.0,
            "reason": "no_previous_close_snapshot",
            "latest_nav": None,
            "latest_nav_date": None,
            "estimate_time": None,
            "contributions": [],
            "snapshot_key": snapshot_key,
            "snapshot_date": context["trade_date"],
            "trade_date": context["trade_date"],
            "market_phase": context["market_phase"],
            "is_final": True,
        }

    def _latest_trading_date(self, current: date, include_current: bool = True) -> str:
        latest = getattr(self.provider, "latest_trading_day", None)
        if callable(latest):
            value = self._safe_call(lambda: latest(current, include_current=include_current))
            if _date_key(value):
                return _date_key(value)

        start = 0 if include_current else 1
        for offset in range(start, 370):
            candidate = current - timedelta(days=offset)
            candidate_dt = datetime.combine(candidate, time.min)
            if self._is_trading_day(candidate_dt):
                return candidate.strftime("%Y-%m-%d")
        return current.strftime("%Y-%m-%d")

    def _previous_nav(self, fund_code: str, trade_date: str) -> LatestNav | None:
        parsed = _parse_date_key(trade_date)
        if parsed is None:
            return None
        previous_trade_date = self._latest_trading_date(parsed, include_current=False)
        by_date = getattr(self.provider, "get_nav_by_date", None)
        if not callable(by_date):
            return None
        previous = self._safe_call(lambda: by_date(fund_code, previous_trade_date))
        if previous is None or previous.nav <= 0:
            return None
        if _date_key(previous.date) != previous_trade_date:
            return None
        return previous

    def _normalize_trade_date(self, trade_date: str, context: dict) -> str:
        context_trade_date = context["trade_date"]
        if not trade_date:
            return context_trade_date
        parsed = _parse_date_key(trade_date)
        if parsed is None:
            return context_trade_date
        if context_trade_date and trade_date > context_trade_date:
            return context_trade_date
        if not self._is_trading_day(datetime.combine(parsed, time.min)):
            return self._latest_trading_date(parsed, include_current=False)
        return trade_date

    def _is_trading_day(self, current: datetime) -> bool:
        calendar = getattr(self.provider, "is_trading_day", None)
        if callable(calendar):
            try:
                return bool(calendar(current.date()))
            except Exception:
                pass
        return current.weekday() < 5

    @staticmethod
    def _is_refresh_window(current: datetime) -> bool:
        minutes = current.hour * 60 + current.minute
        return 9 * 60 <= minutes <= 15 * 60

    @staticmethod
    def _safe_call(fn):
        try:
            return fn()
        except Exception:
            return None


def _date_key(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _parse_date_key(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _quote_trade_date(quotes: dict[str, Quote]) -> str:
    counts: dict[str, int] = {}
    for quote in quotes.values():
        trade_date = _date_key(getattr(quote, "trade_date", None)) or _date_key(getattr(quote, "quote_time", None))
        if not trade_date:
            continue
        counts[trade_date] = counts.get(trade_date, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _is_usable_snapshot_row(row: dict) -> bool:
    if row.get("status") != "estimated":
        return False
    return row.get("estimate_nav") is not None and row.get("estimate_growth_pct") is not None


def _snapshot_has_incomplete(rows: list[dict]) -> bool:
    for row in rows:
        if row.get("status") != "estimated":
            return True
        if row.get("estimate_nav") is None or row.get("estimate_growth_pct") is None:
            return True
    return False


def _snapshot_captured_at(snapshots: list[dict], snapshot_key: str) -> datetime | None:
    for snapshot in snapshots:
        if snapshot.get("snapshot_key") == snapshot_key:
            return _datetime_or_none(snapshot.get("captured_at"))
    return None


def _datetime_or_none(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None
