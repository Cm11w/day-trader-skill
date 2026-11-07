#!/usr/bin/env python3
"""Community check for a crypto token: is its X account real, is anyone
actually talking about it, and have its loudest fans shilled dead coins?

Pure functions, standard library only. Every input is a plain dict (the
shared crypto module in trader-scanner/crypto.py does the fetching and
parsing); every output is JSON-serializable. A number an API leaves out is
None (UNKNOWN), never zero and never invented.

    socials = crypto.parse_dexscreener_socials(dexscreener_doc)
    cg = crypto.parse_coingecko_community(coingecko_doc)
    community = assess_community(socials, cg, load_mentions("mentions.json"))

Honest limits, stated in the output itself:
  - X has no free API, so activity comes from web-search mentions supplied
    by the caller, never from X itself. A coin with no searchable mentions
    looks dormant even if its community lives somewhere search cannot see.
  - Follower counts come from CoinGecko, which updates them on its own
    schedule; they can be stale.
  - A KOL clean record is best-effort: only tokens web search surfaced
    were checked, and a token counts as dead only when DexScreener shows
    zero liquidity and zero 24h volume (or no record at all).
  - Every flag below is a suspect signal, never proof of fakery.
"""

import datetime
import json
import os
import sys

LIMIT_NO_X_API = ("X has no free API: activity comes from web-search mentions "
                  "supplied by the caller, not from X itself")
LIMIT_KOL_BEST_EFFORT = ("KOL clean-record is best-effort: only tokens web search "
                         "surfaced were checked")
DISCLAIMER = ("Every flag is a suspect signal, never proof of fakery: a real "
              "account can trip any of them, and a fake one can pass them all.")

# Authenticity thresholds (see assess_authenticity): a follower count at or
# above LOW_ENGAGEMENT_FOLLOWERS with engagement under LOW_ENGAGEMENT_PCT
# looks bought or inactive; a reply-to-like ratio above REPLY_HEAVY_RATIO
# looks like reply pods or bot replies; zero posts inside the window with
# known followers looks dormant. A follower count at or above BIG_FOLLOWERS
# with unknown account age is a note, never a flag.
LOW_ENGAGEMENT_FOLLOWERS = 10000
LOW_ENGAGEMENT_PCT = 0.05
REPLY_HEAVY_RATIO = 1.0
BIG_FOLLOWERS = 100000


class CommunityError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _median(values):
    """Median of a list of numbers, or None for an empty list."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _parse_time(text):
    """An ISO time string -> aware UTC datetime, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _now_or(now):
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc)


def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- official

def assess_official(socials, cg):
    """Is there an official X account, and do both sources agree on it?

    A handle counts as official when DexScreener socials list it; a
    CoinGecko links.twitter_screen_name confirms it when it matches
    (case-insensitive). DexScreener wins a mismatch: it is the token's own
    listing, CoinGecko may lag.
    """
    socials = socials or {}
    cg = cg or {}
    dex_handle = (socials.get("handle") or None)
    cg_handle = (cg.get("twitter_handle") or None)
    dex_handle = dex_handle.lstrip("@") if dex_handle else None
    cg_handle = cg_handle.lstrip("@") if cg_handle else None
    if dex_handle and cg_handle and dex_handle.lower() == cg_handle.lower():
        return {"has_x": True, "handle": dex_handle, "url": socials.get("twitter_url"),
                "source": "both", "confirmed_by_both": True}
    if dex_handle:
        return {"has_x": True, "handle": dex_handle, "url": socials.get("twitter_url"),
                "source": "dexscreener", "confirmed_by_both": False}
    if cg_handle:
        return {"has_x": True, "handle": cg_handle,
                "url": "https://x.com/" + cg_handle,
                "source": "coingecko", "confirmed_by_both": False}
    return {"has_x": False, "handle": None, "url": None, "source": None,
            "confirmed_by_both": False}


def assess_presence(cg):
    """Follower counts from CoinGecko. Missing stays None (UNKNOWN)."""
    cg = cg or {}
    followers = cg.get("twitter_followers")
    telegram = cg.get("telegram_users")
    known = followers is not None or telegram is not None
    return {"twitter_followers": followers, "telegram_users": telegram,
            "reddit_subscribers": cg.get("reddit_subscribers"),
            "facebook_likes": cg.get("facebook_likes"),
            "status": "known" if known else "unknown"}


# ---------------------------------------------------------------- activity

