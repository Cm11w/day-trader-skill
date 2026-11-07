import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

signal = load("trader-scanner", "signal")

SIG = os.path.join(FIX, "crypto", "signal")
TOKEN = "0x1630aaaabbbbccccddddeeeeffff00001111ffac"
PAIR = "0x51c9a331b57ac985e5b66849ae04a755a9f6257d2b88ae951f9e5a1c7116e6a7"

# The fixed card: 12 lines, these labels, this order, always. The header
# line is wrapped in *...* (WhatsApp bold); labels ignore the asterisks.
# Lines 1-10 are the original format; line 11 (Bonding) was added
# 7 Nov 2026 at the user's request, line 12 (Kreator) the same day.
LABELS = ["SINYAL", "Harga", "Keaslian", "Entry", "Stop", "Time Machine",
          "Risk", "Komunitas", "Link", "Skor", "Bonding", "Kreator"]


def labels(card):
    return [ln.strip("*").split(":", 1)[0] for ln in card.split("\n")]


def load_json(name):
    with open(os.path.join(SIG, name), encoding="utf-8") as fh:
        return json.load(fh)


def base_facts():
    """A CLAUS-like facts dict: the live numbers from 7 Nov ~18:00 WIB."""
    return {"symbol": "CLAUS", "chain": "robinhood", "pair": PAIR, "token": TOKEN,
            "price_usd": 0.00144541, "change_24h": -26.9, "volume24h": 230500.0,
            "liquidity": 161000.0, "fdv": 1445410.0, "age_hours": 34.6,
            "clone": {"verdict": "BELUM JELAS", "queried_rank": 1,
                      "top_chain": "robinhood", "top_age": "umur 34.7j",
                      "shared": True},
            "entry": {"status": "waiting", "level": 0.001651,
                      "close": 0.00144541, "vs_level_pct": -12.45},
            "exit": {"stop": 0.001491, "target": 0.001971, "target_r": 2.0},
            "tm": {"p_target": 0.68, "avg_R": 0.9},
            "risk": {"verdict": "unknown",
                     "reasons": ["GoPlus tidak mencakup robinhood"]},
            "community": {"handle": "contractclaus", "activity": "UNKNOWN"},
            "bonding": {"status": "na",
                        "line": "Bonding: N/A (bukan bonding launch)"},
            "creator": {"creator": "vg6L6Z5gfermdXCUksdcY91P9yKAGJC5KCjamKxZuuE",
                        "creator_short": "vg6L...ZuuE", "prior": 0, "rugs": 0,
                        "verdict": "FIRST", "modifier": 0, "source": "rugcheck"}}


class FakeRunner:
    """Pretends to be the six sub-tools: returns the fixture JSON for the
    script named in argv, and writes --write targets like the real tools."""

    def __init__(self):
        self.entry = "entry-waiting.json"
        self.risk = "risk-unknown.json"

    def __call__(self, argv, timeout=120):
        script = os.path.basename(argv[1])
        if script == "cmc.py":
            name = {"map": "cmc-map.json", "quotes": "cmc-btc-quotes.json",
                    "metadata": "cmc-btc-metadata.json"}.get(argv[2])
            if name is None:
                return 1, "", "unknown cmc subcommand %s" % argv[2]
            return 0, json.dumps(load_json(name)), ""
        name = {"entry.py": self.entry, "exit.py": "exit-plan.json",
                "timemachine.py": "tm.json", "check.py": self.risk,
                "community.py": "community.json",
                "clone_check.py": "clone-unclear.json"}.get(script)
        if name is None:
            return 1, "", "unknown script %s" % script
        text = json.dumps(load_json(name))
        if "--write" in argv:
            path = argv[argv.index("--write") + 1]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return 0, text, ""


