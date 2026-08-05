import tempfile
import unittest
from pathlib import Path

from fundval.store import WatchlistStore, normalize_fund_code


class WatchlistStoreTests(unittest.TestCase):
    def test_normalizes_fund_code_to_six_digits(self):
        self.assertEqual(normalize_fund_code("123"), "000123")
        self.assertEqual(normalize_fund_code(" 161725 "), "161725")

        with self.assertRaises(ValueError):
            normalize_fund_code("abc123")

    def test_persists_watchlist_entries_and_updates_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", "招商白酒")
            store.add_fund("161725", "白酒指数")

            funds = store.list_funds()

        self.assertEqual(len(funds), 1)
        self.assertEqual(funds[0].code, "161725")
        self.assertEqual(funds[0].alias, "白酒指数")

    def test_deletes_watchlist_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.add_fund("161725", "招商白酒")
            deleted = store.delete_fund("161725")
            funds = store.list_funds()

        self.assertTrue(deleted)
        self.assertEqual(funds, [])

    def test_lists_unreconciled_valuation_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:00:00",
                [
                    {
                        "code": "161725",
                        "name": "fund 161725",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.008,
                        "estimate_growth_pct": 0.8,
                        "coverage_pct": 50.0,
                        "confidence": 50.0,
                        "latest_nav": 1.0,
                        "latest_nav_date": "2026-07-30",
                    },
                    {
                        "code": "000001",
                        "name": "fund 000001",
                        "status": "unavailable",
                        "source": "holding",
                        "estimate_nav": None,
                        "estimate_growth_pct": None,
                    },
                ],
            )

            rows = store.list_unreconciled_valuations()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(rows[0]["snapshot_date"], "2026-07-31")
        self.assertEqual(rows[0]["code"], "161725")

    def test_snapshot_date_prefers_payload_trade_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            saved = store.save_snapshot(
                "2026-08-01 10:00",
                "2026-08-01 10:00:00",
                [
                    {
                        "code": "161725",
                        "name": "fund 161725",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.008,
                        "estimate_growth_pct": 0.8,
                        "coverage_pct": 50.0,
                        "confidence": 50.0,
                        "latest_nav": 1.0,
                        "latest_nav_date": "2026-07-30",
                        "trade_date": "2026-07-31",
                    },
                ],
            )
            snapshots = store.list_snapshots()
            rows = store.list_unreconciled_valuations()

        self.assertEqual(saved["snapshot_date"], "2026-07-31")
        self.assertEqual(snapshots[0]["snapshot_date"], "2026-07-31")
        self.assertEqual(rows[0]["snapshot_date"], "2026-07-31")

    def test_delete_snapshot_physically_removes_batch_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:00:00",
                [
                    {
                        "code": "161725",
                        "name": "fund 161725",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.008,
                        "estimate_growth_pct": 0.8,
                    },
                ],
            )
            store.save_snapshot(
                "2026-08-03 15:00",
                "2026-08-03 15:00:00",
                [
                    {
                        "code": "000001",
                        "name": "fund 000001",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.102,
                        "estimate_growth_pct": 1.2,
                    },
                ],
            )

            deleted_count = store.delete_snapshot("2026-07-31 15:00")
            deleted_again = store.delete_snapshot("2026-07-31 15:00")
            deleted_rows = store.get_snapshot("2026-07-31 15:00")
            remaining_rows = store.get_snapshot("2026-08-03 15:00")
            snapshots = store.list_snapshots()

        self.assertEqual(deleted_count, 1)
        self.assertEqual(deleted_again, 0)
        self.assertEqual(deleted_rows, [])
        self.assertEqual(remaining_rows[0]["code"], "000001")
        self.assertEqual([item["snapshot_key"] for item in snapshots], ["2026-08-03 15:00"])

    def test_reconciliation_backfills_snapshot_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.save_snapshot(
                "2026-07-31 15:00",
                "2026-07-31 15:00:00",
                [
                    {
                        "code": "161725",
                        "name": "fund 161725",
                        "status": "estimated",
                        "source": "holding",
                        "estimate_nav": 1.008,
                        "estimate_growth_pct": 0.8,
                        "coverage_pct": 50.0,
                        "confidence": 50.0,
                        "latest_nav": 1.0,
                        "latest_nav_date": "2026-07-30",
                    },
                ],
            )

            store.save_reconciliation(
                {
                    "snapshot_key": "2026-07-31 15:00",
                    "snapshot_date": "2026-07-31",
                    "code": "161725",
                    "source": "holding",
                    "estimate_nav": 1.008,
                    "estimate_growth_pct": 0.8,
                    "latest_nav": 1.0,
                    "actual_nav": 1.009,
                    "actual_nav_date": "2026-07-31",
                    "actual_growth_pct": 0.9,
                    "nav_error_pct": -0.099108,
                    "abs_nav_error_pct": 0.099108,
                    "growth_error_pct": -0.1,
                    "abs_growth_error_pct": 0.1,
                    "reconciled_at": "2026-08-01 08:00:00",
                }
            )
            rows = store.get_snapshot("2026-07-31 15:00")

        self.assertAlmostEqual(rows[0]["actual_nav"], 1.009)
        self.assertEqual(rows[0]["actual_nav_date"], "2026-07-31")
        self.assertAlmostEqual(rows[0]["growth_error_pct"], -0.1)

    def test_profiles_recent_reconciliation_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.save_reconciliation(
                {
                    "snapshot_key": "2026-07-30 15:00",
                    "snapshot_date": "2026-07-30",
                    "code": "161725",
                    "source": "holding",
                    "estimate_nav": 1.01,
                    "estimate_growth_pct": 1.0,
                    "latest_nav": 1.0,
                    "actual_nav": 1.009,
                    "actual_nav_date": "2026-07-30",
                    "actual_growth_pct": 0.9,
                    "nav_error_pct": 0.1,
                    "abs_nav_error_pct": 0.1,
                    "growth_error_pct": 0.1,
                    "abs_growth_error_pct": 0.1,
                    "reconciled_at": "2026-07-31 08:00:00",
                }
            )
            store.save_reconciliation(
                {
                    "snapshot_key": "2026-07-31 15:00",
                    "snapshot_date": "2026-07-31",
                    "code": "161725",
                    "source": "holding",
                    "estimate_nav": 0.998,
                    "estimate_growth_pct": -0.2,
                    "latest_nav": 1.0,
                    "actual_nav": 1.001,
                    "actual_nav_date": "2026-07-31",
                    "actual_growth_pct": 0.1,
                    "nav_error_pct": -0.2,
                    "abs_nav_error_pct": 0.2,
                    "growth_error_pct": -0.3,
                    "abs_growth_error_pct": 0.3,
                    "reconciled_at": "2026-08-01 08:00:00",
                }
            )

            profile = store.get_reconciliation_profile("161725", source="holding")

        self.assertEqual(profile["sample_count"], 2)
        self.assertAlmostEqual(profile["mean_nav_error_pct"], -0.05)
        self.assertAlmostEqual(profile["mean_abs_nav_error_pct"], 0.15)
        self.assertAlmostEqual(profile["mean_growth_error_pct"], -0.1)
        self.assertAlmostEqual(profile["mean_abs_growth_error_pct"], 0.2)
        self.assertAlmostEqual(profile["direction_accuracy_pct"], 50.0)

    def test_lists_recent_reconciliation_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WatchlistStore(Path(temp_dir) / "funds.db")
            store.save_reconciliation(
                {
                    "snapshot_key": "2026-07-30 15:00",
                    "snapshot_date": "2026-07-30",
                    "code": "161725",
                    "source": "holding",
                    "estimate_nav": 1.01,
                    "estimate_growth_pct": 1.0,
                    "latest_nav": 1.0,
                    "actual_nav": 1.009,
                    "actual_nav_date": "2026-07-30",
                    "actual_growth_pct": 0.9,
                    "nav_error_pct": 0.1,
                    "abs_nav_error_pct": 0.1,
                    "growth_error_pct": 0.1,
                    "abs_growth_error_pct": 0.1,
                    "reconciled_at": "2026-07-31 08:00:00",
                }
            )
            store.save_reconciliation(
                {
                    "snapshot_key": "2026-07-31 15:00",
                    "snapshot_date": "2026-07-31",
                    "code": "000001",
                    "source": "official",
                    "estimate_nav": 1.02,
                    "estimate_growth_pct": 2.0,
                    "latest_nav": 1.0,
                    "actual_nav": 1.019,
                    "actual_nav_date": "2026-07-31",
                    "actual_growth_pct": 1.9,
                    "nav_error_pct": 0.098136,
                    "abs_nav_error_pct": 0.098136,
                    "growth_error_pct": 0.1,
                    "abs_growth_error_pct": 0.1,
                    "reconciled_at": "2026-08-01 08:00:00",
                }
            )

            records = store.list_reconciliations(limit=1)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(records[0]["code"], "000001")
        self.assertAlmostEqual(records[0]["abs_nav_error_pct"], 0.098136)


if __name__ == "__main__":
    unittest.main()
