import datetime
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

clone = load("trader-scanner", "clone_check")
crypto = load("trader-scanner", "crypto")

DS = os.path.join(FIX, "crypto", "dexscreener")
SEARCH_LETRO = os.path.join(DS, "search-letro.json")
SEARCH_ANTHROPIC = os.path.join(DS, "search-anthropic.json")
BASE_ANTHROPIC = "0xf6d41cbb39a6540a93e0d29a50b4eb720444e754"
NOW = datetime.datetime(2026, 11, 7, 16, 0)  # naive UTC


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cand(addr, chain="solana", liquidity=1000.0, volume=1000.0, created=None,
         twitter=None, website=None, cg=None):
    return {"token_address": addr, "symbol": "TEST", "name": "Test",
            "chains": [chain], "pair_address": addr + "PAIR", "dex": "pumpswap",
            "price_usd": 0.001, "liquidity_usd": liquidity,
            "volume24h_usd": volume, "price_change_24h_pct": 5.0,
            "oldest_created": created, "n_pairs": 1,
            "twitter_url": twitter, "handle": None, "telegram_url": None,
            "discord_url": None, "website_url": website, "cg_listed": cg}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = clone.main(argv)
    return code, out.getvalue(), err.getvalue()


class Grouping(unittest.TestCase):
    def test_same_address_on_two_chains_is_one_candidate(self):
        pools = [
            {"symbol": "X", "name": "X", "chain": "base", "pair_address": "P1",
             "token_address": "0xabc", "dex": "uni", "price_usd": 1.0,
             "liquidity_usd": 5000.0, "volume24h_usd": 1000.0,
             "price_change_24h_pct": 1.0, "pair_created_at": 1000},
            {"symbol": "X", "name": "X", "chain": "bsc", "pair_address": "P2",
             "token_address": "0xABC", "dex": "pancake", "price_usd": 1.0,
             "liquidity_usd": 9000.0, "volume24h_usd": 2000.0,
             "price_change_24h_pct": 2.0, "pair_created_at": 900},
        ]
        cands = clone.group_candidates(pools)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["chains"], ["base", "bsc"])
        self.assertEqual(cands[0]["liquidity_usd"], 9000.0)  # best pair wins
        self.assertEqual(cands[0]["oldest_created"], 900)

    def test_different_addresses_are_different_candidates(self):
        doc = load_json(os.path.join(DS, "search-letro.json"))
        pools = crypto.parse_dexscreener_pairs(doc)
        pools = [p for p in pools if p["symbol"] == "LETRO"]
        cands = clone.group_candidates(pools)
        self.assertEqual(len(cands), 3)
        self.assertEqual({c["token_address"] for c in cands},
                         {"0xaaaa111111111111111111111111111111111111",
                          "letrosolana11111111111111111111111111111111",
                          "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"})


class Socials(unittest.TestCase):
    def test_socials_from_most_liquid_raw_pair(self):
        doc = load_json(os.path.join(DS, "search-letro.json"))
        raw = doc["pairs"]
        cands = clone.group_candidates(crypto.parse_dexscreener_pairs(doc))
        clone.attach_socials(cands, raw)
        real = [c for c in cands if c["chains"] == ["robinhood"]][0]
        self.assertEqual(real["handle"], "letrocoin")
        self.assertEqual(real["website_url"], "https://letro.xyz")
        clone_sol = [c for c in cands if c["chains"] == ["solana"]][0]
        self.assertIsNone(clone_sol["handle"])
        self.assertIsNone(clone_sol["website_url"])