class Score(unittest.TestCase):
    def test_strong_is_100_kuat(self):
        f = base_facts()
        f.update(change_24h=250.0, volume24h=2_000_000.0, liquidity=800_000.0)
        f["risk"] = {"verdict": "pass", "reasons": []}
        f["clone"] = {"verdict": "REAL", "queried_rank": 1,
                      "top_chain": "solana", "top_age": "umur 253.4 hari"}
        f["entry"] = {"status": "triggered", "level": 0.001, "price": 0.0017,
                      "close": 0.0017, "vs_level_pct": 2.96}
        f["tm"] = {"p_target": 0.85, "avg_R": 1.55}
        s = signal.score_signal(f)
        self.assertEqual(s["total"], 100)
        self.assertEqual(s["band"], "KUAT")
        self.assertEqual(s["parts"], {"momentum": 30, "safety": 25,
                                      "authenticity": 20, "entry": 15,
                                      "tm": 10, "creator": 0})

    def test_claus_like_is_45_waspada(self):
        s = signal.score_signal(base_facts())
        self.assertEqual(s["parts"], {"momentum": 8, "safety": 13,
                                      "authenticity": 12, "entry": 5,
                                      "tm": 7, "creator": 0})
        self.assertEqual(s["total"], 45)
        self.assertEqual(s["band"], "WASPADA")

    def test_weak_is_lemah(self):
        f = base_facts()
        f.update(change_24h=-90.0, volume24h=1000.0, liquidity=2000.0)
        f["risk"] = {"verdict": "fail", "reasons": ["honeypot terdeteksi"]}
        f["clone"] = {"verdict": "BELUM JELAS", "queried_rank": 3,
                      "top_chain": "base", "top_age": "umur 2.1j"}
        f["entry"] = {"status": "waiting", "level": 0.01, "close": 0.005,
                      "vs_level_pct": -50.0}
        f["tm"] = {"p_target": 0.2, "avg_R": -0.8}
        s = signal.score_signal(f)
        self.assertEqual(s["parts"], {"momentum": 0, "safety": 0,
                                      "authenticity": 0, "entry": 2,
                                      "tm": 1, "creator": 0})
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["band"], "LEMAH")

    def test_missing_data_scores_neutral_not_zero(self):
        f = base_facts()
        f["risk"] = {"error": "boom"}
        f["clone"] = {"error": "boom"}
        f["entry"] = {"error": "boom"}
        f["tm"] = {"error": "boom"}
        s = signal.score_signal(f)
        self.assertEqual(s["parts"]["safety"], 6 + 7)
        self.assertEqual(s["parts"]["authenticity"], 10)
        self.assertEqual(s["parts"]["entry"], 0)
        self.assertEqual(s["parts"]["tm"], 5)

    def test_real_verdict_but_queried_is_rank_2_scores_5(self):
        # Anthropic robinhood, 7 Nov 18:11 WIB: the REAL verdict belongs to
        # the solana original (rank 1); the queried robinhood pool is rank 2,
        # a clone, and must not inherit the 20 authenticity points.
        f = base_facts()
        f.update(change_24h=826.0, volume24h=8_460_000.0, liquidity=10_220_000.0)
        f["clone"] = {"verdict": "REAL", "queried_rank": 2,
                      "top_chain": "solana", "top_age": "umur 253.4 hari"}
        f["entry"] = {"status": "triggered", "level": 0.000101429,
                      "price": 0.000102, "close": 0.000102, "vs_level_pct": 0.7}
        f["tm"] = {"error": "no full UTC day"}
        s = signal.score_signal(f)
        self.assertEqual(s["parts"]["authenticity"], 5)
        self.assertEqual(s["total"], 30 + 17 + 5 + 15 + 5)
        self.assertEqual(s["band"], "MENARIK")

    def test_queried_rank_3_scores_0(self):
        f = base_facts()
        f["clone"] = {"verdict": "BELUM JELAS", "queried_rank": 3,
                      "top_chain": "base", "top_age": "umur 2.1j"}
        self.assertEqual(signal.score_signal(f)["parts"]["authenticity"], 0)

    def test_band_edges(self):
        for total, band in ((80, "KUAT"), (79, "MENARIK"), (60, "MENARIK"),
                            (59, "WASPADA"), (40, "WASPADA"), (39, "LEMAH")):
            self.assertEqual(signal._band(total), band)


