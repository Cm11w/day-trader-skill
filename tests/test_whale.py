"""Whale wallets: top buyers per pool and Solana holdings.

GeckoTerminal trades, the Solana RPC and DexScreener prices are stubbed
(no network). Live behaviour was verified 7 Nov 2026: GeckoTerminal
trades carry tx_from_address (the buyer), and the Solana RPC
getTokenAccountsByOwner returns a wallet's token accounts.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-scanner"))
import whale  # noqa: E402
import crypto  # noqa: E402

TOKEN = "CfL8KmoTngbNpmjAo1SD1udnJ1h2Rg23bng6WCbDpump"
QUOTE = "So11111111111111111111111111111111111111112"
POOL = "3ZZwFFsp6ZMamBq3JzVyedqdbxcL4meQxHfhJPhaz27o"
W1 = "FpT4WN18H2uvAzfY2WQ9QZ7jvmik4RTJ19TYZx4Um1VM"
W2 = "B52eGH4esHXZPZYmFwU8Eu1EBTQrj5o42RjEruzvnwby"


def trade(kind, vol_usd, wallet, minutes_ago=5, to_token=TOKEN):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return {"id": "x", "type": "trade", "attributes": {
        "block_timestamp": ts, "kind": kind, "volume_in_usd": str(vol_usd),
        "tx_hash": "0x" + "ab" * 32, "tx_from_address": wallet,
        "from_token_address": QUOTE, "to_token_address": to_token}}


class TestTopBuyers(unittest.TestCase):
    def test_ranks_by_total_buy_volume(self):
        trades = [trade("buy", 100.0, W1), trade("buy", 400.0, W1),
                  trade("buy", 300.0, W2), trade("sell", 999.0, W1)]
        with patch("whale.inflow.fetch_trades",
                   return_value={"data": trades}):
            buyers, err = whale.top_buyers("solana", POOL, TOKEN)
        self.assertIsNone(err)
        self.assertEqual(len(buyers), 2)
        self.assertEqual(buyers[0]["wallet"], W1)
        self.assertEqual(buyers[0]["buy_volume_usd"], 500.0)
        self.assertEqual(buyers[0]["largest_buy_usd"], 400.0)
        self.assertEqual(buyers[0]["buys"], 2)
        self.assertEqual(buyers[1]["wallet"], W2)
        self.assertEqual(buyers[1]["buy_volume_usd"], 300.0)

    def test_sells_do_not_count(self):
        trades = [trade("sell", 5000.0, W1), trade("buy", 10.0, W2)]
        with patch("whale.inflow.fetch_trades",
                   return_value={"data": trades}):
            buyers, err = whale.top_buyers("solana", POOL, TOKEN)
        self.assertEqual([b["wallet"] for b in buyers], [W2])

    def test_old_trades_ignored(self):
        trades = [trade("buy", 5000.0, W1, minutes_ago=45),
                  trade("buy", 10.0, W2, minutes_ago=5)]
        with patch("whale.inflow.fetch_trades",
                   return_value={"data": trades}):
            buyers, err = whale.top_buyers("solana", POOL, TOKEN)
        self.assertEqual([b["wallet"] for b in buyers], [W2])

    def test_limit(self):
        trades = [trade("buy", 100.0, "W%d" % i) for i in range(8)]
        with patch("whale.inflow.fetch_trades",
                   return_value={"data": trades}):
            buyers, err = whale.top_buyers("solana", POOL, TOKEN, limit=3)
        self.assertEqual(len(buyers), 3)

    def test_fetch_failure_returns_error(self):
        with patch("whale.inflow.fetch_trades",
                   side_effect=crypto.CryptoError("GeckoTerminal down")):
            buyers, err = whale.top_buyers("solana", POOL, TOKEN)
        self.assertEqual(buyers, [])
        self.assertIn("GeckoTerminal down", err)


def _token_account(mint, amount):
    return {"account": {"data": {"parsed": {"info": {
        "mint": mint, "tokenAmount": {"uiAmount": amount}}}}}}


class TestHoldings(unittest.TestCase):
    def test_ranks_by_usd_value(self):
        rpc = {"value": [_token_account("MINT_A", 1000.0),
                         _token_account("MINT_B", 5.0),
                         _token_account("MINT_C", 0.0)]}
        pairs = {"pairs": [
            {"baseToken": {"address": "MINT_A"}, "priceUsd": "0.01"},
            {"baseToken": {"address": "MINT_B"}, "priceUsd": "10.0"}]}
        with patch.object(whale, "_sol_rpc", return_value=rpc), \
             patch.object(crypto, "fetch_dexscreener_pairs",
                          return_value=pairs):
            holdings, err = whale.wallet_holdings_solana("WALLET")
        self.assertIsNone(err)
        # MINT_B: 5 x $10 = $50 beats MINT_A: 1000 x $0.01 = $10.
        # MINT_C (0 amount) is dropped.
        self.assertEqual([h["mint"] for h in holdings], ["MINT_B", "MINT_A"])
        self.assertEqual(holdings[0]["value_usd"], 50.0)
        self.assertEqual(holdings[1]["value_usd"], 10.0)

    def test_empty_wallet(self):
        with patch.object(whale, "_sol_rpc", return_value={"value": []}):
            holdings, err = whale.wallet_holdings_solana("WALLET")
        self.assertEqual(holdings, [])
        self.assertIsNone(err)

    def test_rpc_failure_returns_error(self):
        with patch.object(whale, "_sol_rpc",
                          side_effect=crypto.CryptoError("RPC down")):
            holdings, err = whale.wallet_holdings_solana("WALLET")
        self.assertEqual(holdings, [])
        self.assertIn("RPC down", err)

    def test_unpriced_mint_sorts_last(self):
        rpc = {"value": [_token_account("MINT_A", 100.0),
                         _token_account("MINT_B", 1.0)]}
        pairs = {"pairs": [
            {"baseToken": {"address": "MINT_B"}, "priceUsd": "5.0"}]}
        with patch.object(whale, "_sol_rpc", return_value=rpc), \
             patch.object(crypto, "fetch_dexscreener_pairs",
                          return_value=pairs):
            holdings, err = whale.wallet_holdings_solana("WALLET")
        self.assertEqual(holdings[0]["mint"], "MINT_B")
        self.assertEqual(holdings[1]["mint"], "MINT_A")
        self.assertIsNone(holdings[1]["value_usd"])


class TestRegistry(unittest.TestCase):
    def test_record_buy_creates_and_grows(self):
        reg = {}
        whale.record_buy(reg, W1, "solana", POOL, TOKEN, "FARMSEM", 5000.0,
                         price_usd=0.001, now="2026-11-07T14:00:00Z")
        whale.record_buy(reg, W1, "solana", POOL, TOKEN, "FARMSEM", 2000.0,
                         price_usd=0.002, now="2026-11-07T14:05:00Z")
        whale.record_buy(reg, W2, "robinhood", "0xpool", "0xtok", "TEST",
                         3000.0, now="2026-11-07T14:05:00Z")
        self.assertEqual(len(reg), 2)
        e1 = reg[W1]
        self.assertEqual(e1["total_buy_usd"], 7000.0)
        self.assertEqual(e1["buys"], 2)
        self.assertEqual(e1["first_seen"], "2026-11-07T14:00:00Z")
        self.assertEqual(e1["last_seen"], "2026-11-07T14:05:00Z")
        self.assertEqual(e1["chains"], ["solana"])
        self.assertEqual(len(e1["tokens"]), 1)
        tok = e1["tokens"]["solana:" + TOKEN]
        self.assertEqual(tok["first_buy_usd"], 7000.0)
        self.assertEqual(tok["buys"], 2)
        self.assertEqual(tok["first_price_usd"], 0.001)

    def test_top_wallets_ranked_by_volume(self):
        reg = {}
        whale.record_buy(reg, W1, "solana", POOL, TOKEN, "A", 1000.0,
                         now="2026-11-07T14:00:00Z")
        whale.record_buy(reg, W2, "solana", POOL, TOKEN, "A", 9000.0,
                         now="2026-11-07T14:00:00Z")
        top = whale.top_wallets(reg)
        self.assertEqual([t["wallet"] for t in top], [W2, W1])
        self.assertEqual(top[0]["total_buy_usd"], 9000.0)
        self.assertEqual(top[0]["tokens"], 1)

    def test_load_missing_and_corrupt(self):
        self.assertEqual(whale.load_registry("/tmp/does-not-exist-xyz.json"), {})
        with open("/tmp/whale-corrupt.json", "w") as f:
            f.write("not json")
        self.assertEqual(whale.load_registry("/tmp/whale-corrupt.json"), {})

    def test_save_and_reload_roundtrip(self):
        reg = {}
        whale.record_buy(reg, W1, "solana", POOL, TOKEN, "A", 1000.0,
                         now="2026-11-07T14:00:00Z")
        whale.save_registry(reg, "/tmp/whale-reg.json")
        self.assertEqual(whale.load_registry("/tmp/whale-reg.json"), reg)


if __name__ == "__main__":
    unittest.main()
