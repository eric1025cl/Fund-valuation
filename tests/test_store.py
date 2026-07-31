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
        self.assertAlmostEqual(profile["mean_abs_nav_error_pct"], 0.15)
        self.assertAlmostEqual(profile["mean_abs_growth_error_pct"], 0.2)
        self.assertAlmostEqual(profile["direction_accuracy_pct"], 50.0)


if __name__ == "__main__":
    unittest.main()
