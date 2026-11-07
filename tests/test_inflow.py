"""Large fund inflow: whale buys and net buy surges.

GeckoTerminal trades are stubbed (no network). Live behaviour was
verified 7 Nov 2026 on the TRAUMA robinhood pool: kind "buy" means the
meme token is the to_token, volume_in_usd is the trade size, and the
endpoint returns up to 300 recent trades.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-scanner"))
import inflow  # noqa: E402
import crypto  # noqa: E402

TOKEN = "0x5cbB5a5b43976d560002F553E3275fe687a4FBFA"
QUOTE = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
POOL = "0xAcadc6d961E301835AecE17541cC84F333dD6Fa1"


def trade(kind, vol_usd, minutes_ago=5, to_token=TOKEN, from_token=QUOTE,
          wallet="0xWalletA"):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return {"id": "x", "type": "trade", "attributes": {
        "block_timestamp": ts, "kind": kind, "volume_in_usd": str(vol_usd),
        "tx_hash": "0x" + "ab" * 32, "tx_from_address": wallet,
        "from_token_address": from_token,
        "to_token_address": to_token}}


def doc(trades):
    return {"data": trades}


class TestWhale(unittest.TestCase):
    def test_single_whale_buy_alerts(self):
        trades = [trade("buy", 15000.0, wallet="0xWalletA"),
                  trade("buy", 100.0, wallet="0xWalletB"),
                  trade("sell", 500.0, wallet="0xWalletC")]
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertTrue(out["alert"])
        self.assertIn("whale buy", out["reason"])
        self.assertNotIn("$$", out["reason"])
        self.assertEqual(out["largest_buy_usd"], 15000.0)
        self.assertEqual(out["buy_count"], 2)
        self.assertEqual(out["sell_count"], 1)
        self.assertEqual(out["distinct_buyers"], 2)
        self.assertEqual(out["distinct_sellers"], 1)

    def test_one_wallet_only_no_alert(self):
        # TRUMP bsc, 7 Nov 2026: 46 buys + 7 sells, all from one wallet
        # 0xa6...5e52. A single wallet buying and selling to itself is
        # wash trading, not an inflow, so no alert fires.
        trades = ([trade("buy", 800.0, wallet="0xOnlyOne") for _ in range(46)]
                  + [trade("sell", 400.0, wallet="0xOnlyOne") for _ in range(7)])
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertEqual(out["distinct_buyers"], 1)
        self.assertEqual(out["distinct_sellers"], 1)
        self.assertIn("wash trade", out["reason"])

    def test_buy_below_whale_threshold_no_alert(self):
        trades = [trade("buy", 4999.0), trade("sell", 100.0)]
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertEqual(out["largest_buy_usd"], 4999.0)

    def test_old_trades_outside_window_ignored(self):
        trades = [trade("buy", 50000.0, minutes_ago=45),
                  trade("buy", 100.0, minutes_ago=5)]
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertEqual(out["largest_buy_usd"], 100.0)
        self.assertEqual(out["trades_seen"], 1)


class TestSurge(unittest.TestCase):
    def test_net_inflow_surge_alerts(self):
        trades = ([trade("buy", 4000.0, wallet="0xWalletA") for _ in range(3)]
                  + [trade("buy", 4000.0, wallet="0xWalletB") for _ in range(3)]
                  + [trade("sell", 2000.0, wallet="0xWalletC") for _ in range(2)])
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertTrue(out["alert"])
        self.assertIn("net inflow", out["reason"])
        self.assertNotIn("$$", out["reason"])
        self.assertEqual(out["buy_volume_usd"], 24000.0)
        self.assertEqual(out["net_inflow_usd"], 20000.0)
        self.assertEqual(out["distinct_buyers"], 2)

    def test_net_inflow_without_buy_dominance_no_alert(self):
        # Net inflow is big ($20k), but buys are only 1.4x sells: heavy
        # two-way flow, not a buy surge. No single buy is a whale either.
        trades = ([trade("buy", 4000.0, wallet="0xWalletA") for _ in range(9)]
                  + [trade("buy", 4000.0, wallet="0xWalletB") for _ in range(8)]
                  + [trade("sell", 4000.0, wallet="0xWalletC") for _ in range(12)])
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertEqual(out["net_inflow_usd"], 20000.0)
        self.assertEqual(out["buy_volume_usd"], 68000.0)

    def test_sell_heavy_no_alert(self):
        trades = ([trade("buy", 1000.0) for _ in range(3)]
                  + [trade("sell", 8000.0) for _ in range(3)])
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertLess(out["net_inflow_usd"], 0)


class TestRobustness(unittest.TestCase):
    def test_buy_of_other_token_ignored(self):
        # A buy whose to_token is not this token is not our inflow.
        trades = [trade("buy", 50000.0, to_token="0xOTHER")]
        with patch.object(inflow, "fetch_trades", return_value=doc(trades)):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertEqual(out["buy_count"], 0)

    def test_empty_trades_no_alert(self):
        with patch.object(inflow, "fetch_trades", return_value=doc([])):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertEqual(out["trades_seen"], 0)

    def test_fetch_failure_is_unknown_not_alert(self):
        with patch.object(inflow, "fetch_trades",
                          side_effect=crypto.CryptoError("GeckoTerminal down")):
            out = inflow.check_inflow("robinhood", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertIn("GeckoTerminal down", out["error"])

    def test_unknown_chain_no_alert(self):
        out = inflow.check_inflow("nosuchchain", POOL, TOKEN)
        self.assertFalse(out["alert"])
        self.assertIsNotNone(out["error"])


class TestCard(unittest.TestCase):
    def test_inflow_card(self):
        inf = {"window_min": 30, "largest_buy_usd": 15000.0,
               "largest_buy_wallet": "0xAbCdEf1234567890abcdef1234567890AbCdEf12",
               "buy_volume_usd": 30000.0, "sell_volume_usd": 4000.0,
               "net_inflow_usd": 26000.0, "distinct_buyers": 3,
               "distinct_sellers": 1, "reason": "whale buy $15.0rb"}
        card = inflow.inflow_card("GW", "solana", POOL, TOKEN, inf,
                                  price_usd=0.0001755, change_24h=372.0,
                                  volume24h=634700.0, liquidity=72200.0)
        lines = card.split("\n")
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("*INFLOW BESAR: GW (solana)*"))
        self.assertIn("Beli terbesar: $15.0rb", lines[1])
        self.assertIn("wallet 0xAb...Ef12", lines[1])
        self.assertIn("(3 wallet)", lines[1])
        self.assertIn("Bersih masuk: $26.0rb", lines[1])
        self.assertIn("Harga: $0.0001755 | 24j 372.0%", lines[2])
        self.assertIn("dexscreener.com/solana/" + POOL, lines[3])
        self.assertIn("Pemicu: whale buy $15.0rb", lines[4])


if __name__ == "__main__":
    unittest.main()
