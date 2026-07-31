import unittest
from datetime import datetime

import pandas as pd

from fundval.providers import AkshareProvider


class FakeAk:
    def __init__(self):
        self.calls = []

    def fund_portfolio_hold_em(self, symbol, date):
        self.calls.append(date)
        target_year = str(datetime.now().year - 2)
        if date == target_year:
            return pd.DataFrame(
                [
                    {
                        "股票代码": "600519",
                        "股票名称": "贵州茅台",
                        "占净值比例": "30.0",
                    }
                ]
            )
        return pd.DataFrame()


class FakeLatestAk:
    def __init__(self):
        self.calls = []

    def fund_portfolio_hold_em(self, symbol, date):
        self.calls.append(date)
        if date == "":
            return pd.DataFrame(
                [
                    {
                        "股票代码": "000858",
                        "股票名称": "五粮液",
                        "占净值比例": 20.0,
                    }
                ]
            )
        return pd.DataFrame()


class FailingHoldingsAk:
    def fund_portfolio_hold_em(self, symbol, date):
        raise ValueError("upstream parser failed")


class FakeQuoteAk:
    def stock_zh_a_spot_em(self):
        return pd.DataFrame(
            [
                {"代码": "600519", "名称": "贵州茅台", "涨跌幅": 2.0},
            ]
        )

    def stock_hk_spot_em(self):
        return pd.DataFrame(
            [
                {"代码": "00700", "名称": "腾讯控股", "涨跌幅": 1.5},
            ]
        )


class TencentQuoteProvider(AkshareProvider):
    def _fetch_tencent_quote_text(self, query):
        return """
        v_sh600519="1~贵州茅台~600519~1342.30~1361.76~1330.03~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260731134123~-19.46~-1.43~";
        v_hk00700="100~腾讯控股~00700~473.800~471.800~470.000~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~2026/07/31 13:26:21~2.000~0.42~";
        v_usSNDK="200~闪迪~SNDK.OQ~1279.96~1015.89~1135.01~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~~2026-07-30 16:00:01~264.07~25.99~";
        """

    def _ak(self):
        raise AssertionError("akshare fallback should not be needed")


class TestableProvider(AkshareProvider):
    def __init__(self, fake_ak):
        super().__init__()
        self.fake_ak = fake_ak

    def _ak(self):
        return self.fake_ak

    def _fetch_tencent_quote_text(self, query):
        raise RuntimeError("network disabled in tests")


class EastmoneyFallbackProvider(TestableProvider):
    def _fetch_eastmoney_holdings_html(self, code):
        return """
        var apidata={ content:"<table><thead><tr>
        <th>序号</th><th>股票代码</th><th>股票名称</th><th>最新价</th><th>涨跌幅</th><th>相关资讯</th><th>占净值<br />比例</th><th>持股数</th><th>持仓市值</th>
        </tr></thead><tbody>
        <tr><td>1</td><td><a href='//quote.eastmoney.com/unify/r/105.SNDK'>SNDK</a></td><td class='tol'><a>闪迪</a></td><td></td><td>25.99%</td><td></td><td class='tor'>10.10%</td><td></td><td></td></tr>
        <tr><td>2</td><td><a>600519</a></td><td class='tol'><a>贵州茅台</a></td><td></td><td>1.23%</td><td></td><td class='tor'>9.90%</td><td></td><td></td></tr>
        <tr><td>2</td><td><a>000858</a></td><td class='tol'><a>五粮液</a></td><td></td><td>-0.20%</td><td></td><td class='tor'>9.63%</td><td></td><td></td></tr>
        </tbody></table>",arryear:[2026],curyear:2026};
        """


class AkshareProviderTests(unittest.TestCase):
    def test_holdings_uses_latest_available_date_before_guessing_years(self):
        fake_ak = FakeLatestAk()
        provider = TestableProvider(fake_ak)

        holdings = provider.get_holdings("161725")

        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].code, "000858")
        self.assertEqual(fake_ak.calls[0], "")

    def test_holdings_falls_back_across_recent_years(self):
        fake_ak = FakeAk()
        provider = TestableProvider(fake_ak)

        holdings = provider.get_holdings("161725")

        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].code, "600519")
        self.assertIn(str(datetime.now().year - 2), fake_ak.calls)

    def test_holdings_falls_back_to_eastmoney_f10_table(self):
        provider = EastmoneyFallbackProvider(FailingHoldingsAk())

        holdings = provider.get_holdings("005827")

        self.assertEqual(len(holdings), 3)
        self.assertEqual(holdings[0].code, "SNDK")
        self.assertEqual(holdings[0].name, "闪迪")
        self.assertAlmostEqual(holdings[0].weight_pct, 10.1)

    def test_quotes_include_a_share_and_hk_holdings(self):
        provider = TestableProvider(FakeQuoteAk())

        quotes = provider.get_quotes(["600519", "00700"])

        self.assertEqual(quotes["600519"].name, "贵州茅台")
        self.assertEqual(quotes["00700"].name, "腾讯控股")
        self.assertAlmostEqual(quotes["00700"].change_pct, 1.5)

    def test_quotes_use_targeted_tencent_query_before_full_market_fallback(self):
        provider = TencentQuoteProvider()

        quotes = provider.get_quotes(["600519", "00700", "SNDK"])

        self.assertEqual(quotes["600519"].name, "贵州茅台")
        self.assertAlmostEqual(quotes["600519"].change_pct, -1.43)
        self.assertEqual(quotes["00700"].name, "腾讯控股")
        self.assertAlmostEqual(quotes["00700"].change_pct, 0.42)
        self.assertEqual(quotes["SNDK"].name, "闪迪")
        self.assertAlmostEqual(quotes["SNDK"].change_pct, 25.99)


if __name__ == "__main__":
    unittest.main()
