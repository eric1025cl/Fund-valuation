from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import date, datetime, time, timedelta
from threading import Lock, Thread
from typing import Protocol

from .store import WatchFund, WatchlistStore, normalize_fund_code
from .factor_fit import fit_factor_model
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
SNAPSHOT_SAVE_MINUTE = 15 * 60 + 5
LIVE_REFRESH_CACHE_TTL = timedelta(minutes=10)


class FundDataProvider(Protocol):
    def get_fund_name(self, code: str) -> str | None: ...

    def get_official_estimate(self, code: str) -> OfficialEstimate | None: ...

    def get_latest_nav(self, code: str) -> LatestNav | None: ...

    def get_nav_by_date(self, code: str, nav_date: str) -> LatestNav | None: ...

    def get_holdings(self, code: str) -> list[Holding]: ...

    def get_quotes(self, stock_codes: list[str]) -> dict[str, Quote]: ...

    def get_tracking_index_quote(self, code: str, fund_name: str | None = None) -> Quote | None: ...

    def get_qdii_benchmark_quote(
        self,
        code: str,
        fund_name: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict | None: ...

    def health(self) -> dict: ...


class FundValuationService:
    def __init__(
        self,
        store: WatchlistStore,
        provider: FundDataProvider,
        min_coverage_pct: float = 35.0,
        max_workers: int = 4,
        live_refresh_timeout_seconds: float = 25.0,
        live_refresh_max_workers: int = 8,
    ):
        self.store = store
        self.provider = provider
        self.min_coverage_pct = min_coverage_pct
        self.max_workers = max(1, int(max_workers or 1))
        self.live_refresh_timeout_seconds = max(0.1, float(live_refresh_timeout_seconds or 0.1))
        self.live_refresh_max_workers = max(1, int(live_refresh_max_workers or 1))
        self._last_reconciliation_attempt_at: datetime | None = None
        self._watchlist_cache: list[dict] | None = None
        self._watchlist_cache_at: datetime | None = None
        self._watchlist_refreshing = False
        self._watchlist_cache_generation = 0
        self._watchlist_cache_lock = Lock()

    def add_fund(self, code: str, alias: str | None = None) -> WatchFund:
        fund_code = normalize_fund_code(code)
        name = self._safe_call(lambda: self.provider.get_fund_name(fund_code))
        fund = self.store.add_fund(fund_code, alias=alias, name=name)
        self.invalidate_watchlist_cache()
        return fund

    def list_funds(self) -> list[dict]:
        return [fund.to_dict() for fund in self.store.list_funds()]

    def delete_fund(self, code: str) -> bool:
        deleted = self.store.delete_fund(code)
        if deleted:
            self.invalidate_watchlist_cache()
        return deleted

    def invalidate_watchlist_cache(self) -> None:
        with self._watchlist_cache_lock:
            self._watchlist_cache = None
            self._watchlist_cache_at = None
            self._watchlist_cache_generation += 1

    def estimate_watchlist_cached(
        self,
        now: datetime | None = None,
        max_age: timedelta = LIVE_REFRESH_CACHE_TTL,
        force_refresh: bool = False,
    ) -> list[dict]:
        current = now or datetime.now()
        context = self._valuation_context(current)
        if self._should_use_previous_close_snapshot(context):
            funds = self.store.list_funds()
            if self.store.has_snapshot(self._previous_close_snapshot_key(context)):
                return self._snapshot_estimates_for_funds(funds, context, current)

        wall_clock = datetime.now()
        should_refresh = False
        refresh_generation = 0
        with self._watchlist_cache_lock:
            if (
                not force_refresh
                and self._watchlist_cache is not None
                and self._watchlist_cache_at is not None
                and wall_clock - self._watchlist_cache_at < max_age
            ):
                return _copy_result_rows(self._watchlist_cache)

            if not self._watchlist_refreshing:
                self._watchlist_refreshing = True
                self._watchlist_cache_generation += 1
                refresh_generation = self._watchlist_cache_generation
                should_refresh = True

            cached_rows = _copy_result_rows(self._watchlist_cache)

        if should_refresh:
            Thread(
                target=self._refresh_watchlist_cache,
                args=(current, refresh_generation),
                daemon=True,
            ).start()
        return cached_rows

    def _refresh_watchlist_cache(self, current: datetime, generation: int) -> None:
        try:
            rows = self._estimate_live_watchlist_with_timeout(current)
            with self._watchlist_cache_lock:
                if generation == self._watchlist_cache_generation:
                    self._watchlist_cache = _copy_result_rows(rows)
                    self._watchlist_cache_at = datetime.now()
        finally:
            with self._watchlist_cache_lock:
                self._watchlist_refreshing = False

    def _estimate_live_watchlist_with_timeout(self, current: datetime) -> list[dict]:
        context = self._valuation_context(current)
        funds = self.store.list_funds()
        if self._should_use_previous_close_snapshot(context):
            return self._snapshot_estimates_for_funds(funds, context, current)
        if not funds:
            return []

        worker_count = min(self.live_refresh_max_workers, len(funds))
        executor = ThreadPoolExecutor(max_workers=worker_count)
        future_by_index = {
            executor.submit(
                self.estimate_fund,
                fund.code,
                now=current,
                use_snapshot_cache=True,
                include_factor_monitoring=False,
                include_official_estimate=False,
            ): (index, fund)
            for index, fund in enumerate(funds)
        }
        results: list[dict | None] = [None] * len(funds)
        try:
            for future in as_completed(future_by_index, timeout=self.live_refresh_timeout_seconds):
                index, fund = future_by_index[future]
                try:
                    results[index] = future.result()
                except Exception:
                    results[index] = self._live_refresh_unavailable_result(fund, context, "refresh_failed")
        except TimeoutError:
            pass
        finally:
            for future, (index, fund) in future_by_index.items():
                if results[index] is None:
                    future.cancel()
                    results[index] = self._live_refresh_unavailable_result(fund, context, "refresh_timeout")
            executor.shutdown(wait=False, cancel_futures=True)
        return [result for result in results if result is not None]

    def _live_refresh_unavailable_result(self, fund: WatchFund, context: dict, reason: str) -> dict:
        result = {
            "code": fund.code,
            "alias": fund.alias,
            "name": fund.name or f"鍩洪噾{fund.code}",
            "status": "unavailable",
            "source": "refresh",
            "estimate_nav": None,
            "estimate_growth_pct": None,
            "coverage_pct": 0.0,
            "confidence": 0.0,
            "reason": reason,
            "latest_nav": None,
            "latest_nav_date": None,
            "estimate_time": None,
            "contributions": [],
        }
        self._attach_valuation_context(result, context)
        return result

    def estimate_watchlist(
        self,
        now: datetime | None = None,
        use_snapshot_cache: bool = True,
        include_factor_monitoring: bool = True,
        include_official_estimate: bool = True,
    ) -> list[dict]:
        current = now or datetime.now()
        context = self._valuation_context(current)
        funds = self.store.list_funds()
        if use_snapshot_cache and self._should_use_previous_close_snapshot(context):
            return self._snapshot_estimates_for_funds(funds, context, current)
        if len(funds) <= 1 or self.max_workers <= 1:
            return [
                self.estimate_fund(
                    fund.code,
                    now=current,
                    use_snapshot_cache=use_snapshot_cache,
                    include_factor_monitoring=include_factor_monitoring,
                    include_official_estimate=include_official_estimate,
                )
                for fund in funds
            ]
        worker_count = min(self.max_workers, len(funds))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            return list(
                executor.map(
                    lambda fund: self.estimate_fund(
                        fund.code,
                        now=current,
                        use_snapshot_cache=use_snapshot_cache,
                        include_factor_monitoring=include_factor_monitoring,
                        include_official_estimate=include_official_estimate,
                    ),
                    funds,
                )
            )

    def estimate_fund(
        self,
        code: str,
        now: datetime | None = None,
        use_snapshot_cache: bool = True,
        include_factor_monitoring: bool = True,
        include_official_estimate: bool = True,
    ) -> dict:
        current = now or datetime.now()
        context = self._valuation_context(current)
        fund_code = normalize_fund_code(code)
        if use_snapshot_cache and self._should_use_previous_close_snapshot(context):
            fund = self.store.get_fund(fund_code) or WatchFund(code=fund_code)
            return self._snapshot_estimate_for_fund(fund, context, current)
        stored_fund = self.store.get_fund(fund_code)
        name = (stored_fund.name if stored_fund else None) or self._safe_call(
            lambda: self.provider.get_fund_name(fund_code)
        )
        latest_nav = self._safe_call(lambda: self.provider.get_latest_nav(fund_code))
        target_trade_date = self._target_trade_date(name or "", context)
        actual_nav_result = self._actual_nav_estimate(
            fund_code,
            name or "",
            latest_nav,
            context,
            target_trade_date,
        )
        if actual_nav_result is not None:
            return self._with_calibrated_confidence(fund_code, actual_nav_result)

        official = (
            self._safe_call(lambda: self.provider.get_official_estimate(fund_code))
            if include_official_estimate
            else None
        )
        if official is not None:
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
            official_trade_date = _date_key(official.trade_date)
            if _is_qdii_name(name or "") and (
                not official_trade_date or official_trade_date > target_trade_date
            ):
                official_trade_date = target_trade_date
            self._attach_valuation_context(
                result,
                context,
                trade_date=official_trade_date or _date_key(official.estimate_time),
            )
            return self._with_calibrated_confidence(fund_code, result)

        qdii_benchmark_result = self._qdii_benchmark_estimate(
            fund_code,
            name or "",
            latest_nav,
            context,
            target_trade_date,
        )
        if qdii_benchmark_result is not None:
            return self._with_calibrated_confidence(fund_code, qdii_benchmark_result)

        holdings = self._safe_call(lambda: self.provider.get_holdings(fund_code)) or []
        quotes = self._safe_call(lambda: self.provider.get_quotes([h.code for h in holdings])) or {}
        result = calculate_holding_estimate(
            latest_nav=latest_nav,
            holdings=holdings,
            quotes=quotes,
            min_coverage_pct=self._coverage_floor(name or ""),
        ).to_dict()
        factor_result = self._factor_fit_estimate(fund_code, latest_nav) if include_factor_monitoring else None
        tracking_quote = self._tracking_index_quote(fund_code, name or "")
        if result.get("status") != "estimated" and factor_result is not None and factor_result.status == "estimated":
            factor_data = factor_result.to_dict()
            factor_data.update(
                {
                    "code": fund_code,
                    "name": name or f"基金{fund_code}",
                    "estimate_time": None,
                    "holding_reason": result.get("reason"),
                    "holding_coverage_pct": result.get("coverage_pct"),
                }
            )
            self._attach_valuation_context(
                factor_data,
                context,
                trade_date=factor_data.get("trade_date") or target_trade_date,
            )
            return self._with_calibrated_confidence(fund_code, factor_data)

        self._blend_uncovered_holding_estimate(result, factor_result, tracking_quote, name or "")
        self._attach_holding_estimate_risk(result)
        self._attach_factor_monitoring(result, factor_result)
        result.update(
            {
                "code": fund_code,
                "name": name or f"基金{fund_code}",
                "estimate_time": None,
            }
        )
        holding_trade_date = _quote_trade_date(quotes)
        if _is_qdii_name(name or "") and (
            not holding_trade_date or holding_trade_date > target_trade_date
        ):
            holding_trade_date = target_trade_date
        self._attach_valuation_context(result, context, trade_date=holding_trade_date or target_trade_date)
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
        minutes = current.hour * 60 + current.minute
        if minutes < SNAPSHOT_SAVE_MINUTE:
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

    def delete_snapshot(self, snapshot_key: str) -> int:
        deleted = self.store.delete_snapshot(snapshot_key)
        if deleted:
            self.invalidate_watchlist_cache()
        return deleted

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
        target_trade_date: str,
    ) -> dict | None:
        if not target_trade_date or not self._is_final_target(context, target_trade_date):
            return None

        target_nav = self._published_nav_for_target(fund_code, latest_nav, target_trade_date)
        if target_nav is None:
            return None

        target_nav_date = _date_key(target_nav.date)
        previous_nav = self._previous_nav(fund_code, target_trade_date)
        growth_pct = None
        if previous_nav is not None and previous_nav.nav > 0:
            growth_pct = (target_nav.nav / previous_nav.nav - 1) * 100

        result = {
            "code": fund_code,
            "name": fund_name or f"基金{fund_code}",
            "status": "estimated",
            "source": "nav",
            "estimate_nav": round(float(target_nav.nav), 6),
            "estimate_growth_pct": round(growth_pct, 4) if growth_pct is not None else None,
            "coverage_pct": 100.0,
            "confidence": 100.0,
            "reason": None,
            "latest_nav": round(float(previous_nav.nav), 6) if previous_nav is not None else None,
            "latest_nav_date": previous_nav.date if previous_nav is not None else None,
            "actual_nav": round(float(target_nav.nav), 6),
            "actual_nav_date": target_nav.date,
            "estimate_time": None,
            "contributions": [],
        }
        self._attach_valuation_context(result, context, trade_date=target_nav_date)
        return result

    def _factor_fit_estimate(self, fund_code: str, latest_nav: LatestNav | None):
        nav_history = getattr(self.provider, "get_nav_history", None)
        factor_histories = getattr(self.provider, "get_factor_histories", None)
        factor_quotes = getattr(self.provider, "get_factor_quotes", None)
        if not callable(nav_history) or not callable(factor_histories) or not callable(factor_quotes):
            return None

        history = self._safe_call(lambda: nav_history(fund_code, limit=120)) or []
        histories = self._safe_call(lambda: factor_histories(limit=120)) or {}
        if not histories:
            return None
        quotes = self._safe_call(lambda: factor_quotes(list(histories.keys()))) or {}
        return fit_factor_model(
            latest_nav=latest_nav,
            fund_history=history,
            factor_histories=histories,
            factor_quotes=quotes,
        )

    def _tracking_index_quote(self, fund_code: str, fund_name: str) -> Quote | None:
        get_tracking_index_quote = getattr(self.provider, "get_tracking_index_quote", None)
        if not callable(get_tracking_index_quote):
            return None
        return self._safe_call(lambda: get_tracking_index_quote(fund_code, fund_name=fund_name))

    def _qdii_benchmark_estimate(
        self,
        fund_code: str,
        fund_name: str,
        latest_nav: LatestNav | None,
        context: dict,
        target_trade_date: str,
    ) -> dict | None:
        if not _is_qdii_name(fund_name):
            return None
        if latest_nav is None or latest_nav.nav <= 0:
            return None
        latest_nav_date = _date_key(latest_nav.date)
        if not latest_nav_date or not target_trade_date or latest_nav_date >= target_trade_date:
            return None
        get_qdii_benchmark_quote = getattr(self.provider, "get_qdii_benchmark_quote", None)
        if not callable(get_qdii_benchmark_quote):
            return None
        quote = self._safe_call(
            lambda: get_qdii_benchmark_quote(
                fund_code,
                fund_name=fund_name,
                from_date=latest_nav_date,
                to_date=target_trade_date,
            )
        )
        if not isinstance(quote, dict):
            return None
        growth_pct = _number_or_none(quote.get("change_pct"))
        if growth_pct is None:
            return None
        result = {
            "code": fund_code,
            "name": fund_name or f"基金{fund_code}",
            "status": "estimated",
            "source": "qdii_benchmark",
            "estimate_nav": round(float(latest_nav.nav) * (1 + growth_pct / 100.0), 6),
            "estimate_growth_pct": round(growth_pct, 4),
            "coverage_pct": 100.0,
            "confidence": 85.0,
            "reason": None,
            "latest_nav": round(float(latest_nav.nav), 6),
            "latest_nav_date": latest_nav_date,
            "estimate_time": None,
            "contributions": [],
        }
        for key in (
            "benchmark_symbol",
            "benchmark_name",
            "fx_symbol",
            "benchmark_growth_pct",
            "fx_growth_pct",
            "benchmark_start_date",
            "benchmark_end_date",
            "fx_start_date",
            "fx_end_date",
        ):
            if key in quote:
                result[key] = quote[key]
        self._attach_valuation_context(result, context, trade_date=target_trade_date)
        return result

    @staticmethod
    def _attach_factor_monitoring(result: dict, factor_result) -> None:
        if factor_result is None or factor_result.status != "estimated":
            return
        factor_data = factor_result.to_dict()
        result.update(
            {
                "fit_source": factor_data.get("source"),
                "fit_nav": factor_data.get("estimate_nav"),
                "fit_growth_pct": factor_data.get("estimate_growth_pct"),
                "fit_confidence": factor_data.get("confidence"),
                "fit_r2": factor_data.get("fit_r2"),
                "fit_residual_pct": factor_data.get("fit_residual_pct"),
                "fit_sample_count": factor_data.get("sample_count"),
                "factor_exposures": factor_data.get("factor_exposures"),
                "baseline_factor_exposures": factor_data.get("baseline_factor_exposures"),
                "recent_factor_exposures": factor_data.get("recent_factor_exposures"),
                "style_drift_score": factor_data.get("style_drift_score"),
                "style_drift_level": factor_data.get("style_drift_level"),
                "style_drift_reason": factor_data.get("style_drift_reason"),
            }
        )

    @staticmethod
    def _blend_uncovered_holding_estimate(
        result: dict,
        factor_result,
        tracking_quote: Quote | None = None,
        fund_name: str = "",
    ) -> None:
        if result.get("status") != "estimated" or result.get("source") != "holding":
            return

        proxy_source = None
        proxy_name = None
        factor_growth = None
        proxy_growth = _number_or_none(getattr(tracking_quote, "change_pct", None))
        if proxy_growth is not None:
            proxy_source = "tracking_index"
            proxy_name = getattr(tracking_quote, "name", None)
        elif factor_result is not None and getattr(factor_result, "status", None) == "estimated":
            factor_data = factor_result.to_dict()
            factor_growth = _number_or_none(factor_data.get("estimate_growth_pct"))
            if factor_growth is not None:
                proxy_growth = factor_growth
                proxy_source = "factor_fit"
        else:
            return

        coverage = _number_or_none(result.get("coverage_pct"))
        latest_nav = _number_or_none(result.get("latest_nav"))
        if proxy_growth is None or coverage is None or latest_nav is None or latest_nav <= 0:
            return
        if coverage <= 0 or coverage >= 99.999:
            return

        contributions = result.get("contributions") or []
        covered_contribution = sum(
            _number_or_zero(item.get("contribution_pct"))
            for item in contributions
            if isinstance(item, dict)
        )
        uncovered_weight = max(0.0, 100.0 - min(coverage, 100.0))
        if uncovered_weight <= 0:
            return

        raw_growth = _number_or_none(result.get("estimate_growth_pct"))
        momentum_proxy = _holding_momentum_proxy_growth(
            raw_growth=raw_growth,
            factor_growth=factor_growth,
            coverage=coverage,
            fund_name=fund_name,
            tracking_quote=tracking_quote,
        )
        if momentum_proxy is not None:
            proxy_source = "holding_momentum_blend"
            proxy_growth = momentum_proxy["growth"]
            result["holding_momentum_growth_pct"] = round(momentum_proxy["momentum_growth"], 4)
            result["uncovered_proxy_momentum_weight_pct"] = round(momentum_proxy["momentum_weight"] * 100.0, 1)

        blended_growth = covered_contribution + proxy_growth * uncovered_weight / 100.0
        result["raw_holding_estimate_nav"] = result.get("estimate_nav")
        result["raw_holding_estimate_growth_pct"] = round(raw_growth, 4) if raw_growth is not None else None
        result["covered_contribution_pct"] = round(covered_contribution, 4)
        result["uncovered_weight_pct"] = round(uncovered_weight, 4)
        result["uncovered_proxy_source"] = proxy_source
        if proxy_name:
            result["uncovered_proxy_name"] = str(proxy_name)
        result["uncovered_proxy_growth_pct"] = round(proxy_growth, 4)
        result["estimate_growth_pct"] = round(blended_growth, 4)
        result["estimate_nav"] = round(latest_nav * (1 + blended_growth / 100.0), 6)

    @staticmethod
    def _attach_holding_estimate_risk(result: dict) -> None:
        if result.get("status") != "estimated" or result.get("source") != "holding":
            return

        coverage = _number_or_none(result.get("coverage_pct")) or 0.0
        contributions = result.get("contributions") or []
        weighted_abs_move = 0.0
        max_abs_move = 0.0
        for item in contributions:
            if not isinstance(item, dict):
                continue
            weight = _number_or_none(item.get("weight_pct")) or 0.0
            change = abs(_number_or_none(item.get("change_pct")) or 0.0)
            weighted_abs_move += weight * change
            max_abs_move = max(max_abs_move, change)
        if coverage > 0:
            weighted_abs_move /= coverage

        level = "low"
        reasons: list[str] = []
        if coverage < 50.0:
            level = "high"
            reasons.append("low_coverage")
        elif coverage < 65.0:
            level = "medium"
            reasons.append("medium_coverage")

        if weighted_abs_move >= 5.0 or max_abs_move >= 10.0:
            level = "high"
            reasons.append("volatile_holdings")
        elif weighted_abs_move >= 3.0 or max_abs_move >= 7.0:
            if level == "low":
                level = "medium"
            reasons.append("volatile_holdings")

        result["estimate_risk_level"] = level
        result["estimate_risk_reasons"] = reasons
        result["coverage_weighted_abs_move_pct"] = round(weighted_abs_move, 4)
        result["max_holding_abs_move_pct"] = round(max_abs_move, 4)

        confidence = _number_or_none(result.get("confidence"))
        if confidence is None or level == "low":
            return
        multiplier = 0.75 if level == "high" else 0.9
        result["pre_risk_confidence"] = round(confidence, 1)
        result["confidence"] = round(min(95.0, max(0.0, confidence * multiplier)), 1)

    def _published_nav_for_target(
        self,
        fund_code: str,
        latest_nav: LatestNav | None,
        target_trade_date: str,
    ) -> LatestNav | None:
        if latest_nav is None or latest_nav.nav <= 0:
            return None
        latest_nav_date = _date_key(latest_nav.date)
        if latest_nav_date == target_trade_date:
            return latest_nav
        if not latest_nav_date or latest_nav_date < target_trade_date:
            return None

        by_date = getattr(self.provider, "get_nav_by_date", None)
        if not callable(by_date):
            return None
        target_nav = self._safe_call(lambda: by_date(fund_code, target_trade_date))
        if target_nav is None or target_nav.nav <= 0:
            return None
        if _date_key(target_nav.date) != target_trade_date:
            return None
        return target_nav

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
        if _is_qdii_name(normalized):
            return 2.0
        return self.min_coverage_pct

    def _target_trade_date(self, fund_name: str, context: dict) -> str:
        context_trade_date = context["trade_date"]
        if not _is_qdii_name(fund_name):
            return context_trade_date
        parsed = _parse_date_key(context_trade_date)
        if parsed is None:
            return context_trade_date
        return self._latest_trading_date(parsed, include_current=False)

    @staticmethod
    def _is_final_target(context: dict, target_trade_date: str) -> bool:
        context_trade_date = context["trade_date"]
        return bool(context["is_final"] or (context_trade_date and target_trade_date < context_trade_date))

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
        result["target_trade_date"] = effective_trade_date
        result["context_trade_date"] = context["trade_date"]
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
        return bool(context["is_final"] and context["trade_date"])

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
        result["target_trade_date"] = result["trade_date"]
        result["context_trade_date"] = context["trade_date"]
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
            "target_trade_date": context["trade_date"],
            "context_trade_date": context["trade_date"],
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
        return 9 * 60 <= minutes <= SNAPSHOT_SAVE_MINUTE

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


