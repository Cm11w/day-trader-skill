import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

crypto = load("trader-scanner", "crypto")
scan = load("trader-scanner", "scan")

CDIR = os.path.join(FIX, "crypto")
GT = os.path.join(CDIR, "geckoterminal")
DS = os.path.join(CDIR, "dexscreener")
CG = os.path.join(CDIR, "coingecko")
NOW = datetime.datetime(2026, 11, 7, 16, 0)  # naive UTC, matches --now in the offline tests
MOON_ADDR = "8UQdy5sHMVWUWSHeA5B3ZuuAVNumjSLPXdf19MXcpump"


def epoch(text):
    dt = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pool(symbol, created=None, liquidity=50000.0, volume=120000.0, mcap=2000000.0):
    return {"symbol": symbol, "name": symbol, "chain": "solana", "pair_address": symbol + "PAIR",
            "token_address": symbol + "TOKEN", "dex": "pumpswap", "price_usd": 0.001,
            "market_cap_usd": mcap, "liquidity_usd": liquidity, "volume24h_usd": volume,
            "price_change_24h_pct": 5.0, "pair_created_at": created, "source": "geckoterminal"}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


class ChainMap(unittest.TestCase):
    def test_all_nine_map_both_ways(self):
        self.assertEqual(len(crypto.CHAINS), 9)
        for dex, gt in crypto.CHAINS:
            self.assertEqual(crypto.DEX_TO_GT[dex], gt)
            self.assertEqual(crypto.GT_TO_DEX[gt], dex)

    def test_default_chains_are_the_nine_dexscreener_ids(self):
        self.assertEqual(crypto.DEFAULT_CHAINS,
                         ["solana", "ethereum", "base", "bsc", "arbitrum", "polygon",
                          "avalanche", "optimism", "robinhood"])

    def test_robinhood_is_the_same_id_on_both_apis(self):
        self.assertEqual(crypto.DEX_TO_GT["robinhood"], "robinhood")
        self.assertEqual(crypto.GT_TO_DEX["robinhood"], "robinhood")

    def test_polygon_is_polygon_pos_on_geckoterminal(self):
        self.assertEqual(crypto.DEX_TO_GT["polygon"], "polygon_pos")
        self.assertEqual(crypto.GT_TO_DEX["polygon_pos"], "polygon")

    def test_unknown_chain_maps_to_nothing(self):
        self.assertIsNone(crypto.DEX_TO_GT.get("nosuchchain"))
        self.assertIsNone(crypto.GT_TO_DEX.get("nosuchchain"))


class ParseGeckoTerminal(unittest.TestCase):
    def test_trending_pools_parse_to_normalized_pools(self):
        pools = crypto.parse_geckoterminal_pools(load_json(os.path.join(GT, "solana_trending.json")),
                                                 "solana")
        self.assertEqual(len(pools), 5)
        moon = [p for p in pools if p["symbol"] == "MOON"][0]
        self.assertEqual(moon["chain"], "solana")
        self.assertEqual(moon["dex"], "pumpswap")
        self.assertEqual(moon["pair_address"], "8GjMy6ipYsXfMWwRPu9C7ATssorjg6AL88d9xtTZsLqe")
        self.assertEqual(moon["token_address"], MOON_ADDR)
        self.assertAlmostEqual(moon["price_usd"], 0.000123456)
        self.assertAlmostEqual(moon["market_cap_usd"], 2000000.5)
        self.assertAlmostEqual(moon["liquidity_usd"], 50000.0)
        self.assertAlmostEqual(moon["volume24h_usd"], 120000.0)
        self.assertAlmostEqual(moon["price_change_24h_pct"], 12.5)
        self.assertEqual(moon["pair_created_at"], epoch("2026-11-07T14:00:00Z"))
        self.assertEqual(moon["source"], "geckoterminal")

    def test_null_market_cap_stays_unknown_with_fdv_kept(self):
        pools = crypto.parse_geckoterminal_pools(load_json(os.path.join(GT, "solana_new.json")),
                                                 "solana")
        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0]["symbol"], "NEWBIE")
        self.assertEqual(pools[0]["market_cap_usd"], 0.0)
        self.assertIs(pools[0]["market_cap_known"], False)
        self.assertAlmostEqual(pools[0]["fdv_usd"], 300000.0)

    def test_null_market_cap_and_fdv_is_zero(self):
        pools = crypto.parse_geckoterminal_pools(
            {"data": [{"attributes": {"address": "P", "name": "BARE / SOL",
                                      "base_token_price_usd": "0.001", "market_cap_usd": None,
                                      "fdv_usd": None, "reserve_in_usd": "100",
                                      "volume_usd": {"h24": "10"},
                                      "price_change_percentage": {"h24": "1"},
                                      "pool_created_at": "2026-11-07T14:00:00Z"},
                       "relationships": {"base_token": {"data": {"id": "solana_ABC", "type": "token"}},
                                         "dex": {"data": {"id": "pumpswap", "type": "dex"}}}}]},
            "solana")
        self.assertEqual(pools[0]["market_cap_usd"], 0.0)

    def test_empty_pool_list_parses_to_nothing(self):
        pools = crypto.parse_geckoterminal_pools(load_json(os.path.join(GT, "base_new.json")),
                                                 "base")
        self.assertEqual(pools, [])

    def test_symbol_comes_from_the_pool_name(self):
        pools = crypto.parse_geckoterminal_pools(load_json(os.path.join(GT, "base_trending.json")),
                                                 "base")
        self.assertEqual(pools[0]["symbol"], "BASED")
        self.assertEqual(pools[0]["chain"], "base")
        self.assertEqual(pools[0]["token_address"], "0x1111111111111111111111111111111111111111")

    def test_robinhood_network_parses_and_strips_the_fee_tier(self):
        pools = crypto.parse_geckoterminal_pools(
            {"data": [{"attributes": {"address": "0xabc", "name": "CASHCAT / WETH 0.3%",
                                      "base_token_price_usd": "0.096", "market_cap_usd": "95000000",
                                      "fdv_usd": "95000000", "reserve_in_usd": "1551402",
                                      "volume_usd": {"h24": "1820717"},
                                      "price_change_percentage": {"h24": "2.25"},
                                      "pool_created_at": "2026-07-01T19:38:42Z"},
                       "relationships": {"base_token": {"data": {"id": "robinhood_0x020bfc650a365f8bb26819deaabf3e21291018b4", "type": "token"}},
                                         "dex": {"data": {"id": "uniswap-v3-robinhood", "type": "dex"}}}}]},
            "robinhood")
        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0]["symbol"], "CASHCAT")
        self.assertEqual(pools[0]["chain"], "robinhood")
        self.assertEqual(pools[0]["dex"], "uniswap-v3-robinhood")


