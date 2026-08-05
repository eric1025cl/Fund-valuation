import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from fundval.service import FundValuationService
from fundval.store import WatchlistStore
from fundval.factor_fit import FactorPoint
from fundval.valuation import Holding, LatestNav, OfficialEstimate, Quote


TRADING_TIME = datetime(2026, 7, 31, 10, 0, 0)


def factor_points(start_value, returns):
    points = [FactorPoint("2026-07-01", start_value)]
    value = start_value
    for index, growth_pct in enumerate(returns, start=2):
        value *= 1 + growth_pct / 100.0
        points.append(FactorPoint(f"2026-07-{index:02d}", value))
    return points


class FakeProvider:
    def __init__(self, official=None, nav_by_date=None):
        self.official = official
        self.nav_by_date = nav_by_date or {}

    def get_fund_name(self, code):
        return {"161725": "招商中证白酒指数A"}.get(code, f"基金{code}")

    def get_official_estimate(self, code):
        return self.official

    def get_latest_nav(self, code):
        return LatestNav(nav=1.0, date="2026-07-30")

    def get_nav_by_date(self, code, nav_date):
        return self.nav_by_date.get(nav_date)

    def get_holdings(self, code):
        return [
            Holding(name="贵州茅台", code="600519", weight_pct=30.0),
            Holding(name="五粮液", code="000858", weight_pct=20.0),
        ]

    def get_quotes(self, stock_codes):
        return {
            "600519": Quote(code="600519", name="贵州茅台", change_pct=2.0),
            "000858": Quote(code="000858", name="五粮液", change_pct=-1.0),
        }

    def health(self):
        return {"fake": "ok"}


class BasicInfoOnlyProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.name_calls = 0

    def get_fund_name(self, code):
        self.name_calls += 1
        return "招商中证白酒指数A"

    def get_official_estimate(self, code):
        raise AssertionError("add_fund should not request official estimates")

    def get_latest_nav(self, code):
        raise AssertionError("add_fund should not request latest nav")

    def get_nav_by_date(self, code, nav_date):
        raise AssertionError("add_fund should not request historical nav")

    def get_holdings(self, code):
        raise AssertionError("add_fund should not request holdings")

    def get_quotes(self, stock_codes):
        raise AssertionError("add_fund should not request quotes")


class SnapshotOnlyProvider(FakeProvider):
    def get_fund_name(self, code):
        raise AssertionError("non-trading valuation should not request fund names")

    def get_official_estimate(self, code):
        raise AssertionError("non-trading valuation should not request official estimates")

    def get_latest_nav(self, code):
        raise AssertionError("non-trading valuation should not request latest nav")

    def get_nav_by_date(self, code, nav_date):
        raise AssertionError("non-trading valuation should not request historical nav")

    def get_holdings(self, code):
        raise AssertionError("non-trading valuation should not request holdings")

    def get_quotes(self, stock_codes):
        raise AssertionError("non-trading valuation should not request quotes")


class StoredNameOnlyProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.name_calls = 0

    def get_fund_name(self, code):
        self.name_calls += 1
        return "remote fund"

    def get_official_estimate(self, code):
        return OfficialEstimate(
            nav=1.01,
            growth_pct=1.0,
            estimate_time="2026-07-31 10:30",
        )


class SlowLatestNavProvider(FakeProvider):
    def __init__(self):
        super().__init__(
            official=OfficialEstimate(
                nav=1.01,
                growth_pct=1.0,
                estimate_time="2026-07-31 10:30",
            )
        )
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def get_latest_nav(self, code):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            return LatestNav(nav=1.0, date="2026-07-30")
        finally:
            with self.lock:
                self.active -= 1


class BlockingRefreshProvider(FakeProvider):
    def __init__(self):
        super().__init__(
            official=OfficialEstimate(
                nav=1.02,
                growth_pct=2.0,
                estimate_time="2026-07-31 10:30",
            )
        )
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def get_latest_nav(self, code):
        with self.lock:
            self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return LatestNav(nav=1.0, date="2026-07-30")


class ChangingQuoteProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.change_pct = 1.0
        self.quote_calls = 0

    def get_quotes(self, stock_codes):
        self.quote_calls += 1
        return {
            code: Quote(code=code, name=code, change_pct=self.change_pct)
            for code in stock_codes
        }


class CountingFactorProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.nav_history_calls = 0

    def get_nav_history(self, code, limit=120):
        self.nav_history_calls += 1
        return factor_points(1.0, [1.0] * 24)

    def get_factor_histories(self, limit=120):
        return {
            "sh000300": factor_points(100.0, [1.0] * 24),
        }

    def get_factor_quotes(self, factor_codes):
        return {"sh000300": Quote(code="sh000300", name="CSI 300", change_pct=1.2)}


class UnavailableHoldingCountingFactorProvider(CountingFactorProvider):
    def get_holdings(self, code):
        return []

    def get_quotes(self, stock_codes):
        return {}


class CountingOfficialProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.official_calls = 0

    def get_official_estimate(self, code):
        self.official_calls += 1
        return OfficialEstimate(
            nav=1.02,
            growth_pct=2.0,
            estimate_time="2026-07-31 10:30",
        )