def _is_qdii_name(fund_name: str) -> bool:
    return "QDII" in str(fund_name or "").upper()


def _is_index_like_name(fund_name: str) -> bool:
    text = str(fund_name or "")
    upper = text.upper()
    return any(marker in text for marker in ("指数", "指數", "联接", "聯接")) or any(
        marker in upper for marker in ("ETF", "LOF")
    )


def _holding_momentum_proxy_growth(
    raw_growth: float | None,
    factor_growth: float | None,
    coverage: float | None,
    fund_name: str,
    tracking_quote: Quote | None,
) -> dict | None:
    if tracking_quote is not None or _is_qdii_name(fund_name) or _is_index_like_name(fund_name):
        return None
    if raw_growth is None or factor_growth is None or coverage is None:
        return None
    if coverage < 40.0 or abs(raw_growth) < 2.0:
        return None
    if abs(raw_growth - factor_growth) < 0.5:
        return None

    momentum_weight = 0.75 if coverage >= 65.0 else 0.65
    return {
        "growth": raw_growth * momentum_weight + factor_growth * (1.0 - momentum_weight),
        "momentum_growth": raw_growth,
        "momentum_weight": momentum_weight,
    }


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


def _number_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_or_zero(value) -> float:
    parsed = _number_or_none(value)
    return parsed if parsed is not None else 0.0


def _copy_result_rows(rows: list[dict] | None) -> list[dict]:
    return [dict(row) for row in rows or []]