class ParseDexScreener(unittest.TestCase):
    def test_pairs_parse_to_normalized_pools(self):
        pools = crypto.parse_dexscreener_pairs(load_json(os.path.join(DS, "pairs.json")))
        self.assertEqual(len(pools), 3)
        dsx = [p for p in pools if p["symbol"] == "DSX"][0]
        self.assertEqual(dsx["chain"], "solana")
        self.assertEqual(dsx["dex"], "pumpswap")
        self.assertAlmostEqual(dsx["price_usd"], 0.045)
        self.assertAlmostEqual(dsx["market_cap_usd"], 900000.0)
        self.assertAlmostEqual(dsx["liquidity_usd"], 30000.0)
        self.assertAlmostEqual(dsx["volume24h_usd"], 25000.0)
        self.assertAlmostEqual(dsx["price_change_24h_pct"], -3.2)
        self.assertEqual(dsx["pair_created_at"], 1794052800)
        self.assertEqual(dsx["source"], "dexscreener")

    def test_null_market_cap_stays_unknown_with_fdv_kept(self):
        pools = crypto.parse_dexscreener_pairs(load_json(os.path.join(DS, "pairs.json")))
        fdv = [p for p in pools if p["symbol"] == "FDV"][0]
        self.assertEqual(fdv["market_cap_usd"], 0.0)
        self.assertIs(fdv["market_cap_known"], False)
        self.assertAlmostEqual(fdv["fdv_usd"], 750000.0)
        moon = [p for p in pools if p["symbol"] == "MOON"][0]
        self.assertEqual(moon["market_cap_usd"], 0.0)
        self.assertIs(moon["market_cap_known"], False)
        self.assertAlmostEqual(moon["fdv_usd"], 1950000.0)
        dsx = [p for p in pools if p["symbol"] == "DSX"][0]
        self.assertAlmostEqual(dsx["market_cap_usd"], 900000.0)
        self.assertIs(dsx["market_cap_known"], True)

    def test_a_bare_pair_still_parses_with_zeroes(self):
        pools = crypto.parse_dexscreener_pairs(
            {"pairs": [{"chainId": "solana", "dexId": "pumpswap", "pairAddress": "P",
                        "baseToken": {"address": "A", "name": "Bare", "symbol": "BARE"},
                        "quoteToken": {"address": "Q", "name": "SOL", "symbol": "SOL"}}]})
        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0]["price_usd"], 0.0)
        self.assertEqual(pools[0]["liquidity_usd"], 0.0)
        self.assertEqual(pools[0]["volume24h_usd"], 0.0)
        self.assertIsNone(pools[0]["price_change_24h_pct"])
        self.assertIsNone(pools[0]["pair_created_at"])


class ParseDexScreenerTokens(unittest.TestCase):
    def test_boosts_list_gives_chain_and_address(self):
        got = crypto.parse_dexscreener_tokens(
            [{"chainId": "solana", "tokenAddress": MOON_ADDR, "amount": 100},
             {"chainId": "base", "tokenAddress": "0x1111111111111111111111111111111111111111",
              "amount": 50}])
        self.assertEqual(got, [("solana", MOON_ADDR),
                               ("base", "0x1111111111111111111111111111111111111111")])

    def test_rows_without_an_address_are_skipped(self):
        got = crypto.parse_dexscreener_tokens([{"chainId": "solana"}, {"tokenAddress": "A"}])
        self.assertEqual(got, [])


class ParseCoinGecko(unittest.TestCase):
    def test_markets_parse_to_normalized_coins(self):
        coins = crypto.parse_coingecko_markets(load_json(os.path.join(CG, "markets.json")))
        self.assertEqual(len(coins), 2)
        btc = coins[0]
        self.assertEqual(btc["id"], "bitcoin")
        self.assertEqual(btc["symbol"], "BTC")
        self.assertEqual(btc["name"], "Bitcoin")
        self.assertAlmostEqual(btc["price_usd"], 76089.0)
        self.assertEqual(btc["market_cap_usd"], 1530076758491)
        self.assertEqual(btc["volume24h_usd"], 23463624508)
        self.assertAlmostEqual(btc["price_change_24h_pct"], 0.59678)
        self.assertEqual(btc["market_cap_rank"], 1)
        self.assertEqual(coins[1]["symbol"], "ETH")


class Age(unittest.TestCase):
    def test_iso_and_ms_epochs_give_the_same_age(self):
        self.assertAlmostEqual(crypto.pool_age_hours(epoch("2026-11-07T14:00:00Z"), NOW), 2.0)
        self.assertAlmostEqual(crypto.pool_age_hours(1794052800, NOW), 4.0)

    def test_missing_creation_time_has_no_age(self):
        self.assertIsNone(crypto.pool_age_hours(None, NOW))


class Filters(unittest.TestCase):
    def test_defaults_keep_a_good_pool(self):
        kept = crypto.apply_filters([pool("GOOD", created=epoch("2026-11-07T14:00:00Z"))], now=NOW)
        self.assertEqual([p["symbol"] for p in kept], ["GOOD"])

    def test_low_liquidity_drops(self):
        kept = crypto.apply_filters([pool("THIN", created=epoch("2026-11-07T11:00:00Z"),
                                          liquidity=800.0)], now=NOW)
        self.assertEqual(kept, [])

    def test_low_volume_drops(self):
        kept = crypto.apply_filters([pool("DEAD", created=epoch("2026-11-07T13:00:00Z"),
                                          volume=100.0)], now=NOW)
        self.assertEqual(kept, [])

    def test_old_pool_drops(self):
        kept = crypto.apply_filters([pool("OLD", created=epoch("2026-07-03T00:00:00Z"))], now=NOW)
        self.assertEqual(kept, [])

    def test_min_market_cap_drops_a_small_pool(self):
        kept = crypto.apply_filters([pool("SMALL", created=epoch("2026-11-07T14:00:00Z"),
                                          mcap=1000.0)], min_marketcap_usd=100000.0, now=NOW)
        self.assertEqual(kept, [])

    def test_unknown_age_passes_the_age_filter(self):
        kept = crypto.apply_filters([pool("AGeless".upper(), created=None)], now=NOW)
        self.assertEqual(len(kept), 1)