class Render(unittest.TestCase):
    def test_fixed_twelve_lines(self):
        card = signal.render_signal(base_facts())
        lines = card.split("\n")
        self.assertEqual(len(lines), 12)
        self.assertEqual(labels(card), LABELS)
        self.assertTrue(lines[0].startswith("*SINYAL:"))
        self.assertTrue(lines[0].endswith("*"))
        self.assertEqual(lines[10], "Bonding: N/A (bukan bonding launch)")
        self.assertEqual(lines[11],
                         "Kreator: vg6L...ZuuE | token lain: 0 | rug: 0 | FIRST (0)")

    def test_creator_rug_penalises_the_score(self):
        f = base_facts()
        f["creator"] = {"creator": "vg6L6Z5gfermdXCUksdcY91P9yKAGJC5KCjamKxZuuE",
                        "creator_short": "vg6L...ZuuE", "prior": 3, "rugs": 1,
                        "verdict": "RUG", "modifier": -15, "source": "rugcheck"}
        s = signal.score_signal(f)
        self.assertEqual(s["parts"]["creator"], -15)
        self.assertEqual(s["total"], 45 - 15)
        self.assertEqual(s["band"], "LEMAH")
        card = signal.render_signal(f)
        self.assertIn("*SINYAL: CLAUS (robinhood) - Skor 30/100 LEMAH*", card)
        self.assertIn("Kreator: vg6L...ZuuE | token lain: 3 | rug: 1 | RUG (-15)", card)
        self.assertIn("Kreator -15", card)

    def test_creator_clean_bonus_and_clamp(self):
        f = base_facts()
        f["creator"] = {"creator": "vg6L6Z5gfermdXCUksdcY91P9yKAGJC5KCjamKxZuuE",
                        "creator_short": "vg6L...ZuuE", "prior": 2, "rugs": 0,
                        "verdict": "CLEAN", "modifier": 5, "source": "rugcheck"}
        s = signal.score_signal(f)
        self.assertEqual(s["parts"]["creator"], 5)
        self.assertEqual(s["total"], 50)
        self.assertIn("Kreator: vg6L...ZuuE | token lain: 2 | rug: 0 | CLEAN (+5)",
                      signal.render_signal(f))
        # A perfect card plus the clean bonus clamps at 100, never 105.
        g = base_facts()
        g.update(change_24h=250.0, volume24h=2_000_000.0, liquidity=800_000.0)
        g["risk"] = {"verdict": "pass", "reasons": []}
        g["clone"] = {"verdict": "REAL", "queried_rank": 1,
                      "top_chain": "solana", "top_age": "umur 253.4 hari"}
        g["entry"] = {"status": "triggered", "level": 0.001, "price": 0.0017,
                      "close": 0.0017, "vs_level_pct": 2.96}
        g["tm"] = {"p_target": 0.85, "avg_R": 1.55}
        g["creator"] = f["creator"]
        self.assertEqual(signal.score_signal(g)["total"], 100)

    def test_creator_unknown_is_neutral(self):
        f = base_facts()
        f["creator"] = {"creator": None, "verdict": "UNKNOWN", "modifier": 0,
                        "error": "RugCheck down"}
        s = signal.score_signal(f)
        self.assertEqual(s["parts"]["creator"], 0)
        self.assertEqual(s["total"], 45)
        card = signal.render_signal(f)
        self.assertIn("Kreator: UNKNOWN - RugCheck down (0)", card)
        self.assertIn("Kreator 0", card)

    def test_claus_card_content(self):
        card = signal.render_signal(base_facts())
        self.assertIn("*SINYAL: CLAUS (robinhood) - Skor 45/100 WASPADA*", card)
        self.assertIn("Harga: $0.00144541 | 24j -26.9% | Vol $230.5rb | Lik $161.0rb",
                      card)
        self.assertIn("Keaslian: BELUM JELAS - peringkat 1 (token asli, robinhood, umur 34.7j); proyek sama multichain", card)
        self.assertIn("Entry: WAITING - entry $0.001651 (high 24j), close -12.4% di bawahnya", card)
        self.assertIn("Stop: $0.001491 | Target: $0.001971 (2R)", card)
        self.assertIn("Time Machine: target duluan 68.0% | rata-rata +0.90R", card)
        self.assertIn("Risk: UNKNOWN - GoPlus tidak mencakup robinhood", card)
        self.assertIn("Komunitas: @contractclaus | followers: UNKNOWN | aktivitas: UNKNOWN", card)
        self.assertIn("Link: https://dexscreener.com/robinhood/" + PAIR, card)
        self.assertIn("Skor: Momentum 8/30 | Likuiditas-Keamanan 13/25 | Keaslian 12/20 | Entry 5/15 | Time Machine 7/10 | Kreator 0", card)
        self.assertIn("Kreator: vg6L...ZuuE | token lain: 0 | rug: 0 | FIRST (0)", card)

    def test_harga_line_shows_mcap_and_pool_age(self):
        # Market cap sits after FDV; the age is labelled as the POOL's age
        # so it cannot be confused with the original token's age on the
        # Keaslian line. Missing market cap prints UNKNOWN, never $0.
        f = base_facts()
        f["market_cap"] = 900000.0
        card = signal.render_signal(f)
        self.assertIn("FDV $1.45jt | MCap $900.0rb | Umur pool 34.6j", card)
        f2 = base_facts()
        card2 = signal.render_signal(f2)
        self.assertIn("FDV $1.45jt | MCap UNKNOWN | Umur pool 34.6j", card2)

    def test_all_unknown_keeps_the_same_lines(self):
        f = {"symbol": "X", "chain": "base", "pair": "0x1", "token": "0x2",
             "clone": {"error": "boom"}, "entry": {"error": "boom"},
             "exit": {"error": "boom"}, "tm": {"error": "boom"},
             "risk": {"error": "boom"}, "community": {"error": "boom"}}
        card = signal.render_signal(f)
        lines = card.split("\n")
        self.assertEqual(len(lines), 12)
        self.assertEqual(labels(card), LABELS)
        self.assertIn("UNKNOWN", card)
        self.assertIn("Skor 22/100 LEMAH", card)
        # No bonding facts at all -> the Bonding line is UNKNOWN, never dropped.
        self.assertEqual(lines[10],
                         "Bonding: UNKNOWN - tidak ada data bonding")
        # No creator facts at all -> the Kreator line is UNKNOWN, never dropped.
        self.assertEqual(lines[11],
                         "Kreator: UNKNOWN - tidak ada data kreator (0)")

    def test_bonding_line_for_a_bonding_token(self):
        f = base_facts()
        f["bonding"] = {"status": "bonding", "progress": 67.0,
                        "launchpad": "Pump.fun",
                        "line": "Bonding: 67% (belum lulus, Pump.fun)"}
        card = signal.render_signal(f)
        lines = card.split("\n")
        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[10], "Bonding: 67% (belum lulus, Pump.fun)")
        # The first 10 lines keep their exact labels and order.
        self.assertEqual(labels("\n".join(lines[:10])), LABELS[:10])

    def test_bonding_line_for_a_graduate(self):
        f = base_facts()
        f["bonding"] = {"status": "graduated", "progress": 100.0,
                        "launchpad": "Pump.fun",
                        "line": "Bonding: LULUS (PumpSwap)"}
        card = signal.render_signal(f)
        self.assertIn("Bonding: LULUS (PumpSwap)",
                      card.split("\n")[10])

    def test_long_risk_reasons_are_capped(self):
        f = base_facts()
        f["risk"] = {"verdict": "unknown",
                     "reasons": ["top10: no holder data: GoPlus covers EVM chains only; "
                                 "Robinhood honeypot data needs GMGN (API key, out of scope)",
                                 "honeypot: GoPlus covers EVM chains only; "
                                 "Robinhood honeypot data needs GMGN (API key, out of scope)"]}
        card = signal.render_signal(f)
        risk = [ln for ln in card.split("\n") if ln.startswith("Risk:")][0]
        self.assertLessEqual(len(risk), 160)
        self.assertTrue(risk.endswith("..."))
        self.assertIn("top10", risk)

    def test_followers_shown_when_known(self):
        f = base_facts()
        f["community"] = {"handle": "contractclaus", "followers": 1200,
                          "activity": "SEPI"}
        card = signal.render_signal(f)
        self.assertIn("Komunitas: @contractclaus | followers: 1.200 | aktivitas: SEPI",
                      card)

    def test_fmt_int(self):
        self.assertEqual(signal._fmt_int(1200), "1.200")
        self.assertEqual(signal._fmt_int(17500000), "17.500.000")
        self.assertEqual(signal._fmt_int(None), "UNKNOWN")
        self.assertEqual(signal._fmt_int("x"), "UNKNOWN")

    def test_clone_rank_2_shown_in_card(self):
        f = base_facts()
        f["clone"] = {"verdict": "REAL", "queried_rank": 2,
                      "top_chain": "solana", "top_age": "umur 253.4 hari"}
        card = signal.render_signal(f)
        self.assertIn("Keaslian: REAL - peringkat 1 (token asli, solana, umur 253.4 hari); pool ini peringkat 2", card)

    def test_format_never_changes_between_tokens(self):
        f1 = base_facts()
        f2 = dict(f1)
        f2.update(symbol="PEPE", chain="solana", change_24h=250.0,
                  volume24h=2_000_000.0, liquidity=800_000.0)
        f2["risk"] = {"verdict": "pass", "reasons": []}
        f2["clone"] = {"verdict": "REAL", "queried_rank": 1,
                       "top_chain": "solana", "top_age": "umur 253.4 hari"}
        f2["entry"] = {"status": "triggered", "level": 0.001, "price": 0.0017,
                       "close": 0.0017, "vs_level_pct": 2.96}
        f2["tm"] = {"p_target": 0.85, "avg_R": 1.55}
        l1 = labels(signal.render_signal(f1))
        l2 = labels(signal.render_signal(f2))
        self.assertEqual(l1, l2)
        self.assertEqual(l1, LABELS)


