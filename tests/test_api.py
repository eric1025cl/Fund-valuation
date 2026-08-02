import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`.*")

from fastapi.testclient import TestClient

from app import create_app
from fundval.service import FundValuationService
from fundval.store import WatchlistStore
from fundval.valuation import Holding, LatestNav, Quote


class FakeProvider:
    def get_fund_name(self, code):
        return {"161725": "招商中证白酒指数A"}.get(code)

    def get_official_estimate(self, code):
        return None

    def get_latest_nav(self, code):
        return LatestNav(nav=1.0, date="2026-07-30")

    def get_nav_by_date(self, code, nav_date):
        if nav_date == "2026-07-31":
            return LatestNav(nav=1.009, date=nav_date)
        return None

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

    def is_trading_day(self, current):
        return True

    def health(self):
        return {"fake": "ok"}


class ApiTests(unittest.TestCase):
    def test_add_list_estimate_and_delete_fund(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FundValuationService(
                WatchlistStore(Path(temp_dir) / "funds.db"),
                FakeProvider(),
            )
            client = TestClient(create_app(service=service))

            added = client.post("/api/funds", json={"code": "161725", "alias": "白酒"})
            listed = client.get("/api/funds")
            valuations = client.get("/api/valuations")
            snapshot = client.post("/api/snapshots", json={"snapshot_key": "2026-07-31 15:00"})
            reconciliation = client.post("/api/reconciliations")
            snapshots = client.get("/api/snapshots")
            snapshot_rows = client.get("/api/snapshots/2026-07-31%2015%3A00")
            deleted = client.delete("/api/funds/161725")

        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json()["code"], "161725")
        self.assertEqual(added.json()["alias"], "白酒")
        self.assertEqual(listed.json()[0]["name"], "招商中证白酒指数A")
        self.assertEqual(valuations.json()[0]["source"], "holding")
        self.assertAlmostEqual(valuations.json()[0]["estimate_growth_pct"], 0.8)
        self.assertEqual(snapshot.json()["snapshot_key"], "2026-07-31 15:00")
        self.assertEqual(reconciliation.json()["reconciled"], 1)
        self.assertEqual(snapshots.json()[0]["count"], 1)
        self.assertEqual(snapshot_rows.json()[0]["code"], "161725")
        self.assertAlmostEqual(snapshot_rows.json()[0]["actual_nav"], 1.009)
        self.assertEqual(deleted.json(), {"deleted": True})


if __name__ == "__main__":
    unittest.main()
