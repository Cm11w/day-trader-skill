import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import FIX, load

community = load("trader-news", "community")
DIR = os.path.join(FIX, "trader-news")
NOW = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc)

SOCIALS = {"twitter_url": "https://x.com/cogecoin", "handle": "cogecoin",
           "telegram_url": "https://t.me/cogecoin", "discord_url": None,
           "website_url": "https://coge.example"}
CG = {"coin_id": "coge-coin-2", "symbol": "COGE", "name": "COGE COIN",
      "twitter_handle": "cogecoin", "twitter_followers": 12345,
      "telegram_users": 2345, "reddit_subscribers": 321, "facebook_likes": None}


def load_mentions():
    with open(os.path.join(DIR, "mentions.json"), encoding="utf-8") as fh:
        return json.load(fh)["mentions"]


class Official(unittest.TestCase):
    def test_matching_handles_confirm_each_other(self):
        got = community.assess_official(SOCIALS, CG)
        self.assertTrue(got["has_x"])
        self.assertEqual(got["handle"], "cogecoin")
        self.assertEqual(got["url"], "https://x.com/cogecoin")
        self.assertEqual(got["source"], "both")
        self.assertTrue(got["confirmed_by_both"])

    def test_dexscreener_only_is_still_official(self):
        got = community.assess_official(SOCIALS, {"twitter_handle": None})
        self.assertEqual(got["source"], "dexscreener")
        self.assertFalse(got["confirmed_by_both"])

    def test_coingecko_only_is_still_official(self):
        got = community.assess_official({"twitter_url": None, "handle": None}, CG)
        self.assertEqual(got["source"], "coingecko")
        self.assertEqual(got["handle"], "cogecoin")
        self.assertEqual(got["url"], "https://x.com/cogecoin")

    def test_mismatched_handles_trust_dexscreener(self):
        cg = dict(CG, twitter_handle="notcoge")
        got = community.assess_official(SOCIALS, cg)
        self.assertEqual(got["handle"], "cogecoin")
        self.assertEqual(got["source"], "dexscreener")
        self.assertFalse(got["confirmed_by_both"])

    def test_no_handle_anywhere(self):
        got = community.assess_official({"twitter_url": None, "handle": None},
                                        {"twitter_handle": None})
        self.assertFalse(got["has_x"])
        self.assertIsNone(got["handle"])
        self.assertIsNone(got["source"])
        self.assertFalse(got["confirmed_by_both"])


class Presence(unittest.TestCase):
    def test_known_when_a_number_exists(self):
        got = community.assess_presence(CG)
        self.assertEqual(got["twitter_followers"], 12345)
        self.assertEqual(got["telegram_users"], 2345)
        self.assertEqual(got["status"], "known")

    def test_unknown_is_none_never_zero(self):
        got = community.assess_presence({"twitter_followers": None, "telegram_users": None})
        self.assertIsNone(got["twitter_followers"])
        self.assertEqual(got["status"], "unknown")


class Activity(unittest.TestCase):
    def test_window_medians_and_engagement(self):
        got = community.assess_activity(load_mentions(), window_days=7, now=NOW,
                                        followers=12345)
        self.assertEqual(got["posts"], 6)  # the 20 Oct post is outside the window
        self.assertAlmostEqual(got["posts_per_day"], 0.86, places=2)
        self.assertEqual(got["median_likes"], 160)
        self.assertEqual(got["median_reposts"], 40)
        self.assertEqual(got["median_replies"], 17.5)
        self.assertAlmostEqual(got["engagement_rate_pct"], 1.62, places=2)
        self.assertEqual(got["status"], "known")

    def test_no_mentions_is_unknown_not_zero(self):
        got = community.assess_activity([], window_days=7, now=NOW, followers=12345)
        self.assertEqual(got["posts"], 0)
        self.assertIsNone(got["median_likes"])
        self.assertIsNone(got["engagement_rate_pct"])
        self.assertEqual(got["status"], "unknown")

    def test_zero_posts_in_window_is_known_dormant(self):
        old = [dict(m, time="2026-10-01T10:00:00Z") for m in load_mentions()]
        got = community.assess_activity(old, window_days=7, now=NOW, followers=12345)
        self.assertEqual(got["posts"], 0)
        self.assertEqual(got["status"], "known")

    def test_undated_mentions_are_unknown_not_dormant(self):
        got = community.assess_activity(
            [{"author_handle": "x", "time": None, "likes": None, "reposts": None,
              "replies": None}], window_days=7, now=NOW, followers=12345)
        self.assertEqual(got["posts"], 0)
        self.assertEqual(got["status"], "unknown")

    def test_no_followers_means_no_engagement_rate(self):
        got = community.assess_activity(load_mentions(), window_days=7, now=NOW)
        self.assertIsNone(got["engagement_rate_pct"])
        self.assertEqual(got["status"], "known")