class Parse(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRunner()
        self.real = signal._run_cmd
        signal._run_cmd = self.fake

    def tearDown(self):
        signal._run_cmd = self.real

    def test_parse_entry_waiting(self):
        e = signal.run_entry("robinhood", PAIR, "/tmp")
        self.assertEqual(e["status"], "waiting")
        self.assertEqual(e["level"], 0.001651)
        self.assertEqual(e["vs_level_pct"], -12.45)

    def test_parse_entry_triggered(self):
        self.fake.entry = "entry-triggered.json"
        e = signal.run_entry("robinhood", PAIR, "/tmp")
        self.assertEqual(e["status"], "triggered")
        self.assertEqual(e["price"], 0.0017)
        self.assertEqual(e["stop"], 0.00154)

    def test_parse_tm_needs_a_plan(self):
        import tempfile
        t = signal.run_tm("robinhood", PAIR, tempfile.mkdtemp())
        self.assertEqual(t, {"error": "tidak ada rencana exit untuk Time Machine"})

    def test_parse_risk(self):
        r = signal.run_risk("robinhood", PAIR, TOKEN)
        self.assertEqual(r["verdict"], "unknown")
        self.assertIn("GoPlus tidak mencakup robinhood", r["reasons"][0])
        self.fake.risk = "risk-pass.json"
        self.assertEqual(signal.run_risk("base", "0x2", "0x3")["verdict"], "pass")
        self.fake.risk = "risk-fail.json"
        self.assertEqual(signal.run_risk("base", "0x4", "0x5")["verdict"], "fail")

    def test_parse_community_no_mentions_is_unknown(self):
        c = signal.run_community(load_json("dexscreener-pairs.json"),
                                 "robinhood", TOKEN)
        self.assertEqual(c["handle"], "contractclaus")
        self.assertEqual(c["followers"], 1200)
        self.assertEqual(c["activity"], "UNKNOWN")

    def test_parse_clone(self):
        c = signal.run_clone("CLAUS", TOKEN)
        self.assertEqual(c["verdict"], "BELUM JELAS")
        self.assertEqual(c["queried_rank"], 1)
        self.assertTrue(c["shared"])


class CliOffline(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRunner()
        self.real = signal._run_cmd
        signal._run_cmd = self.fake

    def tearDown(self):
        signal._run_cmd = self.real

    def test_offline_card_is_the_fixed_format(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = signal.main(["--token", TOKEN, "--offline", SIG])
        self.assertEqual(code, 0)
        # The card is still exactly 12 lines; the Top 10 Holder block
        # follows it in the same message, after one blank line.
        lines = out.getvalue().strip().split("\n")
        self.assertEqual(labels("\n".join(lines[:12])), LABELS)
        self.assertIn("*SINYAL: CLAUS (robinhood) - Skor 45/100 WASPADA*", lines[0])
        self.assertEqual(lines[12], "")
        self.assertTrue(lines[13].startswith("Top 10 Holder"))

    def test_offline_json(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = signal.main(["--token", TOKEN, "--offline", SIG, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["score"]["total"], 45)
        self.assertEqual(doc["score"]["band"], "WASPADA")
        self.assertEqual(doc["pair"], PAIR)

    def test_offline_picks_the_most_liquid_pool(self):
        pool, _, err = signal.fetch_pool(TOKEN, offline=SIG)
        self.assertIsNone(err)
        self.assertEqual(pool["pair_address"], PAIR)
        self.assertEqual(pool["liquidity_usd"], 161000.0)

    def test_pair_match_is_case_insensitive(self):
        # seen.json menyimpan alamat pair huruf kecil, DexScreener
        # mengembalikan checksummed - pencocokan harus case-insensitive.
        pool, _, err = signal.fetch_pool(TOKEN, offline=SIG,
                                         pair=PAIR.upper())
        self.assertIsNone(err)
        self.assertEqual(pool["pair_address"], PAIR)

    def test_resolve_token_from_pair(self):
        real = signal.crypto.fetch_json
        signal.crypto.fetch_json = lambda path: load_json("dexscreener-pairs.json")
        try:
            self.assertEqual(signal.resolve_token("robinhood", PAIR), TOKEN)
        finally:
            signal.crypto.fetch_json = real

    def test_resolve_token_no_pair(self):
        real = signal.crypto.fetch_json
        signal.crypto.fetch_json = lambda path: {"pairs": []}
        try:
            with self.assertRaises(ValueError):
                signal.resolve_token("robinhood", PAIR)
        finally:
            signal.crypto.fetch_json = real

    def test_pair_only_main(self):
        real = signal.crypto.fetch_json
        signal.crypto.fetch_json = lambda path: load_json("dexscreener-pairs.json")
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = signal.main(["--chain", "robinhood", "--pair", PAIR,
                                    "--offline", SIG])
        finally:
            signal.crypto.fetch_json = real
        self.assertEqual(code, 0)
        self.assertIn("*SINYAL: CLAUS (robinhood) - Skor 45/100 WASPADA*",
                      out.getvalue())


BTC_TOKEN = "0xFaF1C31F6855E91B1a905761C81A79e4966e4709"  # Bitcoin Base on CMC


class ParseCmc(unittest.TestCase):
    def test_map_matches_by_address_not_symbol(self):
        doc = load_json("cmc-btc-map.json")
        listed, cmc_id = signal._parse_cmc_map(doc, BTC_TOKEN)
        self.assertIs(listed, True)
        self.assertEqual(cmc_id, 38552)
        # The same symbol with a different address is a different coin.
        listed, cmc_id = signal._parse_cmc_map(
            doc, "0x0000000000000000000000000000000000000000")
        self.assertIs(listed, False)
        self.assertIsNone(cmc_id)

    def test_map_empty_is_not_listed(self):
        listed, cmc_id = signal._parse_cmc_map(load_json("cmc-map.json"), TOKEN)
        self.assertIs(listed, False)
        self.assertIsNone(cmc_id)

    def test_quotes_parse(self):
        price, mcap, circ, total = signal._parse_cmc_quotes(
            load_json("cmc-btc-quotes.json"))
        self.assertAlmostEqual(price, 75932.17, places=1)
        self.assertEqual(circ, 20109115)
        self.assertEqual(total, 20109115)

    def test_metadata_twitter_handle(self):
        # The BTC metadata fixture has no Twitter URL: no handle is invented.
        self.assertIsNone(signal._parse_cmc_metadata(
            load_json("cmc-btc-metadata.json")))
        doc = {"data": {"1": {"id": 1, "urls": {"twitter": ["https://x.com/somehandle"]}}}}
        self.assertEqual(signal._parse_cmc_metadata(doc), "somehandle")


class RunCmc(unittest.TestCase):
    def setUp(self):
        self.real = signal._run_json
        signal._run_json = self.fake_cmc

    def tearDown(self):
        signal._run_json = self.real

    def fake_cmc(self, argv, timeout=120):
        name = {"map": "cmc-btc-map.json", "quotes": "cmc-btc-quotes.json",
                "metadata": "cmc-btc-metadata.json"}[argv[2]]
        return load_json(name), None

    def test_listed_token_returns_id_and_quote(self):
        got = signal.run_cmc("BTC", BTC_TOKEN, "base")
        self.assertIs(got["listed"], True)
        self.assertEqual(got["cmc_id"], 38552)
        self.assertIsNotNone(got["price_usd"])
        self.assertEqual(got["circulating_supply"], 20109115)

    def test_unlisted_symbol_is_not_listed(self):
        # A symbol CMC does not know answers 400: not listed, not an error.
        def fake_400(argv, timeout=120):
            return None, "urllib.error.HTTPError: HTTP Error 400: Bad Request"
        signal._run_json = fake_400
        got = signal.run_cmc("CLAUS", TOKEN, "robinhood")
        self.assertIs(got["listed"], False)
        self.assertIsNone(got["error"])

    def test_a_real_cmc_failure_is_an_error(self):
        def fake_boom(argv, timeout=120):
            return None, "connection timed out"
        signal._run_json = fake_boom
        got = signal.run_cmc("BTC", BTC_TOKEN, "base")
        self.assertIsNone(got["listed"])
        self.assertIsNotNone(got["error"])

    def test_offline_map_empty_is_not_listed(self):
        got = signal.run_cmc("CLAUS", TOKEN, "robinhood", offline=SIG)
        self.assertIs(got["listed"], False)


class CmcScoreAndRender(unittest.TestCase):
    def test_belum_jelas_rank1_listed_scores_15(self):
        f = base_facts()
        f["clone"] = {"verdict": "BELUM JELAS", "queried_rank": 1,
                      "top_chain": "base", "top_age": "umur 10 hari",
                      "cmc_listed": True}
        self.assertEqual(signal.score_signal(f)["parts"]["authenticity"], 15)

    def test_belum_jelas_rank1_unlisted_scores_12(self):
        f = base_facts()
        f["clone"] = {"verdict": "BELUM JELAS", "queried_rank": 1,
                      "top_chain": "base", "top_age": "umur 10 hari",
                      "cmc_listed": False}
        self.assertEqual(signal.score_signal(f)["parts"]["authenticity"], 12)

    def test_rank2_clone_gets_no_cmc_bump(self):
        f = base_facts()
        f["clone"] = {"verdict": "REAL", "queried_rank": 2,
                      "top_chain": "solana", "top_age": "umur 253.4 hari",
                      "cmc_listed": True}
        self.assertEqual(signal.score_signal(f)["parts"]["authenticity"], 5)

    def test_keaslian_line_notes_the_cmc_listing(self):
        f = base_facts()
        f["clone"] = {"verdict": "BELUM JELAS", "queried_rank": 1,
                      "top_chain": "base", "top_age": "umur 10 hari",
                      "cmc_listed": True}
        card = signal.render_signal(f)
        self.assertIn("Keaslian: BELUM JELAS - peringkat 1 (token asli, base, umur 10 hari); terdaftar di CMC",
                      card)

    def test_community_handle_falls_back_to_cmc(self):
        comm = {"handle": None, "followers": 1200, "activity": "UNKNOWN",
                "posts": None, "kols": 0, "circulating_supply": None,
                "total_supply": None}
        got = signal.apply_cmc_handle(comm, "somehandle")
        self.assertEqual(got["handle"], "somehandle")
        self.assertEqual(got["followers"], 1200)

    def test_community_handle_is_not_overwritten(self):
        comm = {"handle": "contractclaus", "followers": 1200, "activity": "UNKNOWN",
                "posts": None, "kols": 0, "circulating_supply": None,
                "total_supply": None}
        got = signal.apply_cmc_handle(comm, "somehandle")
        self.assertEqual(got["handle"], "contractclaus")


class Holders(unittest.TestCase):
    def test_conversion_by_balance_over_circulating(self):
        h = {"address": "A" * 40, "percent": 0.005, "balance": 5000000.0}
        self.assertAlmostEqual(signal._holder_circ_pct(h, 500000000.0, 1000000000.0), 1.0)

    def test_conversion_by_total_over_circulating(self):
        h = {"address": "A" * 40, "percent": 0.005, "balance": None}
        self.assertAlmostEqual(signal._holder_circ_pct(h, 500000000.0, 1000000000.0), 1.0)

    def test_no_supply_uses_the_api_percent(self):
        h = {"address": "A" * 40, "percent": 0.0546, "balance": None}
        self.assertAlmostEqual(signal._holder_circ_pct(h, None, None), 5.46)

    def test_no_number_is_none(self):
        self.assertIsNone(signal._holder_circ_pct(
            {"address": "A", "percent": None, "balance": None}, None, None))

    def test_holder_rows_sort_and_cap_at_ten(self):
        f = {"risk": {"holders": [{"address": "H%02d" % i, "percent": 0.01 * i,
                                    "balance": None} for i in range(1, 13)],
                    "total_supply": None},
             "cmc": {}, "community": {}}
        rows = signal.holder_rows(f)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["address"], "H12")
        self.assertAlmostEqual(rows[0]["pct"], 12.0)

    def test_holder_rows_empty_without_data(self):
        self.assertEqual(signal.holder_rows({"risk": {"holders": []}, "cmc": {},
                                             "community": {}}), [])

    def test_render_holders_block(self):
        f = {"holders": [{"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                          "pct": 5.46},
                         {"address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                          "pct": 9.49}]}
        block = signal.render_holders(f)
        self.assertEqual(block, "Top 10 Holder:\n1. DezX...B263 - 9.49%\n"
                                "2. 0x83...2913 - 5.46%")

    def test_render_holders_unknown_without_data(self):
        self.assertEqual(signal.render_holders({"holders": []}),
                         "Top 10 Holder: UNKNOWN - tidak ada data holder")
        self.assertEqual(signal.render_holders({}),
                         "Top 10 Holder: UNKNOWN - tidak ada data holder")

    def test_short_address(self):
        self.assertEqual(signal._short_addr(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"), "0x83...2913")
        self.assertEqual(signal._short_addr("short"), "short")

    def test_tiny_percent_keeps_precision(self):
        self.assertEqual(signal._pct2(0.004), "0.0040")
        self.assertEqual(signal._pct2(5.46), "5.46")


class RecordWaiting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="waiting-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self):
        return os.path.join(self.tmp, "waiting.json")

    def waiting_facts(self, status="waiting", level=0.001651):
        f = base_facts()
        f["entry"] = {"status": status, "level": level, "close": 0.001446,
                      "vs_level_pct": -12.4, "price": None, "stop": None,
                      "target": None, "reason": ""}
        return f

    def test_records_a_waiting_pool(self):
        p = self.path()
        self.assertTrue(signal.record_waiting(self.waiting_facts(), p, now=1794000000))
        doc = json.load(open(p))
        self.assertEqual(list(doc), [PAIR])
        e = doc[PAIR]
        self.assertEqual((e["symbol"], e["chain"], e["entry_level"],
                          e["alerted_at"], e["triggered"]),
                         ("CLAUS", "robinhood", 0.001651, 1794000000, False))

    def test_triggered_pool_is_not_recorded(self):
        self.assertFalse(signal.record_waiting(
            self.waiting_facts(status="triggered"), self.path()))

    def test_no_level_is_not_recorded(self):
        self.assertFalse(signal.record_waiting(
            self.waiting_facts(level=None), self.path()))

    def test_triggered_entry_is_never_resurrected(self):
        p = self.path()
        signal.record_waiting(self.waiting_facts(), p, now=1794000000)
        doc = json.load(open(p))
        doc[PAIR]["triggered"] = True
        doc[PAIR]["triggered_at"] = 1794000600
        json.dump(doc, open(p, "w"))
        self.assertFalse(signal.record_waiting(self.waiting_facts(), p, now=1794001200))
        self.assertTrue(json.load(open(p))[PAIR]["triggered"])

    def test_same_level_keeps_the_original_alert_time(self):
        p = self.path()
        signal.record_waiting(self.waiting_facts(), p, now=1794000000)
        self.assertFalse(signal.record_waiting(self.waiting_facts(), p, now=1794001200))
        self.assertEqual(json.load(open(p))[PAIR]["alerted_at"], 1794000000)

    def test_a_new_level_updates_the_entry(self):
        p = self.path()
        signal.record_waiting(self.waiting_facts(level=0.001651), p, now=1794000000)
        self.assertTrue(signal.record_waiting(
            self.waiting_facts(level=0.001800), p, now=1794001200))
        e = json.load(open(p))[PAIR]
        self.assertEqual((e["entry_level"], e["alerted_at"]), (0.001800, 1794001200))


class RecordWaitingCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="waiting-cli-")
        self.real = signal._run_cmd
        signal._run_cmd = FakeRunner()

    def tearDown(self):
        signal._run_cmd = self.real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_offline_run_records_the_waiting_entry(self):
        p = os.path.join(self.tmp, "waiting.json")
        out = io.StringIO()
        with redirect_stdout(out):
            code = signal.main(["--token", TOKEN, "--offline", SIG,
                                "--record-waiting", p])
        self.assertEqual(code, 0)
        doc = json.load(open(p))
        self.assertEqual(list(doc), [PAIR])
        e = doc[PAIR]
        self.assertEqual(e["symbol"], "CLAUS")
        self.assertEqual(e["chain"], "robinhood")
        self.assertAlmostEqual(e["entry_level"], 0.001651)
        self.assertIs(e["triggered"], False)
        self.assertIsInstance(e["alerted_at"], int)


if __name__ == "__main__":
    unittest.main()
