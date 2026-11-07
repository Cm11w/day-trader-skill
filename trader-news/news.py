#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and SEC filings for the stocks you watch or hold and
prints only what is new since the last check, newest first, with how far each
stock has moved today and a flag on anything that touches your tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers TSLA NVDA AMD
    python3 news.py --tickers TSLA --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-sec                         # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources, one request each per ticker:
  - Yahoo Finance headline RSS:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER>&region=US&lang=en-US
  - SEC EDGAR filings Atom feed for the company (8-K by default: earnings,
    executive changes, deals, restatements):
    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<CIK>&type=8-K&count=10&output=atom
    SEC needs a User-Agent with your name and email (sec_user_agent in
    trader/config.json or SEC_USER_AGENT). Without one the SEC part is skipped
    and the tool says so; headlines still run.

  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<TICKER>?range=1d&interval=5m

Times print in New York time (ET). State: trader/state/news.json remembers
what you have seen (30 days), so an item shows once. Offline: --offline DIR
reads yahoo-<TICKER>.xml, chart-<TICKER>.json, sec-<FORM>-<CIK>.xml and
company_tickers.json from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=%s"
            "&dateb=&owner=include&count=%d&output=atom")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PLACEHOLDER = "you@example.com"
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = {"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"}
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b|\bjudge\b|\bcourt\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\boffering\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)"),
    ("regulatory", r"\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\bFTC\b|\bDOJ\b"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|\binflation\b|\bjobs report\b|\bpayrolls\b|\btariffs?\b|\bfutures\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = _dt.datetime.combine(_nth_sunday(y, 3, 2), _dt.time(7, 0))   # 2:00 EST
    end = _dt.datetime.combine(_nth_sunday(y, 11, 1), _dt.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    """Aware or naive UTC -> naive New York wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + _dt.timedelta(hours=eastern_offset(utc))


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def sec_user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        cfg = load_json(config) or {}
        ua = str(cfg.get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, sec_ua=None, offline=None, pause=0.15):
        self.sec_ua = sec_ua
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, sec=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        if sec and not self.sec_ua:
            raise NewsError("no SEC User-Agent set")
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": self.sec_ua if sec else BROWSER_UA, "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def yahoo_symbol(ticker):
    return ticker.upper().replace(".", "-")


def cik_map(fetcher, state):
    """ticker -> 10-digit CIK, from SEC's company_tickers.json (cached in state)."""
    cached = state.setdefault("ciks", {})
    if cached:
        return cached
    data = json.loads(fetcher.get(TICKERS_URL, "company_tickers.json", sec=True).decode("utf-8"))
    for v in data.values():
        cached.setdefault(v["ticker"].upper().replace("-", "."), str(v["cik_str"]).zfill(10))
    return cached


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_sec_atom(data, ticker):
    """EDGAR company filings Atom -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("SEC feed for %s is not valid XML: %s" % (ticker, exc))
    company = ""
    for el in root.iter():
        if _local(el.tag) == "conformed-name":
            company = (el.text or "").strip()
            break
    out = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        form = _text(entry, "filing-type")
        acc = _text(entry, "accession-number")
        items = re.findall(r"\d+\.\d+", _text(entry, "items-desc"))
        labels = [ITEMS_8K.get(i, "item " + i) for i in items if i != "9.01"]
        link = ""
        for c in entry.iter():
            if _local(c.tag) == "link":
                link = c.get("href", "")
        title = "%s filed a %s" % (company or ticker, form)
        if labels:
            title += ": " + ", ".join(labels)
        out.append({"id": "sec:" + acc, "source": "SEC EDGAR", "kind": "filing", "form": form,
                    "ticker": ticker, "title": title, "summary": _text(entry, "form-name"),
                    "items": items, "important": bool(set(items) & IMPORTANT_ITEMS),
                    "link": link, "time": parse_time(_text(entry, "updated"))})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\$%s\b" % re.escape(t),
                r"\b(?:NYSE|NASDAQ|Nasdaq)\s*:\s*%s\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and "2.02" in it.get("items", []) and "earnings" not in it["tags"]:
            it["tags"].insert(0, "earnings")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, sec=True, forms=("8-K",), count=10, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            items += parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
    if sec:
        try:
            ciks = cik_map(fetcher, state)
        except NewsError as exc:
            errors.append("SEC ticker list: %s" % exc)
            ciks = {}
        for t in sorted(watch):
            cik = ciks.get(t)
            if not cik:
                if ciks:
                    errors.append("no SEC CIK for %s" % t)
                continue
            for form in forms:
                try:
                    data = fetcher.get(SEC_FEED % (cik, form, count),
                                       "sec-%s-%s.xml" % (form, cik), sec=True)
                    items += parse_sec_atom(data, t)
                except NewsError as exc:
                    errors.append("SEC %s for %s: %s" % (form, t, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s ET" % to_eastern(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-6s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_eastern(it["time"]).strftime("%Y-%m-%d %H:%M ET") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_eastern(now).strftime("%Y-%m-%d %H:%M ET"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


# ---------------------------------------------------------------- crypto mode

def default_state(asset):
    """The state file for one asset: crypto never mixes with the stock state."""
    if asset == "crypto":
        return "trader/state/news-crypto.json"
    return "trader/state/news.json"


def _load_crypto_modules():
    """The shared crypto data module (trader-scanner/crypto.py) and the
    community check (community.py, next to this script), loaded lazily so
    the stock commands never touch them."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for folder in (here, os.path.join(root, "trader-scanner")):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    import crypto
    import community
    return crypto, community


def _offline_json(offline, name):
    """One JSON file from an offline folder, or None when it is absent.
    A file that is there but not usable JSON is a NewsError, not a crash."""
    if not offline:
        return None
    path = os.path.join(offline, name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise NewsError("%s is not usable: %s" % (path, exc))


def _mentions_from_offline(args):
    """mentions.json from an offline folder, validated: a list of mention
    dicts, or a dict with a "mentions" list. Anything else is a NewsError
    (a string or a dict of the wrong shape would crash the activity check)."""
    doc = _offline_json(args.offline, "mentions.json")
    if doc is None:
        return []
    if isinstance(doc, dict):
        mentions = doc.get("mentions")
    elif isinstance(doc, list):
        mentions = doc
    else:
        raise NewsError("mentions.json must be a list of mentions or a {\"mentions\": [...]} object")
    if not isinstance(mentions, list) or any(not isinstance(m, dict) for m in mentions):
        raise NewsError("mentions.json must be a list of mentions or a {\"mentions\": [...]} object")
    return mentions


def _read_json_arg(path, what):
    """A JSON file named on the command line, or a NewsError."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise NewsError("%s file %s is not usable: %s" % (what, path, exc))


def _fmt_price(price):
    if price is None:
        return "unknown"
    return "${:,.8g}".format(price)


def _fmt_change(change):
    if change is None:
        return "n/a"
    return "%+.2f%%" % change


def render_community(community):
    """The community section of the text report, or [] when there is none."""
    if not community:
        return []
    official = community.get("official") or {}
    presence = community.get("presence") or {}
    activity = community.get("activity") or {}
    authenticity = community.get("authenticity") or {}
    out = [""]
    if official.get("has_x"):
        confirm = " (confirmed by DexScreener and CoinGecko)" if official.get("confirmed_by_both") else ""
        out.append("Community: @%s%s" % (official.get("handle"), confirm))
    else:
        out.append("Community: no official X account found")
    followers = presence.get("twitter_followers")
    telegram = presence.get("telegram_users")
    out.append("  X followers: %s  Telegram: %s  (%s)" % (
        "{:,}".format(followers) if followers is not None else "unknown",
        "{:,}".format(telegram) if telegram is not None else "unknown",
        presence.get("status", "unknown")))
    if activity.get("status") == "known":
        engagement = activity.get("engagement_rate_pct")
        out.append("  Activity (%d days): %d posts, %s/day, median likes %s, "
                   "median reposts %s, engagement %s" % (
                       activity.get("window_days", 7), activity.get("posts", 0),
                       activity.get("posts_per_day"),
                       activity.get("median_likes"),
                       activity.get("median_reposts"),
                       ("%s%%" % engagement) if engagement is not None else "unknown"))
    else:
        out.append("  Activity: unknown (no dated mentions to measure)")
    out.append("  Verdict: %s" % authenticity.get("verdict", "unknown"))
    for flag in authenticity.get("flags") or []:
        out.append("  Flag: %s" % flag)
    for note in authenticity.get("notes") or []:
        out.append("  Note: %s" % note)
    kols = community.get("kols") or []
    if kols:
        for k in kols:
            extra = ""
            if "alive" in k:
                extra = ", %d alive / %d dead tokens (%s)" % (k.get("alive", 0),
                                                              k.get("dead", 0), k.get("status"))
            out.append("  KOL: @%s (%s followers, %d mentions%s)" % (
                k.get("handle"), "{:,}".format(k.get("followers") or 0),
                k.get("mentions", 0), extra))
    else:
        out.append("  KOLs: none found in the mentions")
    for limit in community.get("limits") or []:
        out.append("  Limit: %s" % limit)
    return out


def _community_for_pair(crypto, community, args, now, socials, cg_doc, notes):
    """CoinGecko detail, trending, mentions and KOL history for one pair."""
    chain = args.chain
    if cg_doc:
        cg = crypto.parse_coingecko_community(cg_doc)
    else:
        cg = {"twitter_handle": None, "twitter_followers": None, "telegram_users": None,
              "reddit_subscribers": None, "facebook_likes": None}
    trending_doc = None
    if args.offline:
        trending_doc = _offline_json(args.offline, "cg-trending.json")
    else:
        try:
            trending_doc = crypto.fetch_coingecko_trending()
        except crypto.CryptoError as exc:
            notes.append("CoinGecko trending unavailable: %s" % exc)
    trending = False
    if trending_doc:
        coins = crypto.parse_coingecko_trending(trending_doc)
        if cg.get("coin_id"):
            trending = any(c.get("id") == cg["coin_id"] for c in coins)
        else:
            # Never match by bare symbol: dozens of PEPE/DOGE clones share
            # one. Without the CoinGecko coin id there is no safe match.
            notes.append("CoinGecko coin id unknown: trending not checked "
                         "(a bare symbol would collide with clones)")
    mentions = []
    if args.mentions:
        try:
            mentions = community.load_mentions(args.mentions)
        except community.CommunityError as exc:
            raise NewsError(str(exc))
    elif args.offline:
        mentions = _mentions_from_offline(args)
    if not mentions and not args.mentions and not args.offline:
        notes.append("no mentions file: activity UNKNOWN")
    result = community.assess_community(socials, cg, mentions, now=now)
    if args.kol_tokens:
        kol_map = _read_json_arg(args.kol_tokens, "kol-tokens")
        if not isinstance(kol_map, dict):
            raise NewsError("kol-tokens file %s is not a {handle: [addresses]} object"
                            % args.kol_tokens)
        kols = result.get("kols") or []
        for k in kols:
            k["tokens_mentioned"] = list(kol_map.get(k.get("handle"), []) or [])
        addresses = [a for k in kols for a in k.get("tokens_mentioned", [])]
        token_status = {}
        if addresses:
            if args.offline:
                token_status = _offline_json(args.offline, "token-status.json") or {}
                if not isinstance(token_status, dict):
                    raise NewsError("token-status.json must be a "
                                    "{token address: {liquidity_usd, volume24h_usd}} object")
            else:
                try:
                    pairs = crypto.parse_dexscreener_pairs(
                        crypto.fetch_dexscreener_pairs([(chain, a) for a in addresses]))
                    for p in pairs:
                        token_status[p.get("token_address")] = {
                            "liquidity_usd": p.get("liquidity_usd", 0.0),
                            "volume24h_usd": p.get("volume24h_usd", 0.0)}
                except crypto.CryptoError as exc:
                    notes.append("KOL token lookup unavailable: %s" % exc)
        result["kols"] = community.assess_kol_history(kols, token_status)
    return result, trending


def crypto_pair_main(args, now):
    """news.py --asset crypto --pair <pool>: price, community, trending."""
    crypto, community = _load_crypto_modules()
    if args.chain not in crypto.DEFAULT_CHAINS:
        print("news: unknown chain %s: %s" % (args.chain, ", ".join(crypto.DEFAULT_CHAINS)),
              file=sys.stderr)
        return 2
    notes = ["no Yahoo ticker for this token", "SEC/8-K skipped: stock-only"]
    try:
        if args.offline:
            doc = _offline_json(args.offline, "dexscreener-pairs.json")
            if doc is None:
                raise NewsError("offline: no file %s"
                                % os.path.join(args.offline, "dexscreener-pairs.json"))
        elif args.token:
            doc = crypto.fetch_dexscreener_token(args.token)
        else:
            doc = crypto.fetch_json("%s/latest/dex/pairs/%s/%s"
                                    % (crypto.DS_BASE, args.chain, args.pair))
    except crypto.CryptoError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 1
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 1
    pools = crypto.parse_dexscreener_pairs(doc)
    found = False
    if args.pair:
        hits = [p for p in pools if p.get("pair_address") == args.pair]
        if not hits and args.pair.startswith("0x"):
            # EVM addresses differ in case only; the pool is the same one
            # (a base58 address is case-sensitive, so this stays hex-only)
            hits = [p for p in pools
                    if (p.get("pair_address") or "").lower() == args.pair.lower()]
        if hits:
            pools = hits
            found = True
    if not pools:
        print("news: no DexScreener pairs found for this token", file=sys.stderr)
        return 1
    best = max(pools, key=lambda p: p.get("liquidity_usd", 0.0))
    pair = best.get("pair_address") or args.pair
    if args.pair and not found:
        notes.append("WARNING: pair %s was not in the returned pairs: using the most liquid pair %s"
                     % (args.pair, pair))
    token = args.token or best.get("token_address")
    socials = crypto.parse_dexscreener_socials(doc)
    cg_doc = None
    platform = crypto.CG_PLATFORMS.get(args.chain)
    if platform is None:
        notes.append("CoinGecko has no contract pages for chain %s" % args.chain)
    elif args.offline:
        cg_doc = _offline_json(args.offline, "coingecko-contract.json")
        if cg_doc is None:
            notes.append("CoinGecko: token not listed (community presence UNKNOWN)")
    else:
        try:
            cg_doc = crypto.fetch_coingecko_contract(platform, token)
        except crypto.CryptoError:
            notes.append("CoinGecko: token not listed (community presence UNKNOWN)")
    try:
        comm, trending = _community_for_pair(crypto, community, args, now, socials,
                                             cg_doc, notes)
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 1
    price = best.get("price_usd")
    change = best.get("price_change_24h_pct")
    moves = [{"symbol": best.get("symbol"), "price_usd": price,
              "price_change_24h_pct": change}]
    if args.json:
        print(json.dumps({"asset": "crypto", "chain": args.chain, "pair": pair,
                          "symbol": best.get("symbol"), "price_usd": price,
                          "price_change_24h_pct": change, "moves": moves, "items": [],
                          "trending": trending, "community": comm, "notes": notes}, indent=2))
    else:
        lines = ["%s %s %s (24h)  %s  pair %s" % (best.get("symbol"), _fmt_price(price),
                                                  _fmt_change(change), args.chain, pair)]
        if trending:
            lines[0] += "  TRENDING on CoinGecko"
        for n in notes:
            if n.startswith("WARNING:"):
                lines.append(n)
        lines += render_community(comm)
        for n in notes:
            if not n.startswith("WARNING:"):
                lines.append("note: %s" % n)
        lines.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
                     "Not financial advice." % to_eastern(now).strftime("%Y-%m-%d %H:%M ET"))
        sys.stdout.write("\n".join(lines) + "\n")
    state_path = args.state or default_state("crypto")
    if not args.dry_run:
        state = load_json(state_path) or {}
        remember([], state, now)
        folder = os.path.dirname(state_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0


def crypto_tickers_main(args, now, watch):
    """news.py --asset crypto --tickers BTC-USD: the Yahoo feeds, no SEC,
    and a community check only when --cg-id names a CoinGecko coin."""
    crypto, community = _load_crypto_modules()
    state_path = args.state or default_state("crypto")
    state = load_json(state_path) or {}
    errors = ["SEC/8-K skipped: stock-only"]
    fetcher = Fetcher(None, offline=args.offline)
    items = collect(fetcher, watch, state, sec=False, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = collect_moves(fetcher, watch, errors)
    comm = None
    trending = False
    if args.cg_id:
        cg_doc = None
        if args.offline:
            cg_doc = _offline_json(args.offline, "coingecko-contract.json")
        else:
            try:
                cg_doc = crypto.fetch_json("%s/coins/%s" % (crypto.CG_BASE, args.cg_id))
            except crypto.CryptoError as exc:
                errors.append("CoinGecko coin %s unavailable: %s" % (args.cg_id, exc))
        if cg_doc is not None:
            cg = crypto.parse_coingecko_community(cg_doc)
            trending_doc = None
            if args.offline:
                trending_doc = _offline_json(args.offline, "cg-trending.json")
            else:
                try:
                    trending_doc = crypto.fetch_coingecko_trending()
                except crypto.CryptoError:
                    trending_doc = None
            if trending_doc:
                trending = any(c.get("id") == cg.get("coin_id")
                               for c in crypto.parse_coingecko_trending(trending_doc))
            mentions = []
            if args.mentions:
                try:
                    mentions = community.load_mentions(args.mentions)
                except community.CommunityError as exc:
                    print("news: %s" % exc, file=sys.stderr)
                    return 1
            elif args.offline:
                mentions = _mentions_from_offline(args)
            socials = {}
            if args.offline:
                pairs_doc = _offline_json(args.offline, "dexscreener-pairs.json")
                if pairs_doc is not None:
                    socials = crypto.parse_dexscreener_socials(pairs_doc)
                else:
                    errors.append("no dexscreener-pairs.json offline: DexScreener socials unavailable")
            else:
                platform = crypto.CG_PLATFORMS.get(args.chain)
                token = ((cg_doc.get("platforms") or {}).get(platform) or "") if platform else ""
                if token:
                    try:
                        socials = crypto.parse_dexscreener_socials(
                            crypto.fetch_dexscreener_token(token))
                    except crypto.CryptoError as exc:
                        errors.append("DexScreener socials unavailable: %s" % exc)
                else:
                    errors.append("no contract address for %s on CoinGecko: "
                                  "DexScreener socials unavailable" % args.chain)
            comm = community.assess_community(socials, cg, mentions, now=now)
    else:
        errors.append("community skipped: pass --cg-id to check a CoinGecko coin")
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        first = moves[0] if moves else None
        print(json.dumps({"asset": "crypto", "chain": args.chain, "pair": None,
                          "symbol": sorted(watch)[0] if watch else None,
                          "price_usd": first["price"] if first else None,
                          "price_change_24h_pct": first["pct"] if first else None,
                          "moves": mv, "items": _json_ready(shown),
                          "trending": trending, "community": comm,
                          "notes": errors}, indent=2))
    else:
        out = render_moves(moves, args.move_pct)
        out += render_community(comm)
        text = render(shown, errors, now, [], args.move_pct)
        if out:
            out.append("")
        sys.stdout.write("\n".join(out) + text)
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(state_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default=None,
                    help="default: trader/state/news.json (stocks) or trader/state/news-crypto.json (crypto)")
    ap.add_argument("--asset", default="stocks", choices=("stocks", "crypto"),
                    help="stocks (default) or crypto")
    ap.add_argument("--chain", default="solana", help="crypto chain id (DexScreener id)")
    ap.add_argument("--pair", help="crypto pool address (pair mode)")
    ap.add_argument("--token", help="crypto token address (pair mode; also picks the token)")
    ap.add_argument("--mentions", metavar="FILE", help="mentions JSON for the community check")
    ap.add_argument("--kol-tokens", metavar="FILE",
                    help='{"handle": ["token address", ...]} for KOL history')
    ap.add_argument("--cg-id", help="CoinGecko coin id (community check in --tickers mode)")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--forms", default="8-K", help="SEC forms to watch, comma separated")
    ap.add_argument("--no-sec", action="store_true", help="headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    if args.asset == "crypto":
        try:
            if args.pair or args.token:
                return crypto_pair_main(args, now)
            if args.tickers:
                watch = {t.upper(): None for t in args.tickers}
                return crypto_tickers_main(args, now, watch)
        except NewsError as exc:
            print("news: %s" % exc, file=sys.stderr)
            return 1
        print("news: crypto mode needs --pair (or --token) or --tickers", file=sys.stderr)
        return 2

    if args.tickers:
        watch = {t.upper(): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state_path = args.state or default_state("stocks")
    state = load_json(state_path) or {}
    errors = []
    ua = sec_user_agent(args.user_agent, args.config)
    sec = not args.no_sec
    if sec and not ua and not args.offline:
        errors.append("SEC filings skipped: set sec_user_agent in trader/config.json or "
                      "SEC_USER_AGENT (\"Your Name you@example.com\") to include them")
        sec = False
    fetcher = Fetcher(ua, offline=args.offline)
    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    items = collect(fetcher, watch, state, sec=sec, forms=forms, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(state_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