class Bonding(unittest.TestCase):
    # Quote prices used across the tests (live-ish values, 7 Nov 2026).
    PRICES = {"solana": 89.87, "binancecoin": 693.86, "ethereum": 2241.98}
    # Graduation market caps in USD at those prices:
    #   Pump.fun 410.8 SOL -> $36,919; four.meme 88.2 BNB -> $61,199.

    def test_pumpfun_bonding_halfway(self):
        # Half the Pump.fun graduation market cap -> 50%.
        info = crypto.bonding_info("solana", "pumpfun", "Abc123pump",
                                   410.8 * 89.87 / 2, self.PRICES)
        self.assertEqual(info["status"], "bonding")
        self.assertAlmostEqual(info["progress"], 50.0, places=0)
        self.assertEqual(info["line"], "Bonding: 50% (belum lulus, Pump.fun)")

    def test_pumpfun_bonding_fresh_token_is_near_zero(self):
        # A just-launched token (mcap $67) is ~0% through the curve.
        info = crypto.bonding_info(
            "solana", "pumpfun", "BonY5MjysvGjtXWhgRTSiRVzJzUMPtdLpEYwALfjpump",
            67.18, self.PRICES)
        self.assertEqual(info["status"], "bonding")
        self.assertEqual(info["line"], "Bonding: 0% (belum lulus, Pump.fun)")

    def test_pumpfun_progress_clamps_at_100(self):
        # A market cap above graduation (stale pumpfun pair) never exceeds 100%.
        info = crypto.bonding_info("solana", "pumpfun", "Abc123pump",
                                   100000.0, self.PRICES)
        self.assertEqual(info["progress"], 100.0)

    def test_pumpfun_graduate_on_pumpswap(self):
        # A Pump.fun mint (ends in "pump") trading on PumpSwap graduated.
        info = crypto.bonding_info(
            "solana", "pumpswap",
            "8UQdy5sHMVWUWSHeA5B3ZuuAVNumjSLPXdf19MXcpump",
            8391085.0, self.PRICES)
        self.assertEqual(info["status"], "graduated")
        self.assertEqual(info["line"], "Bonding: LULUS (PumpSwap)")

    def test_fourmeme_bonding(self):
        # four.meme: 88.2 BNB graduation cap; $25k is ~41% at BNB $693.86.
        info = crypto.bonding_info("bsc", "fourmeme", "0x1234", 25000.0,
                                   self.PRICES)
        self.assertEqual(info["status"], "bonding")
        self.assertAlmostEqual(info["progress"], 40.8, places=0)
        self.assertEqual(info["line"], "Bonding: 41% (belum lulus, four.meme)")

    def test_dex_token_is_not_a_bonding_launch(self):
        info = crypto.bonding_info("ethereum", "uniswap", "0x1234",
                                   1000000.0, self.PRICES)
        self.assertEqual(info["status"], "na")
        self.assertEqual(info["line"], "Bonding: N/A (bukan bonding launch)")

    def test_pancakeswap_token_is_not_a_claimed_graduate(self):
        # A BSC token on PancakeSwap is not claimed as a four.meme graduate:
        # its origin cannot be proven from the mint.
        info = crypto.bonding_info("bsc", "pancakeswap", "0x1234",
                                   1000000.0, self.PRICES)
        self.assertEqual(info["status"], "na")
        self.assertEqual(info["line"], "Bonding: N/A (bukan bonding launch)")

    def test_no_dex_and_no_data_is_unknown(self):
        info = crypto.bonding_info("solana", "", "", None, self.PRICES)
        self.assertEqual(info["status"], "unknown")
        self.assertEqual(info["line"], "Bonding: UNKNOWN - tidak ada data bonding")

    def test_bonding_without_quote_price_is_unknown_progress(self):
        # The pool is on a bonding curve but no quote price: the status is
        # shown, the progress is UNKNOWN, never invented.
        info = crypto.bonding_info("solana", "pumpfun", "Abc123pump", 5000.0, {})
        self.assertEqual(info["status"], "bonding")
        self.assertIsNone(info["progress"])
        self.assertEqual(info["line"],
                         "Bonding: belum lulus (Pump.fun), progres UNKNOWN")

    def test_bonding_without_market_cap_is_unknown_progress(self):
        info = crypto.bonding_info("solana", "pumpfun", "Abc123pump", None,
                                   self.PRICES)
        self.assertEqual(info["status"], "bonding")
        self.assertIsNone(info["progress"])
        self.assertEqual(info["line"],
                         "Bonding: belum lulus (Pump.fun), progres UNKNOWN")


class Dedupe(unittest.TestCase):
    def test_same_token_keeps_the_most_liquid_pair(self):
        a = pool("MOON", created=epoch("2026-11-07T14:00:00Z"), liquidity=50000.0)
        b = pool("MOON", created=epoch("2026-11-07T14:05:00Z"), liquidity=20000.0)
        b["pair_address"] = "OTHERPAIR"
        kept = crypto.dedupe_pools([b, a])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["pair_address"], "MOONPAIR")

    def test_different_tokens_are_all_kept(self):
        kept = crypto.dedupe_pools([pool("AAA"), pool("BBB")])
        self.assertEqual(len(kept), 2)

    def test_same_symbol_on_another_chain_is_a_different_token(self):
        a = pool("SAME")
        b = pool("SAME")
        b["chain"] = "base"
        kept = crypto.dedupe_pools([a, b])
        self.assertEqual(len(kept), 2)

    def test_sort_is_most_traded_first(self):
        got = crypto.sort_pools([pool("LOW", volume=1000.0), pool("HIGH", volume=9000.0)])
        self.assertEqual([p["symbol"] for p in got], ["HIGH", "LOW"])


