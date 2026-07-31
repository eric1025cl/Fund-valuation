from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .store import WatchFund, WatchlistStore, normalize_fund_code
from .valuation import (
    Holding,
    LatestNav,
    OfficialEstimate,
    Quote,
    build_reconciliation,
    calculate_holding_estimate,
    calibrate_confidence,
)


SNAPSHOT_REPAIR_INTERVAL = timedelta(minutes=10)


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

    def add_fund(self, code: str, alias: str | None = None) -> WatchFund:
        fund_code = normalize_fund_code(code)
        name = self._safe_call(lambda: self.provider.get_fund_name(fund_code))
        return self.store.add_fund(fund_code, alias=alias, name=name)

    def list_funds(self) -> list[dict]:
        return [fund.to_dict() for fund in self.store.list_funds()]

    def delete_fund(self, code: str) -> bool:
        return self.store.delete_fund(code)

    def estimate_watchlist(self) -> list[dict]:
        return [self.estimate_fund(fund.code) for fund in self.store.list_funds()]

    def estimate_fund(self, code: str) -> dict:
        fund_code = normalize_fund_code(code)
        name = self._safe_call(lambda: self.provider.get_fund_name(fund_code))
        official = self._safe_call(lambda: self.provider.get_official_estimate(fund_code))
        if official is not None:
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
                "latest_nav": None,
                "latest_nav_date": None,
                "estimate_time": official.estimate_time,
                "contributions": [],
            }
            return self._with_calibrated_confidence(fund_code, result)

        latest_nav = self._safe_call(lambda: self.provider.get_latest_nav(fund_code))
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
        return self._with_calibrated_confidence(fund_code, result)

    def create_snapshot(
        self,
        snapshot_key: str | None = None,
        captured_at: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        current = now or datetime.now()
        key = snapshot_key or current.strftime("%Y-%m-%d %H:%M")
        captured = captured_at or current.strftime("%Y-%m-%d %H:%M:%S")
        valuations = self.estimate_watchlist()
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
        is_trading_day = self._is_trading_day(current)
        return {
            "date": current.strftime("%Y-%m-%d"),
            "now": current.strftime("%Y-%m-%d %H:%M:%S"),
            "is_trading_day": is_trading_day,
            "is_refresh_window": is_trading_day and self._is_refresh_window(current),
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

    def health(self) -> dict:
        return self._safe_call(self.provider.health) or {"provider": "unavailable"}

    def _with_calibrated_confidence(self, fund_code: str, result: dict) -> dict:
        if result.get("status") != "estimated" or not isinstance(result.get("confidence"), (int, float)):
            return result
        profile = self.store.get_reconciliation_profile(fund_code, source=result.get("source"))
        calibrated = calibrate_confidence(float(result["confidence"]), profile)
        if profile.get("sample_count", 0) >= 5:
            result["base_confidence"] = result["confidence"]
            result["confidence"] = calibrated
            result["confidence_profile"] = profile
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
