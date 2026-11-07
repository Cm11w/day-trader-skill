"""Creator check: deployer history and rug verdicts.

The creator module is pure over the free fetchers in crypto.py, so the
tests stub those fetchers instead of touching the network. Live behaviour
was verified 7 Nov 2026: RugCheck gives creator + creatorTokens for
Solana, GoPlus gives creator_address + honeypot_with_same_creator for
EVM (incl. robinhood 4663).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-scanner"))
import creator  # noqa: E402
import crypto  # noqa: E402


SOL_TOKEN = "FHCntFVzzGmbcUNmf1jBGaBsi4duboB1cwjg4yrcpump"
SOL_CREATOR = "vg6L6Z5gfermdXCUksdcY91P9yKAGJC5KCjamKxZuuE"
EVM_TOKEN = "0x5cbB5a5b43976d560002F553E3275fe687a4FBFA"
EVM_CREATOR = "0x66E4F311b7FD077911F35d4A2fEbC6a2014fCe24"


def rugcheck_doc(creator=SOL_CREATOR, creator_tokens=None, rugged=False):
    return {"mint": SOL_TOKEN, "creator": creator,
            "creatorTokens": creator_tokens if creator_tokens is not None else [],
            "rugged": rugged, "graphInsidersDetected": 3}


def goplus_doc(creator=EVM_CREATOR, same="0", pct="1.000000"):
    return {"code": 1, "result": {EVM_TOKEN.lower(): {
        "creator_address": creator, "honeypot_with_same_creator": same,
        "creator_percent": pct, "is_honeypot": "0"}}}


class TestPriorMints(unittest.TestCase):
    def test_dicts_and_strings(self):
        toks = [{"mint": "AAA"}, "BBB", {"address": "CCC"}, {"token": "DDD"},
                {"nope": "EEE"}, None, 42]
        self.assertEqual(creator._prior_mints(toks, "ZZZ"),
                         ["AAA", "BBB", "CCC", "DDD"])

    def test_self_excluded(self):
        self.assertEqual(creator._prior_mints([{"mint": SOL_TOKEN}, "OTHER"],
                                              SOL_TOKEN), ["OTHER"])

    def test_empty(self):
        self.assertEqual(creator._prior_mints(None, SOL_TOKEN), [])
        self.assertEqual(creator._prior_mints([], SOL_TOKEN), [])


class TestSolana(unittest.TestCase):
    def test_first_launch(self):
        with patch.object(crypto, "fetch_rugcheck",
                          return_value=rugcheck_doc()):
            out = creator.check_creator("solana", SOL_TOKEN)
        self.assertEqual(out["verdict"], "FIRST")
        self.assertEqual(out["modifier"], 0)
        self.assertEqual(out["creator"], SOL_CREATOR)
        self.assertEqual(out["prior"], 0)
        self.assertEqual(out["rugs"], 0)

    def test_clean_prior_tokens(self):
        prior = [{"mint": "AAA111"}, {"mint": "BBB222"}]
        def fake_rugcheck(mint, pause=0):
            if mint == SOL_TOKEN:
                return rugcheck_doc(creator_tokens=prior)
            return rugcheck_doc(creator=SOL_CREATOR, rugged=False)
        with patch.object(crypto, "fetch_rugcheck", side_effect=fake_rugcheck):
            out = creator.check_creator("solana", SOL_TOKEN)
        self.assertEqual(out["verdict"], "CLEAN")
        self.assertEqual(out["modifier"], 5)
        self.assertEqual(out["prior"], 2)
        self.assertEqual(out["rugs"], 0)
        self.assertEqual(out["checked"], 2)

    def test_rug_prior_token(self):
        prior = [{"mint": "AAA111"}, {"mint": "BBB222"}]
        def fake_rugcheck(mint, pause=0):
            if mint == SOL_TOKEN:
                return rugcheck_doc(creator_tokens=prior)
            if mint == "AAA111":
                return rugcheck_doc(creator=SOL_CREATOR, rugged=True)
            return rugcheck_doc(creator=SOL_CREATOR, rugged=False)
        with patch.object(crypto, "fetch_rugcheck", side_effect=fake_rugcheck):
            out = creator.check_creator("solana", SOL_TOKEN)
        self.assertEqual(out["verdict"], "RUG")
        self.assertEqual(out["modifier"], -15)
        self.assertEqual(out["rugs"], 1)

    def test_rugcheck_failure_is_unknown(self):
        with patch.object(crypto, "fetch_rugcheck",
                          side_effect=crypto.CryptoError("RugCheck down")):
            out = creator.check_creator("solana", SOL_TOKEN)
        self.assertEqual(out["verdict"], "UNKNOWN")
        self.assertEqual(out["modifier"], 0)
        self.assertIn("RugCheck down", out["error"])

    def test_no_creator_is_unknown(self):
        with patch.object(crypto, "fetch_rugcheck",
                          return_value=rugcheck_doc(creator=None)):
            out = creator.check_creator("solana", SOL_TOKEN)
        self.assertEqual(out["verdict"], "UNKNOWN")
        self.assertEqual(out["modifier"], 0)

    def test_prior_check_capped_at_five(self):
        prior = [{"mint": "T%d" % i} for i in range(8)]
        calls = []
        def fake_rugcheck(mint, pause=0):
            calls.append(mint)
            if mint == SOL_TOKEN:
                return rugcheck_doc(creator_tokens=prior)
            return rugcheck_doc(creator=SOL_CREATOR, rugged=False)
        with patch.object(crypto, "fetch_rugcheck", side_effect=fake_rugcheck):
            out = creator.check_creator("solana", SOL_TOKEN)
        self.assertEqual(out["checked"], 5)
        self.assertEqual(out["prior"], 8)

    def test_dead_prior_token_via_dexscreener(self):
        # RugCheck has no report for the prior token; DexScreener shows
        # no pairs -> dead -> counts as a rug.
        prior = [{"mint": "DEAD111"}]
        def fake_rugcheck(mint, pause=0):
            if mint == SOL_TOKEN:
                return rugcheck_doc(creator_tokens=prior)
            raise crypto.CryptoError("no report")
        def fake_ds(addr, pause=0):
            return {"pairs": []}
        with patch.object(crypto, "fetch_rugcheck", side_effect=fake_rugcheck), \
             patch.object(crypto, "fetch_dexscreener_token", side_effect=fake_ds):
            out = creator.check_creator("solana", SOL_TOKEN)
        self.assertEqual(out["verdict"], "RUG")
        self.assertEqual(out["rugs"], 1)


class TestEVM(unittest.TestCase):
    def test_honeypot_same_creator_is_rug(self):
        with patch.object(crypto, "fetch_goplus_security",
                          return_value=goplus_doc(same="1")):
            out = creator.check_creator("robinhood", EVM_TOKEN)
        self.assertEqual(out["verdict"], "RUG")
        self.assertEqual(out["modifier"], -15)
        self.assertTrue(out["honeypot_same_creator"])
        self.assertEqual(out["creator"], EVM_CREATOR)

    def test_no_honeypot_same_creator_is_neutral(self):
        with patch.object(crypto, "fetch_goplus_security",
                          return_value=goplus_doc(same="0")):
            out = creator.check_creator("robinhood", EVM_TOKEN)
        self.assertEqual(out["verdict"], "UNKNOWN")
        self.assertEqual(out["modifier"], 0)
        self.assertFalse(out["honeypot_same_creator"])
        self.assertEqual(out["creator"], EVM_CREATOR)

    def test_no_goplus_record_is_unknown(self):
        with patch.object(crypto, "fetch_goplus_security",
                          return_value={"code": 1, "result": {}}):
            out = creator.check_creator("bsc", EVM_TOKEN)
        self.assertEqual(out["verdict"], "UNKNOWN")
        self.assertEqual(out["creator"], None)

    def test_chain_without_goplus_is_unknown(self):
        out = creator.check_creator("solana2", EVM_TOKEN)
        self.assertEqual(out["verdict"], "UNKNOWN")


class TestCreatorLine(unittest.TestCase):
    def test_solana_first(self):
        line = creator.creator_line(
            {"creator": SOL_CREATOR, "creator_short": "vg6L...ZuuE",
             "prior": 0, "rugs": 0, "verdict": "FIRST", "modifier": 0,
             "source": "rugcheck"})
        self.assertEqual(line, "Kreator: vg6L...ZuuE | token lain: 0 | rug: 0 | FIRST (0)")

    def test_solana_rug(self):
        line = creator.creator_line(
            {"creator": SOL_CREATOR, "creator_short": "vg6L...ZuuE",
             "prior": 3, "rugs": 1, "verdict": "RUG", "modifier": -15,
             "source": "rugcheck"})
        self.assertEqual(line, "Kreator: vg6L...ZuuE | token lain: 3 | rug: 1 | RUG (-15)")

    def test_solana_clean_bonus(self):
        line = creator.creator_line(
            {"creator": SOL_CREATOR, "creator_short": "vg6L...ZuuE",
             "prior": 2, "rugs": 0, "verdict": "CLEAN", "modifier": 5,
             "source": "rugcheck"})
        self.assertEqual(line, "Kreator: vg6L...ZuuE | token lain: 2 | rug: 0 | CLEAN (+5)")

    def test_evm_unknown_shows_creator(self):
        line = creator.creator_line(
            {"creator": EVM_CREATOR, "creator_short": "0x66...Ce24",
             "verdict": "UNKNOWN", "modifier": 0, "source": "goplus",
             "honeypot_same_creator": False})
        self.assertEqual(
            line, "Kreator: 0x66...Ce24 | honeypot kreator sama: tidak | UNKNOWN (0)")

    def test_no_creator(self):
        line = creator.creator_line(
            {"creator": None, "verdict": "UNKNOWN", "modifier": 0,
             "error": "RugCheck down"})
        self.assertEqual(line, "Kreator: UNKNOWN - RugCheck down (0)")


if __name__ == "__main__":
    unittest.main()