class NowParsing(unittest.TestCase):
    def test_zulu_and_naive_both_mean_utc(self):
        self.assertEqual(crypto.parse_now_utc("2026-11-07 16:00Z"), NOW)
        self.assertEqual(crypto.parse_now_utc("2026-11-07 16:00"), NOW)

    def test_garbage_raises(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.parse_now_utc("yesterday-ish")


class MemesOffline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "watchlist-crypto.json")

    def base_args(self, extra=None):
        args = ["memes", "--offline", CDIR, "--chains", "solana,base",
                "--now", "2026-11-07 16:00Z", "--out", self.out, "--json"]
        return args + (extra or [])

    def test_end_to_end_offline_scan(self):
        code, text, err = run(self.base_args())
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["created_by"], "trader-scanner")
        self.assertEqual(doc["mode"], "memes")
        self.assertEqual(doc["universe"], 10)
        self.assertEqual(doc["scanned"], 8)
        self.assertEqual(doc["failed"], 0)
        self.assertEqual(doc["source"], "geckoterminal+dexscreener")
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["MOON", "BASED", "DSX", "FDV", "NEWBIE"])
        moon = doc["hits"][0]
        for key in ("symbol", "name", "chain", "pair_address", "dex", "price_usd", "market_cap_usd",
                    "liquidity_usd", "volume24h_usd", "price_change_24h_pct", "pair_age_hours",
                    "source"):
            self.assertIn(key, moon, key)
        self.assertEqual(moon["chain"], "solana")
        self.assertEqual(moon["dex"], "pumpswap")
        self.assertEqual(moon["source"], "geckoterminal")
        self.assertAlmostEqual(moon["pair_age_hours"], 2.0)
        self.assertAlmostEqual(moon["price_usd"], 0.000123456)
        self.assertEqual(doc["hits"][2]["source"], "dexscreener")
        self.assertEqual(doc["hits"][3]["market_cap_usd"], 0.0)  # null mcap: unknown, not the FDV
        self.assertIs(doc["hits"][3]["market_cap_known"], False)
        self.assertAlmostEqual(doc["hits"][3]["fdv_usd"], 750000.0)
        self.assertEqual(doc["hits"][4]["market_cap_usd"], 0.0)  # null mcap, fdv kept separately
        self.assertIs(doc["hits"][4]["market_cap_known"], False)
        self.assertAlmostEqual(doc["hits"][4]["fdv_usd"], 300000.0)
        with open(self.out, encoding="utf-8") as fh:
            written = json.load(fh)
        # the file holds the doc; stdout adds only the timing field
        self.assertEqual({k: v for k, v in doc.items() if k != "seconds"}, written)

    def test_max_age_three_hours_keeps_only_the_young(self):
        code, text, err = run(self.base_args(["--max-age-hours", "3"]))
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["MOON", "NEWBIE"])

    def test_impossible_filters_find_nothing(self):
        code, text, err = run(self.base_args(["--min-liquidity-usd", "100000"]))
        self.assertEqual(code, 1)
        doc = json.loads(text)
        self.assertEqual(doc["hits"], [])

    def test_unknown_chain_fails_without_touching_the_network(self):
        code, text, err = run(["memes", "--chains", "nosuchchain", "--now", "2026-11-07 16:00Z",
                               "--out", self.out, "--json"])
        self.assertEqual(code, 1)
        self.assertIn("nosuchchain", err)

    def test_limit_caps_the_hits(self):
        code, text, err = run(self.base_args(["--limit", "2"]))
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["MOON", "BASED"])

    def test_chains_solana_only_drops_the_base_pairs(self):
        code, text, err = run(["memes", "--offline", CDIR, "--chains", "solana",
                               "--now", "2026-11-07 16:00Z", "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["universe"], 8)
        self.assertEqual(doc["scanned"], 6)
        self.assertEqual(doc["failed"], 0)
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["MOON", "DSX", "NEWBIE"])

    def test_robinhood_without_fixtures_counts_as_failed_but_keeps_the_hits(self):
        code, text, err = run(["memes", "--offline", CDIR, "--chains", "solana,base,robinhood",
                               "--now", "2026-11-07 16:00Z", "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["failed"], 2)  # robinhood trending + new files are missing
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["MOON", "BASED", "DSX", "FDV", "NEWBIE"])

    def test_nan_and_negative_numeric_args_fail(self):
        for args, word in ((["--min-liquidity-usd", "nan"], "--min-liquidity-usd"),
                           (["--min-volume24h-usd", "inf"], "--min-volume24h-usd"),
                           (["--max-age-hours", "-1"], "--max-age-hours"),
                           (["--min-marketcap-usd", "nan"], "--min-marketcap-usd"),
                           (["--limit", "0"], "--limit"),
                           (["--pause", "nan"], "--pause"),
                           (["--show", "0"], "--show")):
            code, text, err = run(self.base_args(args))
            self.assertEqual(code, 1, args)
            self.assertIn(word, err, args)


def run_crypto(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = crypto.main(argv)
    return code, out.getvalue(), err.getvalue()


GP = os.path.join(CDIR, "goplus")
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class ParseOHLCV(unittest.TestCase):
    def test_bars_parse_and_sort_ascending(self):
        bars = crypto.parse_ohlcv(load_json(os.path.join(GT, "ohlcv_minute.json")))
        self.assertEqual(len(bars), 5)
        self.assertEqual([b["t"] for b in bars],
                         [1794041700, 1794042000, 1794042300, 1794042600, 1794042900])
        first = bars[0]
        self.assertAlmostEqual(first["o"], 0.016911609624897503)
        self.assertAlmostEqual(first["h"], 0.017643437680847578)
        self.assertAlmostEqual(first["l"], 0.016909357251402725)
        self.assertAlmostEqual(first["c"], 0.01746852316526902)
        self.assertAlmostEqual(first["v"], 7124.169492964765)

    def test_rows_with_a_missing_close_are_dropped(self):
        bars = crypto.parse_ohlcv(
            {"data": {"attributes": {"ohlcv_list": [[100, 1.0, 1.1, 0.9, None, 5.0],
                                                    [200, 1.0, 1.1, 0.9, 1.05, 7.0],
                                                    [300, "bad", 1.1, 0.9, 1.05, 7.0]]}}})
        self.assertEqual([b["t"] for b in bars], [200])

    def test_empty_list_parses_to_nothing(self):
        self.assertEqual(crypto.parse_ohlcv({"data": {"attributes": {"ohlcv_list": []}}}), [])
        self.assertEqual(crypto.parse_ohlcv({}), [])

    def test_to_series_matches_the_scanner_shape(self):
        bars = crypto.parse_ohlcv(load_json(os.path.join(GT, "ohlcv_minute.json")))
        s = crypto.ohlcv_to_series(bars)
        self.assertEqual(len(s["t"]), 5)
        self.assertEqual(len(s["c"]), 5)
        self.assertEqual(len(s["o"]), 5)
        self.assertEqual(len(s["h"]), 5)
        self.assertEqual(len(s["l"]), 5)
        self.assertEqual(len(s["v"]), 5)
        self.assertAlmostEqual(s["c"][-1], 0.01700239693502563)
        self.assertEqual(s["meta"], {})


class ParseCoinGeckoCommunity(unittest.TestCase):
    def test_contract_detail_gives_handle_and_followers(self):
        got = crypto.parse_coingecko_community(load_json(os.path.join(CG, "contract_coge.json")))
        self.assertEqual(got["coin_id"], "coge-coin-2")
        self.assertEqual(got["symbol"], "COGE")
        self.assertEqual(got["name"], "COGE COIN")
        self.assertEqual(got["twitter_handle"], "cogecoin")
        self.assertEqual(got["twitter_followers"], 12345)
        self.assertEqual(got["telegram_users"], 2345)
        self.assertEqual(got["reddit_subscribers"], 321)

    def test_missing_community_data_is_unknown_not_zero(self):
        got = crypto.parse_coingecko_community({"id": "x", "symbol": "x", "name": "X"})
        self.assertIsNone(got["twitter_handle"])
        self.assertIsNone(got["twitter_followers"])
        self.assertIsNone(got["telegram_users"])

    def test_platform_ids_cover_the_evm_chains_and_solana(self):
        self.assertEqual(crypto.CG_PLATFORMS["solana"], "solana")
        self.assertEqual(crypto.CG_PLATFORMS["base"], "base")
        self.assertEqual(crypto.CG_PLATFORMS["bsc"], "binance-smart-chain")
        self.assertEqual(crypto.CG_PLATFORMS["arbitrum"], "arbitrum-one")
        self.assertEqual(crypto.CG_PLATFORMS["polygon"], "polygon-pos")
        self.assertEqual(crypto.CG_PLATFORMS["avalanche"], "avalanche")
        self.assertEqual(crypto.CG_PLATFORMS["optimism"], "optimistic-ethereum")
        self.assertIsNone(crypto.CG_PLATFORMS.get("robinhood"))


class ParseCoinGeckoTrending(unittest.TestCase):
    def test_trending_parses_money_strings(self):
        coins = crypto.parse_coingecko_trending(load_json(os.path.join(CG, "trending.json")))
        self.assertEqual(len(coins), 2)
        self.assertEqual(coins[0]["symbol"], "DACITY")
        self.assertEqual(coins[0]["market_cap_rank"], 926)
        self.assertAlmostEqual(coins[0]["price_usd"], 0.016319540926820846)
        self.assertAlmostEqual(coins[0]["price_change_24h_pct"], 118.91286618002516)
        self.assertEqual(coins[0]["market_cap_usd"], 16319541.0)
        self.assertEqual(coins[0]["volume24h_usd"], 3763506.0)
        self.assertEqual(coins[1]["symbol"], "BTC")

    def test_money_string_parsing(self):
        self.assertEqual(crypto._money("$16,319,541"), 16319541.0)
        self.assertEqual(crypto._money("$0.50"), 0.5)
        self.assertEqual(crypto._money(None), 0.0)
        self.assertEqual(crypto._money("n/a"), 0.0)


class ParseGoPlus(unittest.TestCase):
    def test_security_flags_parse(self):
        got = crypto.parse_goplus_security(load_json(os.path.join(GP, "base_usdc.json")), USDC)
        self.assertIsNotNone(got)
        self.assertIs(got["is_honeypot"], False)
        self.assertEqual(got["buy_tax"], 0.0)
        self.assertEqual(got["sell_tax"], 0.0)
        self.assertIs(got["cannot_buy"], False)
        self.assertIs(got["is_proxy"], True)
        self.assertIs(got["is_open_source"], True)
        self.assertEqual(got["holder_count"], 11521634)
        self.assertEqual(got["creator_percent"], 0.0)
        self.assertAlmostEqual(got["top10_percent"], 0.054639230401827482 + 0.037501575414606033
                               + 0.037406325174016555, places=6)
        self.assertEqual(len(got["top_holders"]), 3)

    def test_unknown_address_is_none(self):
        got = crypto.parse_goplus_security(load_json(os.path.join(GP, "base_usdc.json")),
                                           "0x0000000000000000000000000000000000000000")
        self.assertIsNone(got)

    def test_goplus_covers_evm_including_robinhood(self):
        for chain in ("ethereum", "bsc", "polygon", "arbitrum", "optimism",
                      "avalanche", "base", "robinhood"):
            self.assertIn(chain, crypto.GOPLUS_CHAINS)
        self.assertEqual(crypto.GOPLUS_CHAINS["robinhood"], "4663")
        # Solana is not in the EVM map: it has its own GoPlus endpoint.
        self.assertNotIn("solana", crypto.GOPLUS_CHAINS)


BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def bonk_goplus():
    return crypto.parse_goplus_solana(load_json(os.path.join(GP, "solana_bonk.json")), BONK_MINT)


def bonk_rugcheck():
    return crypto.parse_rugcheck(load_json(os.path.join(GP, "rugcheck_bonk.json")))


def bonk_jupiter():
    return crypto.parse_jupiter_quote(load_json(os.path.join(GP, "jupiter_bonk.json")))


def bonk_security():
    return crypto.merge_solana_security(bonk_goplus(), bonk_rugcheck(), bonk_jupiter())


class ParseGoPlusSolana(unittest.TestCase):
    def test_bonk_record_parses(self):
        got = bonk_goplus()
        self.assertIsNotNone(got)
        self.assertIs(got["freezable"], False)
        self.assertIs(got["mintable"], False)
        self.assertIs(got["transfer_hook"], False)
        self.assertEqual(got["transfer_fee_pct"], 0.0)
        self.assertEqual(got["holder_count"], 1020153)
        self.assertEqual(len(got["top_holders"]), 10)
        self.assertEqual(got["top_holders"][0]["address"],
                         "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")
        self.assertAlmostEqual(got["top_holders"][0]["percent"], 0.0883, places=4)
        self.assertAlmostEqual(got["top10_percent"], 0.3843, places=3)
        self.assertAlmostEqual(got["total_supply"], 87994357732509.84, places=0)

    def test_unknown_mint_is_none(self):
        got = crypto.parse_goplus_solana(load_json(os.path.join(GP, "solana_bonk.json")),
                                         "So11111111111111111111111111111111111111112")
        self.assertIsNone(got)

    def test_empty_transfer_fee_is_zero_not_unknown(self):
        got = bonk_goplus()
        self.assertEqual(got["transfer_fee_pct"], 0.0)


class ParseRugCheck(unittest.TestCase):
    def test_bonk_report_parses(self):
        got = bonk_rugcheck()
        self.assertIsNotNone(got)
        self.assertEqual(got["score_normalised"], 7)
        self.assertEqual([r["name"] for r in got["risks"]], ["Mutable metadata"])
        self.assertEqual(got["risks"][0]["level"], "warn")
        self.assertEqual(got["danger"], [])
        # The full report carries no lpLockedPct (it is None); the summary
        # endpoint reported 18.21 for the same token.
        self.assertIsNone(got["lp_locked_pct"])
        self.assertEqual(len(got["top_holders"]), 20)
        # RugCheck percents are 0-100; they are normalised to fractions.
        self.assertAlmostEqual(got["top_holders"][0]["percent"], 0.09485781017799967, places=6)
        self.assertEqual(got["top_holders"][0]["address"],
                         "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM")

    def test_a_doc_without_a_mint_is_none(self):
        self.assertIsNone(crypto.parse_rugcheck({"score_normalised": 7}))
        self.assertIsNone(crypto.parse_rugcheck(None))

    def test_danger_level_risks_are_listed(self):
        got = crypto.parse_rugcheck({"mint": "M", "score_normalised": 90,
                                     "risks": [{"name": "Freeze Authority enabled", "level": "danger"},
                                               {"name": "Mutable metadata", "level": "warn"}],
                                     "topHolders": []})
        self.assertEqual(got["danger"], ["Freeze Authority enabled"])


class ParseJupiter(unittest.TestCase):
    def test_bonk_quote_is_sellable(self):
        got = bonk_jupiter()
        self.assertIs(got["sellable"], True)
        self.assertLess(got["price_impact_pct"], 0.01)
        self.assertEqual(got["out_amount"], 311)
        # The route label varies between live quotes (the first live call
        # said "Meteora DLMM"); the saved fixture routes via Whirlpool.
        self.assertEqual(got["route_label"], "Whirlpool")

    def test_no_route_is_not_sellable(self):
        self.assertIs(crypto.parse_jupiter_quote(None)["sellable"], False)
        self.assertIs(crypto.parse_jupiter_quote({"error": "No route found"})["sellable"], False)
        self.assertIs(crypto.parse_jupiter_quote({"routePlan": []})["sellable"], False)

    def test_no_route_is_none_not_an_error(self):
        # fetch_jupiter_quote turns a 400/404 into None (no route); a 429
        # still raises. The None case is covered by test_no_route_is_not_sellable.
        self.assertIs(crypto.parse_jupiter_quote(None)["sellable"], False)


class MergeSolanaSecurity(unittest.TestCase):
    def test_bonk_merges_to_a_clean_record(self):
        got = bonk_security()
        self.assertIsNotNone(got)
        self.assertEqual(got["chain"], "solana")
        self.assertIs(got["is_honeypot"], False)
        self.assertEqual(got["buy_tax"], 0.0)
        self.assertEqual(got["sell_tax"], 0.0)
        self.assertIs(got["freezable"], False)
        self.assertIs(got["mintable"], False)
        self.assertIs(got["transfer_hook"], False)
        self.assertIs(got["sellable"], True)
        self.assertEqual(got["rugcheck_score"], 7)
        self.assertEqual(got["rugcheck_danger"], [])
        self.assertEqual(len(got["top_holders"]), 10)
        self.assertAlmostEqual(got["top10_percent"], 0.3843, places=3)

    def test_all_three_missing_is_none(self):
        self.assertIsNone(crypto.merge_solana_security(None, None, None))

    def test_a_transfer_hook_is_a_honeypot(self):
        rec = dict(bonk_goplus(), transfer_hook=True)
        got = crypto.merge_solana_security(rec, bonk_rugcheck(), bonk_jupiter())
        self.assertIs(got["is_honeypot"], True)

    def test_no_sell_route_is_a_honeypot(self):
        got = crypto.merge_solana_security(bonk_goplus(), bonk_rugcheck(),
                                           {"sellable": False, "price_impact_pct": None,
                                            "out_amount": None, "route_label": None})
        self.assertIs(got["is_honeypot"], True)

    def test_absurd_impact_on_a_tiny_probe_is_a_honeypot(self):
        got = crypto.merge_solana_security(bonk_goplus(), bonk_rugcheck(),
                                           {"sellable": True, "price_impact_pct": 87.5,
                                            "out_amount": 1, "route_label": "X"})
        self.assertIs(got["is_honeypot"], True)

    def test_a_failed_probe_is_unknown_never_a_pass(self):
        # GoPlus says no hook, but the Jupiter probe never answered: the
        # honeypot verdict is UNKNOWN, not a green light.
        got = crypto.merge_solana_security(bonk_goplus(), bonk_rugcheck(), None)
        self.assertIsNone(got["is_honeypot"])
        self.assertIsNone(got["sellable"])

    def test_rugcheck_holders_fill_in_when_goplus_has_none(self):
        rec = dict(bonk_goplus(), top_holders=[], top10_percent=None)
        got = crypto.merge_solana_security(rec, bonk_rugcheck(), bonk_jupiter())
        self.assertEqual(len(got["top_holders"]), 20)
        self.assertAlmostEqual(got["top10_percent"],
                               sum(h["percent"] for h in bonk_rugcheck()["top_holders"][:10]),
                               places=6)


class ParseDexScreenerSocials(unittest.TestCase):
    def test_socials_come_from_the_most_liquid_pair(self):
        got = crypto.parse_dexscreener_socials(load_json(os.path.join(DS, "pairs_socials.json")))
        self.assertEqual(got["twitter_url"], "https://x.com/mooncoin")
        self.assertEqual(got["handle"], "mooncoin")
        self.assertEqual(got["telegram_url"], "https://t.me/mooncoin")
        self.assertEqual(got["website_url"], "https://moon.example")

    def test_twitter_com_url_gives_the_same_handle(self):
        got = crypto.parse_dexscreener_socials(
            {"pairs": [{"chainId": "base", "dexId": "uniswap", "pairAddress": "P",
                        "baseToken": {"address": "A", "name": "X", "symbol": "X"},
                        "liquidity": {"usd": 100.0},
                        "info": {"socials": [{"type": "twitter",
                                               "url": "https://twitter.com/somehandle"}]}}]})
        self.assertEqual(got["handle"], "somehandle")

    def test_no_socials_is_all_unknown(self):
        got = crypto.parse_dexscreener_socials({"pairs": []})
        self.assertIsNone(got["twitter_url"])
        self.assertIsNone(got["handle"])
        self.assertIsNone(got["telegram_url"])


class CryptoCommandsOffline(unittest.TestCase):
    def test_ohlcv_command_reads_a_fixture(self):
        code, text, err = run_crypto(["ohlcv", "--chain", "solana", "--pair", "PAIR",
                                      "--offline", os.path.join(GT, "ohlcv_minute.json"),
                                      "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(len(doc["bars"]), 5)
        self.assertEqual(doc["bars"][0]["t"], 1794041700)

    def test_cg_trending_command_reads_a_fixture(self):
        code, text, err = run_crypto(["cg-trending", "--offline",
                                      os.path.join(CG, "trending.json"), "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual([c["symbol"] for c in doc["coins"]], ["DACITY", "BTC"])

    def test_contract_command_reads_a_fixture(self):
        code, text, err = run_crypto(["contract", "--chain", "solana", "--address", MOON_ADDR,
                                      "--offline", os.path.join(CG, "contract_coge.json"),
                                      "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["twitter_handle"], "cogecoin")
        self.assertEqual(doc["twitter_followers"], 12345)

    def test_security_command_reads_a_fixture(self):
        code, text, err = run_crypto(["security", "--chain", "base", "--address", USDC,
                                      "--offline", os.path.join(GP, "base_usdc.json"),
                                      "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertIs(doc["is_honeypot"], False)
        self.assertEqual(doc["holder_count"], 11521634)

    def test_security_on_solana_reads_a_fixture(self):
        code, text, err = run_crypto(["security", "--chain", "solana", "--address", BONK_MINT,
                                      "--offline", os.path.join(GP, "solana_bonk.json"),
                                      "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["chain"], "solana")
        self.assertIs(doc["freezable"], False)
        self.assertIs(doc["mintable"], False)
        self.assertIs(doc["transfer_hook"], False)
        self.assertEqual(doc["holder_count"], 1020153)

    def test_security_on_an_unknown_chain_fails(self):
        code, text, err = run_crypto(["security", "--chain", "faketoken", "--address", MOON_ADDR,
                                      "--offline", os.path.join(GP, "base_usdc.json"),
                                      "--json"])
        self.assertEqual(code, 1)
        self.assertIn("faketoken", err)


class WatchCommand(unittest.TestCase):
    def test_watch_offline_json_most_trending_first(self):
        code, text, err = run_crypto(["watch", "--offline",
                                      os.path.join(CG, "trending.json"), "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual([c["symbol"] for c in doc["coins"]], ["DACITY", "BTC"])
        self.assertEqual(doc["source"], "coingecko")

    def test_watch_limit_caps_the_list(self):
        code, text, err = run_crypto(["watch", "--offline",
                                      os.path.join(CG, "trending.json"), "--limit", "1", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual([c["symbol"] for c in doc["coins"]], ["DACITY"])

    def test_watch_writes_the_out_file_when_given(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "watchlist-crypto-trending.json")
            code, text, err = run_crypto(["watch", "--offline",
                                          os.path.join(CG, "trending.json"),
                                          "--limit", "1", "--out", out, "--json"])
            self.assertEqual(code, 0, err)
            with open(out, encoding="utf-8") as fh:
                written = json.load(fh)
            self.assertEqual([c["symbol"] for c in written["coins"]], ["DACITY"])

    def test_watch_text_prints_most_trending_first(self):
        code, text, err = run_crypto(["watch", "--offline",
                                      os.path.join(CG, "trending.json")])
        self.assertEqual(code, 0, err)
        self.assertLess(text.index("DACITY"), text.index("BTC"))

    def test_watch_with_no_trending_coins_fails(self):
        with tempfile.TemporaryDirectory() as d:
            empty = os.path.join(d, "trending.json")
            with open(empty, "w") as fh:
                json.dump({"coins": []}, fh)
            code, text, err = run_crypto(["watch", "--offline", empty, "--json"])
            self.assertEqual(code, 1)


class ParseCreatedDates(unittest.TestCase):
    def test_fractional_seconds_parse(self):
        self.assertEqual(crypto.parse_iso_created("2026-11-07T14:00:00.123Z"),
                         epoch("2026-11-07T14:00:00Z"))

    def test_numeric_offset_parses(self):
        self.assertEqual(crypto.parse_iso_created("2026-11-07T14:00:00+00:00"),
                         epoch("2026-11-07T14:00:00Z"))
        self.assertEqual(crypto.parse_iso_created("2026-11-07T16:00:00+02:00"),
                         epoch("2026-11-07T14:00:00Z"))

    def test_garbage_is_none(self):
        self.assertIsNone(crypto.parse_iso_created("not-a-date"))
        self.assertIsNone(crypto.parse_iso_created(""))
        self.assertIsNone(crypto.parse_iso_created(None))

    def test_unparseable_created_fails_the_age_filter(self):
        bad = pool("BADDATE", created="not-a-date")
        self.assertEqual(crypto.apply_filters([bad], now=NOW), [])
        missing = pool("NODATE", created=None)
        self.assertEqual(len(crypto.apply_filters([missing], now=NOW)), 1)


class FiltersNoneAndUnknown(unittest.TestCase):
    def test_none_liquidity_drops_without_crashing(self):
        bad = pool("NONELIQ", created=epoch("2026-11-07T14:00:00Z"))
        bad["liquidity_usd"] = None
        self.assertEqual(crypto.apply_filters([bad], now=NOW), [])
        bad["volume24h_usd"] = None
        bad["liquidity_usd"] = 50000.0
        self.assertEqual(crypto.apply_filters([bad], now=NOW), [])

    def test_unknown_market_cap_fails_a_market_cap_floor(self):
        unknown = pool("FDVONLY", created=epoch("2026-11-07T14:00:00Z"), mcap=0.0)
        unknown["market_cap_known"] = False
        self.assertEqual(crypto.apply_filters([unknown], min_marketcap_usd=1000.0, now=NOW), [])
        self.assertEqual(len(crypto.apply_filters([unknown], min_marketcap_usd=0.0, now=NOW)), 1)


class DedupeEmptyKeys(unittest.TestCase):
    def test_pools_without_any_address_are_all_kept(self):
        a = pool("AAA")
        b = pool("BBB")
        for p in (a, b):
            p["token_address"] = ""
            p["pair_address"] = ""
        self.assertEqual(len(crypto.dedupe_pools([a, b])), 2)


class HandleFromUrl(unittest.TestCase):
    def test_profile_url_gives_the_handle(self):
        self.assertEqual(crypto._handle_from_url("https://x.com/mooncoin"), "mooncoin")
        self.assertEqual(crypto._handle_from_url("https://twitter.com/somehandle"), "somehandle")
        self.assertEqual(crypto._handle_from_url("https://x.com/@handle"), "handle")

    def test_status_url_gives_the_profile_not_the_id(self):
        self.assertEqual(crypto._handle_from_url("https://x.com/realhandle/status/123"),
                         "realhandle")

    def test_bare_domain_home_and_reserved_words_are_not_handles(self):
        for url in ("https://x.com/", "https://x.com/home", "https://x.com/explore",
                    "https://x.com/notifications", "https://example.com/foo",
                    "https://x.com/i/articles", ""):
            self.assertIsNone(crypto._handle_from_url(url), url)


class Top10Sorting(unittest.TestCase):
    def test_top10_sums_the_largest_holders_not_the_first_ten(self):
        holders = [{"address": "0x%02d" % i, "percent": str(p),
                    "is_locked": "0", "is_contract": "0"}
                   for i, p in enumerate([0.01] * 10 + [0.4, 0.3])]
        doc = {"code": 1, "result": {"0xabc": {
            "is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "cannot_buy": "0",
            "is_proxy": "0", "is_open_source": "1", "owner_address": "",
            "creator_address": "", "creator_percent": "0", "holder_count": "12",
            "holders": holders}}}
        got = crypto.parse_goplus_security(doc, "0xabc")
        self.assertAlmostEqual(got["top10_percent"], 0.78)
        self.assertAlmostEqual(got["top_holders"][0]["percent"], 0.4)


class FetchMemesPaging(unittest.TestCase):
    def test_all_boosted_tokens_are_fetched_not_just_the_first_thirty(self):
        from unittest import mock
        wanted = [("solana", "TOKEN%02d" % i) for i in range(35)]
        seen = []

        def fake_pairs(addresses, pause=0):
            seen.extend(addresses)
            return {"pairs": []}

        with mock.patch.object(crypto, "fetch_geckoterminal", return_value={"data": []}), \
                mock.patch.object(crypto, "fetch_dexscreener_boosts", return_value=wanted), \
                mock.patch.object(crypto, "fetch_dexscreener_pairs", side_effect=fake_pairs):
            pools, failed = crypto.fetch_memes(["solana"], pause=0)
        self.assertEqual(failed, 0)
        self.assertEqual(sorted(set(seen)), sorted(wanted))
        self.assertEqual(len(seen), 35)


class FetchOhlcvPaged(unittest.TestCase):
    def _fake(self, calls):
        def fake_fetch(network, pool_address, timeframe, aggregate, limit, before=None, pause=0):
            calls.append((limit, before))
            all_ts = list(range(3000, 0, -1))
            rows = [t for t in all_ts if before is None or t < before][:limit]
            return {"data": {"attributes": {"ohlcv_list": [[t, 1, 1, 1, 1, 1] for t in rows]}}}
        return fake_fetch

    def test_pages_past_the_1000_bar_cap(self):
        from unittest import mock
        calls = []
        with mock.patch.object(crypto, "fetch_ohlcv", side_effect=self._fake(calls)):
            doc = crypto.fetch_ohlcv_paged("solana", "PAIR", "minute", 5, 1400, pause=0)
        rows = doc["data"]["attributes"]["ohlcv_list"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(rows), 2000)
        self.assertEqual(len({r[0] for r in rows}), 2000)

    def test_a_short_page_stops_paging(self):
        from unittest import mock
        calls = []

        def fake_fetch(network, pool_address, timeframe, aggregate, limit, before=None, pause=0):
            calls.append((limit, before))
            rows = [[t, 1, 1, 1, 1, 1] for t in range(500, 0, -1)]
            return {"data": {"attributes": {"ohlcv_list": rows}}}
        with mock.patch.object(crypto, "fetch_ohlcv", side_effect=fake_fetch):
            doc = crypto.fetch_ohlcv_paged("solana", "PAIR", "minute", 5, 1400, pause=0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(doc["data"]["attributes"]["ohlcv_list"]), 500)


class PctText(unittest.TestCase):
    def test_unknown_change_prints_as_unknown(self):
        self.assertEqual(crypto._pct_text(None), "n/a")
        self.assertEqual(crypto._pct_text(12.5), "+12.50%")
        self.assertEqual(crypto._pct_text(-3.2), "-3.20%")


class NumericArgs(unittest.TestCase):
    def test_zero_or_negative_counts_fail(self):
        code, text, err = run_crypto(["ohlcv", "--chain", "solana", "--pair", "PAIR",
                                      "--offline", os.path.join(GT, "ohlcv_minute.json"),
                                      "--limit", "0", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("--limit", err)
        code, text, err = run_crypto(["watch", "--offline",
                                      os.path.join(CG, "trending.json"), "--limit", "-1"])
        self.assertEqual(code, 1)
        self.assertIn("--limit", err)

    def test_nan_pause_fails(self):
        code, text, err = run_crypto(["ohlcv", "--chain", "solana", "--pair", "PAIR",
                                      "--offline", os.path.join(GT, "ohlcv_minute.json"),
                                      "--pause", "nan", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("--pause", err)


if __name__ == "__main__":
    unittest.main()
