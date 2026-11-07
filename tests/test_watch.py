"""Tests for the momentum watch entry-trigger re-alert (waiting.json).

watch.py lives outside the day-trader repo, so it is loaded by file path.
The DexScreener price fetch is injected in the re-check tests; the fetch
itself is tested with a stubbed urlopen, never the network.
"""
import importlib.util
import json
import os
import tempfile
import unittest

WATCH = "/home/hatch/workspace/momentum-watch/watch.py"


def load_watch():
    spec = importlib.util.spec_from_file_location("watch", WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watch = load_watch()
NOW = 1794000000


def entry(symbol="CLAUS", chain="robinhood", pair="PAIR1", level=0.001651,
          alerted_at=NOW):
    return {"symbol": symbol, "chain": chain, "pair": pair, "token": "0xabc",
            "entry_level": level, "alerted_at": alerted_at, "triggered": False}


class RecheckWaiting(unittest.TestCase):
    def test_price_above_the_level_triggers_once(self):
        w = {"PAIR1": entry()}
        got, changed = watch.recheck_waiting(w, NOW + 300, fetch=lambda c, p: 0.001700)
        self.assertEqual(len(got), 1)
        self.assertIs(changed, True)
        self.assertIs(w["PAIR1"]["triggered"], True)
        self.assertEqual(w["PAIR1"]["trigger_price"], 0.001700)
        # A second run does not re-alert the same pool.
        got, changed = watch.recheck_waiting(w, NOW + 600, fetch=lambda c, p: 0.001800)
        self.assertEqual(got, [])
        self.assertIs(changed, False)

    def test_price_at_or_below_the_level_stays_waiting(self):
        w = {"PAIR1": entry(), "PAIR2": entry(symbol="X", pair="PAIR2")}
        got, changed = watch.recheck_waiting(w, NOW + 300, fetch=lambda c, p: 0.001651)
        self.assertEqual(got, [])
        self.assertIs(changed, False)
        self.assertIs(w["PAIR1"]["triggered"], False)
        self.assertIs(w["PAIR2"]["triggered"], False)

    def test_an_unreadable_price_never_triggers(self):
        w = {"PAIR1": entry()}
        got, changed = watch.recheck_waiting(w, NOW + 300, fetch=lambda c, p: None)
        self.assertEqual(got, [])
        self.assertIs(changed, False)
        self.assertIs(w["PAIR1"]["triggered"], False)

    def test_entries_older_than_seven_days_are_dropped(self):
        w = {"OLD": entry(pair="OLD", alerted_at=NOW - 8 * 86400),
             "PAIR1": entry()}
        got, changed = watch.recheck_waiting(w, NOW, fetch=lambda c, p: 0.001000)
        self.assertNotIn("OLD", w)
        self.assertIn("PAIR1", w)
        self.assertIs(changed, True)

    def test_a_missing_level_is_skipped(self):
        w = {"PAIR1": entry()}
        del w["PAIR1"]["entry_level"]
        got, changed = watch.recheck_waiting(w, NOW + 300, fetch=lambda c, p: 0.002)
        self.assertEqual(got, [])

    def test_triggered_line_carries_chain_and_pair(self):
        e = entry()
        e["trigger_price"] = 0.001700
        line = watch.triggered_line(e)
        self.assertIn("CLAUS (robinhood)", line)
        self.assertIn("entry $0.001651 tercapai", line)
        self.assertIn("https://dexscreener.com/robinhood/PAIR1", line)


class FetchPairPrice(unittest.TestCase):
    def _fake_urlopen(self, doc):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(doc).encode()

        return lambda req, timeout=20: Resp()

    def test_returns_the_price(self):
        real = watch.urllib.request.urlopen
        watch.urllib.request.urlopen = self._fake_urlopen(
            {"pairs": [{"priceUsd": "0.001426", "baseToken": {"symbol": "CLAUS"}}]})
        try:
            self.assertAlmostEqual(watch.fetch_pair_price("robinhood", "PAIR1"), 0.001426)
        finally:
            watch.urllib.request.urlopen = real

    def test_no_pair_is_none(self):
        real = watch.urllib.request.urlopen
        watch.urllib.request.urlopen = self._fake_urlopen({"pairs": []})
        try:
            self.assertIsNone(watch.fetch_pair_price("robinhood", "PAIR1"))
        finally:
            watch.urllib.request.urlopen = real

    def test_a_network_error_is_none(self):
        real = watch.urllib.request.urlopen

        def boom(req, timeout=20):
            raise watch.urllib.error.URLError("down")

        watch.urllib.request.urlopen = boom
        try:
            self.assertIsNone(watch.fetch_pair_price("robinhood", "PAIR1"))
        finally:
            watch.urllib.request.urlopen = real


class WaitingState(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "waiting.json")
            watch.save_waiting({"PAIR1": entry()}, p)
            self.assertEqual(watch.load_waiting(p), {"PAIR1": entry()})
            self.assertEqual(watch.load_waiting(os.path.join(d, "missing.json")), {})

    def test_scan_limit_is_50(self):
        self.assertIn("--limit", watch.SCAN)
        self.assertEqual(watch.SCAN[watch.SCAN.index("--limit") + 1], "50")


if __name__ == "__main__":
    unittest.main()