class Scoring(unittest.TestCase):
    def test_oldest_liquid_socials_cg_wins_by_a_clear_gap(self):
        cands = [
            cand("0xreal", chain="base", liquidity=42000.0, volume=251200.0,
                 created=1000, twitter="https://x.com/real", cg=True),
            cand("0xclone1", chain="solana", liquidity=8000.0, volume=12000.0,
                 created=2000, cg=False),
            cand("0xclone2", chain="bsc", liquidity=3000.0, volume=4000.0,
                 created=3000, cg=False),
        ]
        scored = clone.score_candidates(cands, now=NOW)
        self.assertEqual(scored[0]["token_address"], "0xreal")
        self.assertEqual(scored[0]["score"], 2 + 2 + 1 + 1 + 3)
        self.assertEqual(clone.verdict(scored)[0], "REAL")
        self.assertEqual(clone.verdict(scored)[1], "kuat")

    def test_tie_is_belum_jelas(self):
        cands = [
            cand("0xa", liquidity=10000.0, volume=5000.0, created=1000),
            cand("0xb", liquidity=10000.0, volume=5000.0, created=1000),
        ]
        scored = clone.score_candidates(cands, now=NOW)
        self.assertEqual(scored[0]["score"], scored[1]["score"])
        self.assertEqual(clone.verdict(scored)[0], "BELUM JELAS")

    def test_single_candidate_has_no_comparison(self):
        scored = clone.score_candidates([cand("0xonly")], now=NOW)
        self.assertEqual(clone.verdict(scored)[0], "SATU KANDIDAT")

    def test_shared_website_marks_multichain_not_clone(self):
        cands = [
            cand("0xa", chain="base", website="https://same.xyz"),
            cand("0xb", chain="bsc", website="https://same.xyz"),
        ]
        scored = clone.score_candidates(cands, now=NOW)
        self.assertEqual(scored[0].get("shared_with"), scored[1]["token_address"])
        self.assertEqual(scored[1].get("shared_with"), scored[0]["token_address"])

    def test_coingecko_unknown_adds_nothing(self):
        cands = [cand("0xa", liquidity=5000.0, created=1000, cg=None)]
        scored = clone.score_candidates(cands, now=NOW)
        self.assertEqual(scored[0]["score"], 2 + 2 + 1)  # oldest+liq+vol only


class CliOffline(unittest.TestCase):
    def test_letro_fixture_verdict_is_real_robinhood(self):
        code, out, err = run(["LETRO", "--offline", SEARCH_LETRO])
        self.assertEqual(code, 0, err)
        self.assertIn("REAL", out)
        self.assertIn("robinhood", out)
        self.assertIn("3 kandidat", out)
        self.assertNotIn("FROG", out)  # the noise pair is filtered out

    def test_letro_fixture_json(self):
        code, out, err = run(["LETRO", "--offline", SEARCH_LETRO, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"]["label"], "REAL")
        self.assertEqual(len(doc["candidates"]), 3)
        self.assertEqual(doc["candidates"][0]["chains"], ["robinhood"])

    def test_chain_filter(self):
        code, out, err = run(["LETRO", "--offline", SEARCH_LETRO, "--chain", "solana"])
        self.assertEqual(code, 0, err)
        self.assertIn("1 kandidat", out)
        self.assertIn("SATU KANDIDAT", out)

    def test_no_match_exits_1(self):
        code, out, err = run(["NOPE", "--offline", SEARCH_LETRO])
        self.assertEqual(code, 1)
        self.assertIn("tidak ada kandidat", err)

    def test_address_merges_queried_token(self):
        code, out, err = run(["Anthropic", "--offline", SEARCH_ANTHROPIC,
                              "--address", BASE_ANTHROPIC, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["verdict"]["label"], "REAL")
        self.assertEqual(doc["candidates"][0]["chains"], ["solana"])
        queried = [c for c in doc["candidates"]
                   if c["token_address"] == BASE_ANTHROPIC]
        self.assertEqual(len(queried), 1)
        self.assertTrue(queried[0].get("queried"))
        self.assertEqual(queried[0]["score"], 3)  # likuiditas + volume tertinggi
        self.assertEqual(doc["candidates"][1]["token_address"], BASE_ANTHROPIC)

    def test_address_marks_queried_in_human_output(self):
        code, out, err = run(["Anthropic", "--offline", SEARCH_ANTHROPIC,
                              "--address", BASE_ANTHROPIC])
        self.assertEqual(code, 0, err)
        self.assertIn("YANG DITANYAKAN", out)
        self.assertIn("REAL", out)


if __name__ == "__main__":
    unittest.main()