def assess_activity(mentions, window_days=7, now=None, followers=None):
    """Mentions inside the window: how often the account posts, how much
    each post gets, and the engagement rate against the official follower
    count. `mentions` is a list of {time (ISO), author, author_handle,
    author_followers, likes, reposts, replies, text, url, platform}.
    engagement_rate_pct is 100 * median(likes + reposts per post) /
    official followers when followers are known, else None. A mention list
    with dated posts but nothing in the window is still known activity
    (dormant); a list with no dated post at all is unknown (no data)."""
    now = _now_or(now)
    cutoff = now - datetime.timedelta(days=window_days)
    window = []
    dated = False
    for m in mentions or []:
        t = _parse_time((m or {}).get("time"))
        if t is None:
            continue
        dated = True
        if t < cutoff or t > now:
            continue
        window.append(m)
    likes = [_num(m.get("likes")) for m in window]
    reposts = [_num(m.get("reposts")) for m in window]
    replies = [_num(m.get("replies")) for m in window]
    sums = []
    for m in window:
        like = _num(m.get("likes")) or 0.0
        repost = _num(m.get("reposts")) or 0.0
        sums.append(like + repost)
    med_sum = _median(sums)
    engagement = None
    if med_sum is not None and followers:
        engagement = round(100.0 * med_sum / followers, 2)
    posts = len(window)
    return {"posts": posts,
            "posts_per_day": round(posts / float(window_days), 2) if window_days else None,
            "median_likes": _median(likes),
            "median_reposts": _median(reposts),
            "median_replies": _median(replies),
            "engagement_rate_pct": engagement,
            "window_days": window_days,
            "status": "known" if dated else "unknown"}


# ---------------------------------------------------------------- authenticity

