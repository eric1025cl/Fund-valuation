import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fundval.service import FundValuationService
from fundval.store import WatchlistStore
from fundval.valuation import Holding, LatestNav, OfficialEstimate, Quote


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


class FundValuationServiceTests(unittest.TestCase):
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

            result = service.estimate_fund("161725")

        self.assertEqual(result["source"], "official")
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimate_nav"], 1.2345)
        self.assertEqual(result["estimate_growth_pct"], 1.23)

    def test_falls_back_to_holding_estimate_when_official_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())

            result = service.estimate_fund("161725")

        self.assertEqual(result["source"], "holding")
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(result["coverage_pct"], 50.0)
        self.assertAlmostEqual(result["estimate_growth_pct"], 0.8)

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

            result = service.estimate_fund("001595")

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["source"], "holding")
        self.assertAlmostEqual(result["coverage_pct"], 0.39)
        self.assertAlmostEqual(result["estimate_growth_pct"], -2.0)

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

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(profile["sample_count"], 1)
        self.assertAlmostEqual(profile["mean_abs_growth_error_pct"], 0.1)

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

            result = service.estimate_fund("161725")

        self.assertEqual(result["base_confidence"], 50.0)
        self.assertGreater(result["confidence"], 50.0)
        self.assertEqual(result["confidence_profile"]["sample_count"], 30)

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

    def test_due_snapshot_runs_once_after_15(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "白酒")

            before = service.create_due_snapshot(datetime(2026, 7, 31, 14, 59, 0))
            first = service.create_due_snapshot(datetime(2026, 7, 31, 15, 0, 0))
            second = service.create_due_snapshot(datetime(2026, 7, 31, 15, 1, 0))

        self.assertIsNone(before)
        self.assertEqual(first["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(first["snapshot_date"], "2026-07-31")
        self.assertIsNone(second)

    def test_due_snapshot_skips_non_trading_days(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=FakeProvider())
            service.add_fund("161725", "白酒")

            saturday = service.create_due_snapshot(datetime(2026, 8, 1, 15, 0, 0))
            sunday = service.create_due_snapshot(datetime(2026, 8, 2, 15, 0, 0))

        self.assertIsNone(saturday)
        self.assertIsNone(sunday)

    def test_trading_day_uses_provider_calendar_when_available(self):
        class CalendarProvider(FakeProvider):
            def is_trading_day(self, current):
                return str(current) == "2026-07-31"

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            service = FundValuationService(store=store, provider=CalendarProvider())
            service.add_fund("161725", "白酒")

            trading_day = service.create_due_snapshot(datetime(2026, 7, 31, 15, 0, 0))
            weekday_holiday = service.create_due_snapshot(datetime(2026, 8, 3, 15, 0, 0))
            status = service.trading_status(datetime(2026, 8, 3, 10, 0, 0))

        self.assertEqual(trading_day["snapshot_date"], "2026-07-31")
        self.assertIsNone(weekday_holiday)
        self.assertFalse(status["is_trading_day"])
        self.assertFalse(status["is_refresh_window"])


if __name__ == "__main__":
    unittest.main()
