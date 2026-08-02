import unittest
from pathlib import Path


class StaticPageTests(unittest.TestCase):
    def test_explains_coverage_and_confidence_rules(self):
        html = Path("web/index.html").read_text(encoding="utf-8")

        self.assertIn("覆盖率 = 参与估值的持仓权重合计", html)
        self.assertIn("置信度不是准确率概率", html)
        self.assertIn("低覆盖率估值仅代表方向参考", html)

    def test_explains_trading_refresh_and_snapshot_date(self):
        html = Path("web/index.html").read_text(encoding="utf-8")
        js = Path("web/app.js").read_text(encoding="utf-8")

        self.assertIn("交易日 9:00-15:00 页面打开时每 10 分钟自动刷新", html)
        self.assertIn("非交易日优先使用上一交易日 15:00 估值快照，没有快照则重新计算", html)
        self.assertIn("估值交易日", html)
        self.assertIn("快照日期", html)
        self.assertIn("实际净值", js)
        self.assertIn("const AUTO_REFRESH_INTERVAL_MS = 10 * 60 * 1000", js)
        self.assertIn("function isTradingRefreshWindow", js)
        self.assertIn("/api/trading-status", js)
        self.assertIn("document.hidden", js)


if __name__ == "__main__":
    unittest.main()