class Authenticity(unittest.TestCase):
    def _base(self):
        official = community.assess_official(SOCIALS, CG)
        presence = community.assess_presence(CG)
        activity = community.assess_activity(load_mentions(), window_days=7, now=NOW,
                                             followers=12345)
        return official, presence, activity

    def test_solid_when_nothing_is_wrong(self):
        got = community.assess_authenticity(*self._base())
        self.assertEqual(got["verdict"], "solid")
        self.assertEqual(got["flags"], [])

    def test_low_engagement_for_follower_count(self):
        official, presence, activity = self._base()
        activity = dict(activity, engagement_rate_pct=0.01)
        got = community.assess_authenticity(official, presence, activity)
        self.assertEqual(got["verdict"], "mixed")
        self.assertIn("low engagement for follower count (possible bought or inactive followers)",
                      got["flags"])
        self.assertIn("never proof of fakery", got["disclaimer"])

    def test_reply_heavy_engagement(self):
        official, presence, activity = self._base()
        activity = dict(activity, median_likes=10, median_replies=25)
        got = community.assess_authenticity(official, presence, activity)
        self.assertEqual(got["verdict"], "mixed")
        self.assertIn("reply-heavy engagement (possible reply pods or bot replies)", got["flags"])

    def test_dormant_account(self):
        official, presence, activity = self._base()
        activity = dict(activity, posts=0, median_likes=None, median_reposts=None,
                        median_replies=None, engagement_rate_pct=None)
        got = community.assess_authenticity(official, presence, activity)
        self.assertEqual(got["verdict"], "mixed")
        self.assertIn("dormant account (no posts in 7 days)", got["flags"])

    def test_two_flags_is_suspect(self):
        official, presence, activity = self._base()
        activity = dict(activity, engagement_rate_pct=0.01, median_likes=10, median_replies=25)
        got = community.assess_authenticity(official, presence, activity)
        self.assertEqual(got["verdict"], "suspect")
        self.assertEqual(len(got["flags"]), 2)

    def test_unknown_activity_is_unknown(self):
        official, presence, _ = self._base()
        activity = community.assess_activity([], window_days=7, now=NOW, followers=12345)
        got = community.assess_authenticity(official, presence, activity)
        self.assertEqual(got["verdict"], "unknown")

    def test_huge_followers_with_unknown_age_is_a_note_not_a_flag(self):
        official, presence, activity = self._base()
        presence = dict(presence, twitter_followers=250000)
        got = community.assess_authenticity(official, presence, activity)
        self.assertEqual(got["flags"], [])
        self.assertEqual(got["verdict"], "solid")
        self.assertTrue(any("100000" in n for n in got["notes"]))


class Kols(unittest.TestCase):
    def test_kols_above_the_threshold_sorted_by_followers(self):
        kols = community.find_kols(load_mentions(), exclude="cogecoin")
        self.assertEqual([k["handle"] for k in kols], ["cryptowhale"])
        self.assertEqual(kols[0]["followers"], 50000)
        self.assertEqual(kols[0]["mentions"], 2)
        self.assertEqual(kols[0]["sample_url"], "https://x.com/cryptowhale/status/201")

    def test_at_prefixed_and_mixed_case_handles_dedupe_and_exclude(self):
        mentions = [
            {"author_handle": "@CryptoWhale", "author_followers": 50000, "url": "u1"},
            {"author_handle": "cryptowhale", "author_followers": 40000, "url": "u2"},
            {"author_handle": "CRYPTOWHALE", "author_followers": 60000, "url": "u3"},
            {"author_handle": "@Cogecoin", "author_followers": 90000, "url": "u4"},
        ]
        kols = community.find_kols(mentions, exclude="cogecoin")
        self.assertEqual([k["handle"] for k in kols], ["CryptoWhale"])
        self.assertEqual(kols[0]["followers"], 60000)
        self.assertEqual(kols[0]["mentions"], 3)
        # the official account under an @ and a different case is excluded, not a KOL
        self.assertNotIn("Cogecoin", [k["handle"] for k in kols])
        kols = community.find_kols(mentions, exclude="@COGECOIN")
        self.assertEqual([k["handle"] for k in kols], ["CryptoWhale"])

    def test_threshold_is_configurable(self):
        kols = community.find_kols(load_mentions(), min_followers=400, exclude="cogecoin")
        self.assertEqual([k["handle"] for k in kols], ["cryptowhale", "tinytrader"])

    def test_kol_history_counts_alive_and_dead(self):
        kols = [{"handle": "cryptowhale", "followers": 50000, "mentions": 2,
                 "sample_url": "https://x.com/cryptowhale/status/201",
                 "tokens_mentioned": ["ALIVE", "DEAD"]}]
        status = {"ALIVE": {"liquidity_usd": 50000.0, "volume24h_usd": 10000.0}}
        got = community.assess_kol_history(kols, status)
        self.assertEqual(got[0]["alive"], 1)
        self.assertEqual(got[0]["dead"], 1)  # DEAD is missing from the status map
        self.assertEqual(got[0]["status"], "partial")

    def test_zero_liquidity_and_zero_volume_is_dead(self):
        kols = [{"handle": "x", "followers": 10000, "mentions": 1, "sample_url": "",
                 "tokens_mentioned": ["RUGGED"]}]
        status = {"RUGGED": {"liquidity_usd": 0.0, "volume24h_usd": 0.0}}
        got = community.assess_kol_history(kols, status)
        self.assertEqual(got[0]["dead"], 1)
        self.assertEqual(got[0]["status"], "checked")

    def test_no_tokens_mentioned_is_unknown(self):
        kols = [{"handle": "x", "followers": 10000, "mentions": 1, "sample_url": "",
                 "tokens_mentioned": []}]
        got = community.assess_kol_history(kols, {})
        self.assertEqual(got[0]["status"], "unknown")
        self.assertEqual(got[0]["alive"], 0)
        self.assertEqual(got[0]["dead"], 0)