class FundValuationServiceTests(unittest.TestCase):
    def test_add_fund_only_fetches_basic_info(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            provider = BasicInfoOnlyProvider()
            service = FundValuationService(store=store, provider=provider)

            fund = service.add_fund("161725", "白酒")

        self.assertEqual(fund.code, "161725")
        self.assertEqual(fund.alias, "白酒")
        self.assertEqual(fund.name, "招商中证白酒指数A")
        self.assertEqual(provider.name_calls, 1)

    def test_uses_official_estimate_before_holding_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(
                store=store,
                provider=FakeProvider(
                    official=OfficialEstimate(
                        nav=1.2345,
                        growth_pct=1.23,
                        estimate_time="2026-07-31 10:30",
                    )
                ),
            )

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["source"], "official")
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimate_nav"], 1.2345)
        self.assertEqual(result["estimate_growth_pct"], 1.23)
        self.assertEqual(result["latest_nav"], 1.0)
        self.assertEqual(result["latest_nav_date"], "2026-07-30")

    def test_falls_back_to_holding_estimate_when_official_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["source"], "holding")
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(result["coverage_pct"], 50.0)
        self.assertAlmostEqual(result["estimate_growth_pct"], 0.8)

    def test_estimate_uses_stored_fund_name_without_refetching_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", name="stored fund")
            provider = StoredNameOnlyProvider()
            service = FundValuationService(store=store, provider=provider)

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["name"], "stored fund")
        self.assertEqual(provider.name_calls, 0)

    def test_watchlist_estimates_multiple_funds_concurrently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            for code in ("000001", "000002", "000003"):
                store.add_fund(code, name=f"fund {code}")
            provider = SlowLatestNavProvider()
            service = FundValuationService(store=store, provider=provider)

            rows = service.estimate_watchlist(now=TRADING_TIME)

        self.assertEqual([row["code"] for row in rows], ["000001", "000002", "000003"])
        self.assertGreater(provider.max_active, 1)

    def test_cached_watchlist_returns_immediately_while_refresh_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", name="fund 161725")
            provider = BlockingRefreshProvider()
            service = FundValuationService(store=store, provider=provider)

            rows = service.estimate_watchlist_cached(now=TRADING_TIME)
            self.assertEqual(rows, [])
            self.assertTrue(provider.started.wait(timeout=1))

            second_rows = service.estimate_watchlist_cached(now=TRADING_TIME)
            self.assertEqual(second_rows, [])
            self.assertEqual(provider.calls, 1)

            provider.release.set()
            for _ in range(100):
                refreshed = service.estimate_watchlist_cached(now=TRADING_TIME)
                if refreshed:
                    break
                time.sleep(0.01)
            else:
                self.fail("background refresh did not populate cache")

        self.assertEqual(refreshed[0]["code"], "161725")
        self.assertEqual(refreshed[0]["source"], "holding")

    def test_forced_cached_watchlist_returns_refreshed_rows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", name="fund 161725")
            provider = ChangingQuoteProvider()
            service = FundValuationService(store=store, provider=provider)

            first_rows = service.estimate_watchlist_cached(now=TRADING_TIME, force_refresh=True)
            provider.change_pct = 3.0
            cached_rows = service.estimate_watchlist_cached(now=TRADING_TIME)
            refreshed_rows = service.estimate_watchlist_cached(now=TRADING_TIME, force_refresh=True)

        self.assertEqual(len(first_rows), 1)
        self.assertEqual(first_rows[0]["estimate_growth_pct"], 1.0)
        self.assertEqual(cached_rows[0]["estimate_growth_pct"], 1.0)
        self.assertEqual(refreshed_rows[0]["estimate_growth_pct"], 3.0)
        self.assertEqual(provider.quote_calls, 2)

    def test_cached_watchlist_uses_current_close_snapshot_after_market_close(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", "fund", name="fund 161725")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:05:00",
                [
                    {
                        "code": "161725",
                        "name": "fund 161725",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.0888,
                        "estimate_growth_pct": 8.88,
                        "coverage_pct": 70.0,
                        "confidence": 60.0,
                        "reason": None,
                        "latest_nav": 1.0,
                        "latest_nav_date": "2026-07-30",
                        "trade_date": "2026-07-31",
                        "market_phase": "closed",
                        "is_final": True,
                        "contributions": [],
                    }
                ],
            )
            service = FundValuationService(store=store, provider=SnapshotOnlyProvider())

            rows = service.estimate_watchlist_cached(now=datetime(2026, 7, 31, 16, 0, 0))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_key"], "2026-07-31 15:00")
        self.assertAlmostEqual(rows[0]["estimate_growth_pct"], 8.88)
        self.assertEqual(rows[0]["trade_date"], "2026-07-31")

    def test_cached_watchlist_skips_factor_monitoring_for_speed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", name="fund 161725")
            provider = CountingFactorProvider()
            service = FundValuationService(store=store, provider=provider)

            service.estimate_watchlist_cached(now=TRADING_TIME)
            for _ in range(100):
                refreshed = service.estimate_watchlist_cached(now=TRADING_TIME)
                if refreshed:
                    break
                time.sleep(0.01)
            else:
                self.fail("background refresh did not populate cache")

            self.assertEqual(provider.nav_history_calls, 0)
            service.estimate_watchlist(now=TRADING_TIME)

        self.assertGreater(provider.nav_history_calls, 0)

    def test_cached_watchlist_skips_factor_fallback_for_speed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", name="fund 161725")
            provider = UnavailableHoldingCountingFactorProvider()
            service = FundValuationService(store=store, provider=provider)

            service.estimate_watchlist_cached(now=TRADING_TIME)
            for _ in range(100):
                refreshed = service.estimate_watchlist_cached(now=TRADING_TIME)
                if refreshed:
                    break
                time.sleep(0.01)
            else:
                self.fail("background refresh did not populate cache")

            self.assertEqual(refreshed[0]["status"], "unavailable")
            self.assertEqual(provider.nav_history_calls, 0)
            service.estimate_watchlist(now=TRADING_TIME)

        self.assertGreater(provider.nav_history_calls, 0)

    def test_cached_watchlist_populates_timeout_rows_when_live_refresh_is_slow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", name="fund 161725")
            provider = BlockingRefreshProvider()
            service = FundValuationService(
                store=store,
                provider=provider,
                live_refresh_timeout_seconds=0.01,
            )

            service.estimate_watchlist_cached(now=TRADING_TIME)
            self.assertTrue(provider.started.wait(timeout=1))
            for _ in range(100):
                refreshed = service.estimate_watchlist_cached(now=TRADING_TIME)
                if refreshed:
                    break
                time.sleep(0.01)
            else:
                self.fail("background refresh did not populate timeout cache")
            provider.release.set()

        self.assertEqual(refreshed[0]["status"], "unavailable")
        self.assertEqual(refreshed[0]["source"], "refresh")
        self.assertEqual(refreshed[0]["reason"], "refresh_timeout")

    def test_cached_watchlist_skips_official_estimate_for_speed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", name="fund 161725")
            provider = CountingOfficialProvider()
            service = FundValuationService(store=store, provider=provider)

            service.estimate_watchlist_cached(now=TRADING_TIME)
            for _ in range(100):
                refreshed = service.estimate_watchlist_cached(now=TRADING_TIME)
                if refreshed:
                    break
                time.sleep(0.01)
            else:
                self.fail("background refresh did not populate cache")

            self.assertEqual(refreshed[0]["source"], "holding")
            self.assertEqual(provider.official_calls, 0)
            full_rows = service.estimate_watchlist(now=TRADING_TIME)

        self.assertEqual(full_rows[0]["source"], "official")
        self.assertGreater(provider.official_calls, 0)

    def test_non_trading_estimate_calculates_when_previous_close_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", "白酒", name="招商中证白酒指数A")
            service = FundValuationService(store=store, provider=FakeProvider())

            result = service.estimate_watchlist(now=datetime(2026, 8, 1, 10, 0, 0))[0]
            status = service.trading_status(datetime(2026, 8, 1, 10, 0, 0))

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["source"], "holding")
        self.assertAlmostEqual(result["estimate_growth_pct"], 0.8)
        self.assertNotIn("snapshot_key", result)
        self.assertEqual(result["trade_date"], "2026-07-31")
        self.assertEqual(result["market_phase"], "non_trading")
        self.assertTrue(result["is_final"])
        self.assertEqual(status["trade_date"], "2026-07-31")
        self.assertTrue(status["is_final"])

    def test_manual_snapshot_recalculates_when_non_trading_snapshot_cache_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "白酒")

            snapshot = service.create_snapshot(now=datetime(2026, 8, 1, 10, 0, 0))
            rows = service.get_snapshot("2026-07-31 15:00")

        self.assertEqual(snapshot["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(rows[0]["source"], "holding")
        self.assertAlmostEqual(rows[0]["estimate_growth_pct"], 0.8)

    def test_non_trading_estimate_reuses_previous_close_snapshot_without_recalculation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", "白酒", name="招商中证白酒指数A")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:00:00",
                [
                    {
                        "code": "161725",
                        "name": "招商中证白酒指数A",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.008,
                        "estimate_growth_pct": 0.8,
                        "coverage_pct": 50.0,
                        "confidence": 50.0,
                        "reason": None,
                        "latest_nav": 1.0,
                        "latest_nav_date": "2026-07-30",
                        "trade_date": "2026-07-31",
                        "market_phase": "closed",
                        "is_final": True,
                        "contributions": [],
                    }
                ],
            )
            service = FundValuationService(store=store, provider=SnapshotOnlyProvider())

            watchlist = service.estimate_watchlist(now=datetime(2026, 8, 1, 10, 0, 0))
            single = service.estimate_fund("161725", now=datetime(2026, 8, 1, 10, 0, 0))

        result = watchlist[0]
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["source"], "holding")
        self.assertEqual(result["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(result["trade_date"], "2026-07-31")
        self.assertEqual(result["market_phase"], "non_trading")
        self.assertTrue(result["is_final"])
        self.assertAlmostEqual(result["estimate_growth_pct"], 0.8)
        self.assertAlmostEqual(single["estimate_nav"], 1.008)
        self.assertEqual(single["snapshot_key"], "2026-07-31 15:00")

    def test_non_trading_estimate_calculates_funds_missing_from_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", "白酒", name="招商中证白酒指数A")
            store.add_fund("000001", "补算", name="基金000001")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:00:00",
                [
                    {
                        "code": "161725",
                        "name": "招商中证白酒指数A",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.008,
                        "estimate_growth_pct": 0.8,
                        "coverage_pct": 50.0,
                        "confidence": 50.0,
                        "reason": None,
                        "latest_nav": 1.0,
                        "latest_nav_date": "2026-07-30",
                        "trade_date": "2026-07-31",
                        "market_phase": "closed",
                        "is_final": True,
                        "contributions": [],
                    }
                ],
            )
            service = FundValuationService(store=store, provider=FakeProvider())

            results = service.estimate_watchlist(now=datetime(2026, 8, 1, 10, 0, 0))

        by_code = {item["code"]: item for item in results}
        self.assertEqual(by_code["161725"]["snapshot_key"], "2026-07-31 15:00")
        self.assertNotIn("snapshot_key", by_code["000001"])
        self.assertEqual(by_code["000001"]["source"], "holding")
        self.assertAlmostEqual(by_code["000001"]["estimate_growth_pct"], 0.8)

    def test_non_trading_estimate_recalculates_unusable_snapshot_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", "白酒", name="招商中证白酒指数A")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:00:00",
                [
                    {
                        "code": "161725",
                        "name": "招商中证白酒指数A",
                        "status": "unavailable",
                        "source": "holding",
                        "estimate_nav": None,
                        "estimate_growth_pct": None,
                        "coverage_pct": 0.0,
                        "confidence": 0.0,
                        "reason": "no_quotes",
                        "trade_date": "2026-07-31",
                    }
                ],
            )
            service = FundValuationService(store=store, provider=FakeProvider())

            result = service.estimate_fund("161725", now=datetime(2026, 8, 1, 10, 0, 0))

        self.assertNotIn("snapshot_key", result)
        self.assertEqual(result["source"], "holding")
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(result["estimate_growth_pct"], 0.8)

    def test_estimate_uses_quote_trade_date_when_source_data_is_stale(self):
        class StaleQuoteProvider(FakeProvider):
            def get_quotes(self, stock_codes):
                return {
                    "600519": Quote(
                        code="600519",
                        name="贵州茅台",
                        change_pct=2.0,
                        trade_date="2026-07-31",
                    ),
                    "000858": Quote(
                        code="000858",
                        name="五粮液",
                        change_pct=-1.0,
                        trade_date="2026-07-31",
                    ),
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=StaleQuoteProvider())

            result = service.estimate_fund("161725", now=datetime(2026, 8, 3, 10, 0, 0))

        self.assertEqual(result["trade_date"], "2026-07-31")
        self.assertEqual(result["market_phase"], "closed")
        self.assertTrue(result["is_final"])

    def test_uses_published_nav_when_trade_date_nav_is_available(self):
        class PublishedNavProvider(FakeProvider):
            def get_latest_nav(self, code):
                return LatestNav(nav=1.009, date="2026-07-31")

            def get_nav_by_date(self, code, nav_date):
                if nav_date == "2026-07-30":
                    return LatestNav(nav=1.0, date="2026-07-30")
                return None

            def get_holdings(self, code):
                raise AssertionError("published NAV should skip holding fallback")

            def get_quotes(self, stock_codes):
                raise AssertionError("published NAV should skip quote fallback")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=PublishedNavProvider())

            result = service.estimate_fund("161725", now=datetime(2026, 7, 31, 15, 30, 0))

        self.assertEqual(result["source"], "nav")
        self.assertEqual(result["trade_date"], "2026-07-31")
        self.assertAlmostEqual(result["estimate_nav"], 1.009)
        self.assertAlmostEqual(result["estimate_growth_pct"], 0.9)
        self.assertAlmostEqual(result["actual_nav"], 1.009)
        self.assertEqual(result["latest_nav_date"], "2026-07-30")
        self.assertTrue(result["is_final"])

    def test_qdii_uses_previous_trade_date_nav_during_trading(self):
        class QdiiPublishedNavProvider(FakeProvider):
            def get_fund_name(self, code):
                return "华夏全球精选QDII"

            def get_latest_nav(self, code):
                return LatestNav(nav=1.052, date="2026-07-30")

            def get_nav_by_date(self, code, nav_date):
                if nav_date == "2026-07-29":
                    return LatestNav(nav=1.04, date="2026-07-29")
                return None

            def get_official_estimate(self, code):
                raise AssertionError("published QDII NAV should skip official estimate")

            def get_holdings(self, code):
                raise AssertionError("published QDII NAV should skip holding fallback")

            def get_quotes(self, stock_codes):
                raise AssertionError("published QDII NAV should skip quote fallback")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=QdiiPublishedNavProvider())

            result = service.estimate_fund("000041", now=TRADING_TIME)

        self.assertEqual(result["source"], "nav")
        self.assertEqual(result["trade_date"], "2026-07-30")
        self.assertEqual(result["target_trade_date"], "2026-07-30")
        self.assertEqual(result["context_trade_date"], "2026-07-31")
        self.assertEqual(result["market_phase"], "closed")
        self.assertTrue(result["is_final"])
        self.assertAlmostEqual(result["estimate_nav"], 1.052)
        self.assertAlmostEqual(result["actual_nav"], 1.052)
        self.assertEqual(result["actual_nav_date"], "2026-07-30")
        self.assertAlmostEqual(result["latest_nav"], 1.04)
        self.assertEqual(result["latest_nav_date"], "2026-07-29")

    def test_qdii_nasdaq_uses_overseas_benchmark_and_fx_before_holdings(self):
        case = self

        class QdiiBenchmarkProvider(FakeProvider):
            def get_fund_name(self, code):
                return "建信纳斯达克100指数(QDII)A人民币"

            def get_latest_nav(self, code):
                return LatestNav(nav=3.3217, date="2026-07-31")

            def get_nav_by_date(self, code, nav_date):
                return None

            def get_official_estimate(self, code):
                return None

            def get_qdii_benchmark_quote(self, code, fund_name=None, from_date=None, to_date=None):
                case.assertEqual(code, "539001")
                case.assertEqual(fund_name, "建信纳斯达克100指数(QDII)A人民币")
                case.assertEqual(from_date, "2026-07-31")
                case.assertEqual(to_date, "2026-08-03")
                return {
                    "source": "qdii_benchmark",
                    "benchmark_symbol": "QQQ",
                    "benchmark_name": "纳斯达克100/QQQ",
                    "fx_symbol": "USDCNYC",
                    "benchmark_growth_pct": 1.76,
                    "fx_growth_pct": -0.16,
                    "change_pct": 1.5972,
                    "benchmark_start_date": "2026-07-31",
                    "benchmark_end_date": "2026-08-03",
                    "fx_start_date": "2026-07-31",
                    "fx_end_date": "2026-08-03",
                }

            def get_holdings(self, code):
                raise AssertionError("QDII benchmark should skip holding fallback")

            def get_quotes(self, stock_codes):
                raise AssertionError("QDII benchmark should skip quote fallback")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=QdiiBenchmarkProvider())

            result = service.estimate_fund("539001", now=datetime(2026, 8, 4, 10, 0, 0))

        self.assertEqual(result["source"], "qdii_benchmark")
        self.assertEqual(result["trade_date"], "2026-08-03")
        self.assertEqual(result["target_trade_date"], "2026-08-03")
        self.assertEqual(result["context_trade_date"], "2026-08-04")
        self.assertEqual(result["market_phase"], "closed")
        self.assertTrue(result["is_final"])
        self.assertAlmostEqual(result["estimate_growth_pct"], 1.5972)
        self.assertAlmostEqual(result["estimate_nav"], 3.374754)
        self.assertAlmostEqual(result["latest_nav"], 3.3217)
        self.assertEqual(result["latest_nav_date"], "2026-07-31")
        self.assertEqual(result["benchmark_symbol"], "QQQ")
        self.assertEqual(result["fx_symbol"], "USDCNYC")
        self.assertAlmostEqual(result["benchmark_growth_pct"], 1.76)
        self.assertAlmostEqual(result["fx_growth_pct"], -0.16)

    def test_etf_link_uses_lower_coverage_floor_for_holding_estimate(self):
        class EtfProvider(FakeProvider):
            def get_fund_name(self, code):
                return "天弘中证银行ETF联接C"

            def get_holdings(self, code):
                return [Holding(name="招商银行", code="600036", weight_pct=0.39)]

            def get_quotes(self, stock_codes):
                return {"600036": Quote(code="600036", name="招商银行", change_pct=-2.0)}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=EtfProvider())

            result = service.estimate_fund("001595", now=TRADING_TIME)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["source"], "holding")
        self.assertAlmostEqual(result["coverage_pct"], 0.39)
        self.assertAlmostEqual(result["estimate_growth_pct"], -2.0)

    def test_low_coverage_holding_falls_back_to_factor_fit(self):
        class FactorFallbackProvider(FakeProvider):
            def get_holdings(self, code):
                return [Holding(name="Tiny Holding", code="600519", weight_pct=5.0)]

            def get_quotes(self, stock_codes):
                return {"600519": Quote(code="600519", name="Tiny Holding", change_pct=1.0)}

            def get_nav_history(self, code, limit=120):
                return factor_points(1.0, [1.0] * 24)

            def get_factor_histories(self, limit=120):
                return {
                    "sh000300": factor_points(100.0, [1.0] * 24),
                }

            def get_factor_quotes(self, factor_codes):
                return {"sh000300": Quote(code="sh000300", name="CSI 300", change_pct=1.5)}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FactorFallbackProvider())

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["source"], "factor_fit")
        self.assertAlmostEqual(result["estimate_growth_pct"], 1.5, places=3)
        self.assertAlmostEqual(result["estimate_nav"], 1.015, places=3)
        self.assertAlmostEqual(result["fit_r2"], 1.0, places=3)
        self.assertIn("factor_exposures", result)

    def test_holding_estimate_blends_uncovered_weight_with_factor_fit(self):
        class FactorBlendProvider(FakeProvider):
            def get_nav_history(self, code, limit=120):
                return factor_points(1.0, [1.0] * 24)

            def get_factor_histories(self, limit=120):
                return {
                    "sh000300": factor_points(100.0, [1.0] * 24),
                }

            def get_factor_quotes(self, factor_codes):
                return {"sh000300": Quote(code="sh000300", name="CSI 300", change_pct=1.2)}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FactorBlendProvider())

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["source"], "holding")
        self.assertAlmostEqual(result["raw_holding_estimate_growth_pct"], 0.8)
        self.assertAlmostEqual(result["covered_contribution_pct"], 0.4)
        self.assertAlmostEqual(result["uncovered_weight_pct"], 50.0)
        self.assertEqual(result["uncovered_proxy_source"], "factor_fit")
        self.assertAlmostEqual(result["uncovered_proxy_growth_pct"], 1.2)
        self.assertAlmostEqual(result["estimate_growth_pct"], 1.0)
        self.assertAlmostEqual(result["estimate_nav"], 1.01)

    def test_active_holding_estimate_blends_uncovered_weight_with_holding_momentum(self):
        class ActiveMomentumProvider(FakeProvider):
            def get_fund_name(self, code):
                return "信澳业绩驱动混合A"

            def get_holdings(self, code):
                return [
                    Holding(name="A", code="300001", weight_pct=15.0),
                    Holding(name="B", code="300002", weight_pct=15.0),
                    Holding(name="C", code="300003", weight_pct=15.0),
                    Holding(name="D", code="300004", weight_pct=15.0),
                ]

            def get_quotes(self, stock_codes):
                return {
                    code: Quote(code=code, name=code, change_pct=10.0)
                    for code in stock_codes
                }

            def get_nav_history(self, code, limit=120):
                return factor_points(1.0, [1.0] * 24)

            def get_factor_histories(self, limit=120):
                return {
                    "sh000300": factor_points(100.0, [1.0] * 24),
                }

            def get_factor_quotes(self, factor_codes):
                return {"sh000300": Quote(code="sh000300", name="CSI 300", change_pct=1.0)}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=ActiveMomentumProvider())

            result = service.estimate_fund("016370", now=TRADING_TIME)

        self.assertEqual(result["source"], "holding")
        self.assertAlmostEqual(result["raw_holding_estimate_growth_pct"], 10.0)
        self.assertAlmostEqual(result["covered_contribution_pct"], 6.0)
        self.assertAlmostEqual(result["uncovered_weight_pct"], 40.0)
        self.assertEqual(result["uncovered_proxy_source"], "holding_momentum_blend")
        self.assertAlmostEqual(result["holding_momentum_growth_pct"], 10.0)
        self.assertAlmostEqual(result["uncovered_proxy_growth_pct"], 6.85)
        self.assertAlmostEqual(result["fit_growth_pct"], 1.0, places=3)
        self.assertAlmostEqual(result["estimate_growth_pct"], 8.74)

    def test_etf_link_blends_uncovered_weight_with_tracking_index_before_factor_fit(self):
        class TrackingIndexProvider(FakeProvider):
            def get_fund_name(self, code):
                return "天弘中证银行ETF联接C"

            def get_holdings(self, code):
                return [Holding(name="招商银行", code="600036", weight_pct=1.19)]

            def get_quotes(self, stock_codes):
                return {"600036": Quote(code="600036", name="招商银行", change_pct=-2.8)}

            def get_nav_history(self, code, limit=120):
                return factor_points(1.0, [1.0] * 24)

            def get_factor_histories(self, limit=120):
                return {
                    "sh000300": factor_points(100.0, [1.0] * 24),
                }

            def get_factor_quotes(self, factor_codes):
                return {"sh000300": Quote(code="sh000300", name="CSI 300", change_pct=-1.3)}

            def get_tracking_index_quote(self, code, fund_name=None):
                return Quote(code="sz399986", name="中证银行", change_pct=-2.56)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=TrackingIndexProvider())

            result = service.estimate_fund("001595", now=TRADING_TIME)

        self.assertEqual(result["source"], "holding")
        self.assertAlmostEqual(result["raw_holding_estimate_growth_pct"], -2.8)
        self.assertAlmostEqual(result["covered_contribution_pct"], -0.0333)
        self.assertAlmostEqual(result["uncovered_weight_pct"], 98.81)
        self.assertEqual(result["uncovered_proxy_source"], "tracking_index")
        self.assertEqual(result["uncovered_proxy_name"], "中证银行")
        self.assertAlmostEqual(result["uncovered_proxy_growth_pct"], -2.56)
        self.assertAlmostEqual(result["fit_growth_pct"], -1.3, places=3)
        self.assertAlmostEqual(result["estimate_growth_pct"], -2.5628)

    def test_holding_estimate_marks_high_risk_and_reduces_confidence_for_volatile_holdings(self):
        class VolatileHoldingProvider(FakeProvider):
            def get_holdings(self, code):
                return [
                    Holding(name="A", code="600519", weight_pct=40.0),
                    Holding(name="B", code="000858", weight_pct=20.0),
                ]

            def get_quotes(self, stock_codes):
                return {
                    "600519": Quote(code="600519", name="A", change_pct=-12.0),
                    "000858": Quote(code="000858", name="B", change_pct=-8.0),
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=VolatileHoldingProvider())

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["estimate_risk_level"], "high")
        self.assertIn("volatile_holdings", result["estimate_risk_reasons"])
        self.assertAlmostEqual(result["pre_risk_confidence"], 60.0)
        self.assertAlmostEqual(result["confidence"], 45.0)

    def test_holding_estimate_includes_factor_fit_monitoring_fields(self):
        class FactorMonitorProvider(FakeProvider):
            def get_nav_history(self, code, limit=120):
                return factor_points(1.0, [1.0] * 24)

            def get_factor_histories(self, limit=120):
                return {
                    "sh000300": factor_points(100.0, [1.0] * 24),
                }

            def get_factor_quotes(self, factor_codes):
                return {"sh000300": Quote(code="sh000300", name="CSI 300", change_pct=1.2)}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FactorMonitorProvider())

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["source"], "holding")
        self.assertAlmostEqual(result["fit_growth_pct"], 1.2, places=3)
        self.assertAlmostEqual(result["fit_nav"], 1.012, places=3)
        self.assertIn("style_drift_score", result)
        self.assertIn("factor_exposures", result)

    def test_creates_and_lists_snapshot_batches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "白酒")

            batch = service.create_snapshot(snapshot_key="2026-07-31 15:00", captured_at="2026-07-31 15:00:00")
            batches = service.list_snapshots()
            rows = service.get_snapshot("2026-07-31 15:00")

        self.assertEqual(batch["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(batch["count"], 1)
        self.assertEqual(batches[0]["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(rows[0]["code"], "161725")
        self.assertAlmostEqual(rows[0]["estimate_growth_pct"], 0.8)

    def test_reconciles_snapshots_against_official_nav_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(
                store=store,
                provider=FakeProvider(nav_by_date={"2026-07-31": LatestNav(nav=1.009, date="2026-07-31")}),
            )
            service.add_fund("161725", "fund")
            service.create_snapshot(snapshot_key="2026-07-31 15:00", captured_at="2026-07-31 15:00:00")

            result = service.reconcile_snapshots(now=datetime(2026, 8, 1, 8, 0, 0))
            profile = store.get_reconciliation_profile("161725", source="holding")
            rows = service.get_snapshot("2026-07-31 15:00")

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(profile["sample_count"], 1)
        self.assertAlmostEqual(profile["mean_abs_growth_error_pct"], 0.1)
        self.assertAlmostEqual(rows[0]["actual_nav"], 1.009)
        self.assertEqual(rows[0]["actual_nav_date"], "2026-07-31")

    def test_lists_reconciliation_records_for_display(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(
                store=store,
                provider=FakeProvider(nav_by_date={"2026-07-31": LatestNav(nav=1.009, date="2026-07-31")}),
            )
            service.add_fund("161725", "fund")
            service.create_snapshot(snapshot_key="2026-07-31 15:00", captured_at="2026-07-31 15:00:00")
            service.reconcile_snapshots(now=datetime(2026, 8, 1, 8, 0, 0))

            records = service.list_reconciliations()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["code"], "161725")
        self.assertEqual(records[0]["snapshot_date"], "2026-07-31")
        self.assertAlmostEqual(records[0]["abs_growth_error_pct"], 0.1)

    def test_due_reconciliation_retries_skipped_snapshots_after_interval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            provider = FakeProvider(nav_by_date={})
            service = FundValuationService(store=store, provider=provider)
            service.add_fund("161725", "fund")
            service.create_snapshot(snapshot_key="2026-07-31 15:00", captured_at="2026-07-31 15:00:00")

            early = service.reconcile_due_snapshots(now=datetime(2026, 7, 31, 16, 0, 0))
            provider.nav_by_date["2026-07-31"] = LatestNav(nav=1.009, date="2026-07-31")
            too_soon = service.reconcile_due_snapshots(now=datetime(2026, 7, 31, 16, 29, 59))
            retried = service.reconcile_due_snapshots(now=datetime(2026, 7, 31, 16, 30, 0))
            rows = service.get_snapshot("2026-07-31 15:00")

        self.assertEqual(early["checked"], 1)
        self.assertEqual(early["reconciled"], 0)
        self.assertEqual(early["skipped"], 1)
        self.assertIsNone(too_soon)
        self.assertEqual(retried["checked"], 1)
        self.assertEqual(retried["reconciled"], 1)
        self.assertAlmostEqual(rows[0]["actual_nav"], 1.009)

    def test_estimate_uses_reconciliation_profile_to_calibrate_confidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            for index in range(30):
                store.save_reconciliation(
                    {
                        "snapshot_key": f"2026-06-{index + 1:02d} 15:00",
                        "snapshot_date": f"2026-06-{index + 1:02d}",
                        "code": "161725",
                        "source": "holding",
                        "estimate_nav": 1.01,
                        "estimate_growth_pct": 1.0,
                        "latest_nav": 1.0,
                        "actual_nav": 1.008,
                        "actual_nav_date": f"2026-06-{index + 1:02d}",
                        "actual_growth_pct": 0.8,
                        "nav_error_pct": 0.2,
                        "abs_nav_error_pct": 0.2,
                        "growth_error_pct": 0.2,
                        "abs_growth_error_pct": 0.2,
                        "reconciled_at": "2026-07-01 08:00:00",
                    }
                )
            service = FundValuationService(store=store, provider=FakeProvider())

            result = service.estimate_fund("161725", now=TRADING_TIME)

        self.assertEqual(result["pre_risk_confidence"], 50.0)
        self.assertEqual(result["base_confidence"], 45.0)
        self.assertGreater(result["confidence"], 50.0)
        self.assertEqual(result["confidence_profile"]["sample_count"], 30)
        self.assertAlmostEqual(result["raw_estimate_growth_pct"], 0.8)
        self.assertAlmostEqual(result["estimate_growth_pct"], 0.68)
        self.assertEqual(result["adjustment_source"], "historical_reconciliation")

    def test_due_snapshot_repairs_existing_incomplete_batch_after_retry_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "stale")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:00:00",
                [
                    {
                        "code": "161725",
                        "name": "stale",
                        "status": "unavailable",
                        "source": "holding",
                        "estimate_nav": None,
                        "estimate_growth_pct": None,
                        "coverage_pct": 0.0,
                        "confidence": 0.0,
                        "reason": "no_holdings",
                        "latest_nav": 1.0,
                        "latest_nav_date": "2026-07-30",
                        "contributions": [],
                    }
                ],
            )

            too_soon = service.create_due_snapshot(datetime(2026, 7, 31, 15, 9, 59))
            repaired = service.create_due_snapshot(datetime(2026, 7, 31, 15, 10, 0))
            rows = service.get_snapshot("2026-07-31 15:00")

        self.assertIsNone(too_soon)
        self.assertEqual(repaired["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(rows[0]["status"], "estimated")
        self.assertAlmostEqual(rows[0]["estimate_growth_pct"], 0.8)

    def test_due_snapshot_runs_once_after_1505(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "白酒")

            before = service.create_due_snapshot(datetime(2026, 7, 31, 14, 59, 0))
            at_close = service.create_due_snapshot(datetime(2026, 7, 31, 15, 0, 0))
            before_save = service.create_due_snapshot(datetime(2026, 7, 31, 15, 4, 59))
            first = service.create_due_snapshot(datetime(2026, 7, 31, 15, 5, 0))
            second = service.create_due_snapshot(datetime(2026, 7, 31, 15, 6, 0))

        self.assertIsNone(before)
        self.assertIsNone(at_close)
        self.assertIsNone(before_save)
        self.assertEqual(first["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(first["snapshot_date"], "2026-07-31")
        self.assertIsNone(second)

    def test_manual_snapshot_on_weekend_uses_previous_trade_date_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "白酒")

            snapshot = service.create_snapshot(now=datetime(2026, 8, 1, 10, 0, 0))
            rows = service.get_snapshot("2026-07-31 15:00")

        self.assertEqual(snapshot["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(snapshot["snapshot_date"], "2026-07-31")
        self.assertEqual(rows[0]["snapshot_date"], "2026-07-31")
        self.assertEqual(rows[0]["trade_date"], "2026-07-31")

    def test_due_snapshot_skips_non_trading_days(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "白酒")

            saturday = service.create_due_snapshot(datetime(2026, 8, 1, 15, 0, 0))
            sunday = service.create_due_snapshot(datetime(2026, 8, 2, 15, 0, 0))

        self.assertIsNone(saturday)
        self.assertIsNone(sunday)

    def test_refresh_window_extends_until_snapshot_save_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())

            before_snapshot = service.trading_status(datetime(2026, 7, 31, 15, 4, 0))
            after_snapshot = service.trading_status(datetime(2026, 7, 31, 15, 6, 0))

        self.assertTrue(before_snapshot["is_refresh_window"])
        self.assertFalse(after_snapshot["is_refresh_window"])

    def test_trading_day_uses_provider_calendar_when_available(self):
        class CalendarProvider(FakeProvider):
            def is_trading_day(self, current):
                return str(current) == "2026-07-31"

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=CalendarProvider())
            service.add_fund("161725", "白酒")

            trading_day = service.create_due_snapshot(datetime(2026, 7, 31, 15, 5, 0))
            weekday_holiday = service.create_due_snapshot(datetime(2026, 8, 3, 15, 0, 0))
            status = service.trading_status(datetime(2026, 8, 3, 10, 0, 0))

        self.assertEqual(trading_day["snapshot_date"], "2026-07-31")
        self.assertIsNone(weekday_holiday)
        self.assertFalse(status["is_trading_day"])
        self.assertFalse(status["is_refresh_window"])


if __name__ == "__main__":
    unittest.main()