def assess_authenticity(official, presence, activity):
    """Verdict from the explicit thresholds in the module docstring.

    verdict: "solid" (no flags, activity known), "mixed" (one flag),
    "suspect" (two or more flags), "unknown" (no activity data). A follower
    count at or above 100000 with unknown account age is a note, never a
    flag. Every flag is a suspect signal, never proof of fakery.
    """
    official = official or {}
    presence = presence or {}
    activity = activity or {}
    flags = []
    notes = []
    followers = presence.get("twitter_followers")
    engagement = activity.get("engagement_rate_pct")
    if (followers is not None and followers >= LOW_ENGAGEMENT_FOLLOWERS
            and engagement is not None and engagement < LOW_ENGAGEMENT_PCT):
        flags.append("low engagement for follower count "
                     "(possible bought or inactive followers)")
    likes = activity.get("median_likes")
    replies = activity.get("median_replies")
    if likes is not None and likes > 0 and replies is not None and replies / likes > REPLY_HEAVY_RATIO:
        flags.append("reply-heavy engagement (possible reply pods or bot replies)")
    if (activity.get("posts") == 0 and activity.get("status") == "known"
            and followers is not None):
        flags.append("dormant account (no posts in %d days)" % activity.get("window_days", 7))
    if followers is not None and followers >= BIG_FOLLOWERS:
        notes.append("large follower count (%d, above %d) with unknown account age: "
                     "not a flag on its own" % (followers, BIG_FOLLOWERS))
    if len(flags) >= 2:
        verdict = "suspect"
    elif len(flags) == 1:
        verdict = "mixed"
    elif activity.get("status") == "known":
        verdict = "solid"
    else:
        verdict = "unknown"
    return {"verdict": verdict, "flags": flags, "notes": notes,
            "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------- KOLs

def find_kols(mentions, min_followers=10000, exclude=None):
    """Authors with at least min_followers, deduped by handle, most
    followers first: {handle, followers, mentions, sample_url}. `exclude`
    is a handle (or several) to leave out, normally the official account:
    the project talking about itself is not a KOL. Handles are compared
    without a leading @ and case-insensitively, so "@CryptoWhale" and
    "cryptowhale" are one KOL and the official account never slips in
    under a different case or an @."""
    def _norm(handle):
        return str(handle or "").strip().lstrip("@")
    skip = set()
    if exclude:
        if isinstance(exclude, str):
            skip = {_norm(exclude).lower()}
        else:
            skip = {_norm(h).lower() for h in exclude}
    by_handle = {}
    for m in mentions or []:
        handle = _norm((m or {}).get("author_handle"))
        followers = (m or {}).get("author_followers")
        if not handle or handle.lower() in skip or followers is None:
            continue
        try:
            followers = int(followers)
        except (TypeError, ValueError):
            continue
        if followers < min_followers:
            continue
        entry = by_handle.get(handle.lower())
        if entry is None:
            by_handle[handle.lower()] = {"handle": handle, "followers": followers,
                                         "mentions": 1, "sample_url": (m or {}).get("url") or ""}
        else:
            entry["mentions"] += 1
            if followers > entry["followers"]:
                entry["followers"] = followers
    return sorted(by_handle.values(), key=lambda k: (-k["followers"], k["handle"]))


def assess_kol_history(kols, token_status):
    """Per KOL: are the tokens they mentioned still alive?

    `token_status` is a caller-supplied dict {token_address:
    {liquidity_usd, volume24h_usd}} from DexScreener lookups. A token is
    dead when it is missing from the map, or when both its liquidity and
    its 24h volume are zero. status is "checked" when every mentioned
    token was looked up, "partial" when some were missing, "unknown" when
    the KOL mentioned no tokens at all.
    """
    token_status = token_status or {}
    out = []
    for k in kols or []:
        tokens = list((k or {}).get("tokens_mentioned") or [])
        alive = dead = 0
        missing = False
        for addr in tokens:
            rec = token_status.get(addr)
            if rec is None:
                dead += 1
                missing = True
                continue
            liq = _num(rec.get("liquidity_usd")) or 0.0
            vol = _num(rec.get("volume24h_usd")) or 0.0
            if liq == 0.0 and vol == 0.0:
                dead += 1
            else:
                alive += 1
        if not tokens:
            status = "unknown"
        elif missing:
            status = "partial"
        else:
            status = "checked"
        out.append({"handle": k.get("handle"), "followers": k.get("followers"),
                    "tokens_mentioned": tokens, "alive": alive, "dead": dead,
                    "status": status})
    return out


# ---------------------------------------------------------------- combined

def assess_community(socials, cg, mentions, token_status=None, now=None):
    """The full community check: official, presence, activity,
    authenticity, KOLs, and the honest limits of the check."""
    official = assess_official(socials, cg)
    presence = assess_presence(cg)
    activity = assess_activity(mentions, now=now, followers=presence.get("twitter_followers"))
    authenticity = assess_authenticity(official, presence, activity)
    kols = find_kols(mentions, exclude=official.get("handle"))
    if token_status is not None:
        kols = assess_kol_history(kols, token_status)
    return {"official": official, "presence": presence, "activity": activity,
            "authenticity": authenticity, "kols": kols,
            "limits": [LIMIT_NO_X_API, LIMIT_KOL_BEST_EFFORT]}


# ---------------------------------------------------------------- loading

def load_mentions(path):
    """A mentions file -> a list of mention dicts. Accepts {"mentions": [...]}
    or a bare list. Raises CommunityError on a missing or invalid file."""
    if not path or not os.path.isfile(path):
        raise CommunityError("no mentions file at %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CommunityError("%s is not usable mentions JSON: %s" % (path, exc))
    if isinstance(doc, dict):
        items = doc.get("mentions")
    else:
        items = doc
    if not isinstance(items, list):
        raise CommunityError("%s has no mentions list" % path)
    return [m for m in items if isinstance(m, dict)]


def _load_json(path, what):
    """One JSON file, or a CommunityError."""
    if not path or not os.path.isfile(path):
        raise CommunityError("no %s file at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise CommunityError("%s is not usable JSON: %s" % (path, exc))


def check_main(argv):
    """`community.py check`: the full community check from files, for a
    standalone run (news.py does the same thing in its pair and tickers
    modes). Prints the assess_community JSON: official, presence,
    activity, authenticity, KOLs and the honest limits."""
    import argparse
    ap = argparse.ArgumentParser(description="Community check for one crypto token.")
    ap.add_argument("--pairs", help="DexScreener pairs JSON (socials come from the most liquid pair)")
    ap.add_argument("--cg", help="CoinGecko coin-detail JSON")
    ap.add_argument("--mentions", help="mentions JSON ({mentions: [...]} or a bare list)")
    ap.add_argument("--token-status", help="token-status JSON for the KOL history check")
    ap.add_argument("--now", help="ISO time, for reproducible runs (default: now, UTC)")
    a = ap.parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    scanner = os.path.normpath(os.path.join(here, "..", "trader-scanner"))
    if scanner not in sys.path:
        sys.path.insert(0, scanner)
    import crypto
    socials = crypto.parse_dexscreener_socials(_load_json(a.pairs, "pairs") if a.pairs else None)
    cg = crypto.parse_coingecko_community(_load_json(a.cg, "CoinGecko coin") if a.cg else None)
    mentions = load_mentions(a.mentions) if a.mentions else []
    token_status = None
    if a.token_status:
        token_status = _load_json(a.token_status, "token-status")
        if not isinstance(token_status, dict):
            raise CommunityError("token-status.json must be a "
                                 "{token address: {liquidity_usd, volume24h_usd}} object")
    now = _parse_time(a.now) if a.now else None
    print(json.dumps(assess_community(socials, cg, mentions, token_status=token_status,
                                      now=now), indent=2))
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 2 and sys.argv[1] == "check":
            sys.exit(check_main(sys.argv[2:]))
        if len(sys.argv) < 2:
            print("usage: community.py check [--pairs P] [--cg C] [--mentions M] "
                  "[--token-status T] [--now ISO]", file=sys.stderr)
            print("   or: community.py MENTIONS.json  (count the mentions only)", file=sys.stderr)
            sys.exit(2)
        mentions = load_mentions(sys.argv[1])
        print(json.dumps({"mentions": len(mentions)}, indent=2))
    except CommunityError as exc:
        print("community.py: %s" % exc, file=sys.stderr)
        sys.exit(1)