class Combined(unittest.TestCase):
    def test_assess_community_combines_everything(self):
        crypto = load("trader-scanner", "crypto")
        with open(os.path.join(FIX, "crypto", "coingecko", "contract_coge.json")) as fh:
            cg = crypto.parse_coingecko_community(json.load(fh))
        with open(os.path.join(DIR, "dexscreener-pairs.json")) as fh:
            socials = crypto.parse_dexscreener_socials(json.load(fh))
        got = community.assess_community(socials, cg, load_mentions(), now=NOW)
        self.assertEqual(got["official"]["handle"], "cogecoin")
        self.assertTrue(got["official"]["confirmed_by_both"])
        self.assertEqual(got["presence"]["twitter_followers"], 12345)
        self.assertEqual(got["activity"]["posts"], 6)
        self.assertEqual(got["authenticity"]["verdict"], "solid")
        self.assertEqual([k["handle"] for k in got["kols"]], ["cryptowhale"])
        self.assertIn("X has no free API: activity comes from web-search mentions supplied "
                      "by the caller, not from X itself", got["limits"])
        self.assertIn("KOL clean-record is best-effort: only tokens web search surfaced "
                      "were checked", got["limits"])

    def test_assess_community_with_no_data_is_unknown(self):
        got = community.assess_community({"handle": None, "twitter_url": None},
                                         {"twitter_handle": None, "twitter_followers": None,
                                          "telegram_users": None},
                                         [], now=NOW)
        self.assertFalse(got["official"]["has_x"])
        self.assertEqual(got["presence"]["status"], "unknown")
        self.assertEqual(got["activity"]["status"], "unknown")
        self.assertEqual(got["authenticity"]["verdict"], "unknown")
        self.assertEqual(got["kols"], [])
        self.assertTrue(got["limits"])


class LoadMentions(unittest.TestCase):
    def test_bare_list_and_wrapped_object(self):
        path = os.path.join(DIR, "mentions.json")
        self.assertEqual(len(community.load_mentions(path)), 7)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"author_handle": "a"}], fh)
            bare = fh.name
        try:
            self.assertEqual(community.load_mentions(bare), [{"author_handle": "a"}])
        finally:
            os.unlink(bare)

    def test_missing_or_invalid_file_raises(self):
        with self.assertRaises(community.CommunityError):
            community.load_mentions(os.path.join(DIR, "no-such-file.json"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            bad = fh.name
        try:
            with self.assertRaises(community.CommunityError):
                community.load_mentions(bad)
        finally:
            os.unlink(bad)


class CheckMain(unittest.TestCase):
    def test_check_runs_the_full_check_from_files(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = community.check_main(["--pairs", os.path.join(DIR, "dexscreener-pairs.json"),
                                         "--cg", os.path.join(FIX, "crypto", "coingecko",
                                                             "contract_coge.json"),
                                         "--mentions", os.path.join(DIR, "mentions.json"),
                                         "--now", "2026-11-07T16:00:00Z"])
        self.assertEqual(code, 0)
        got = json.loads(out.getvalue())
        self.assertEqual(got["official"]["handle"], "cogecoin")
        self.assertTrue(got["official"]["confirmed_by_both"])
        self.assertEqual([k["handle"] for k in got["kols"]], ["cryptowhale"])
        self.assertEqual(got["activity"]["posts"], 6)
        self.assertEqual(got["authenticity"]["verdict"], "solid")

    def test_check_without_files_is_unknown_not_a_crash(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = community.check_main([])
        self.assertEqual(code, 0)
        got = json.loads(out.getvalue())
        self.assertFalse(got["official"]["has_x"])
        self.assertEqual(got["presence"]["status"], "unknown")
        self.assertEqual(got["kols"], [])


if __name__ == "__main__":
    unittest.main()
