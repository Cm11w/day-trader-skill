---
name: trader-news
description: >-
  News Monitor. Tracks breaking news on every stock you watch and flags what
  moves it: Yahoo Finance headlines per ticker, each company's 8-K filings on
  SEC EDGAR (earnings, CEO changes, deals, restatements, bankruptcy) and how
  far each stock is from yesterday's close. Shows only what is new since the
  last check, newest first, in New York time, tags each item (earnings,
  analyst, deal, halt, regulatory, macro...) and never repeats one. Use when
  the user says "any news on TSLA", "why is this moving", "what's the news on
  my watchlist", "morning briefing", "pre-market news", "monitor the news",
  "did anything break", "check the 8-Ks", or after /trader-scanner finds setups.
argument-hint: "[tickers]"
---

# trader-news

A chart can look perfect and still be about to gap on an earnings date, a
halt or a lawsuit. Before a setup becomes a trade, this skill reads the two
feeds that carry those first, for every stock on the watchlist, and tells you
only what is new. It also prints how far each stock has moved today, so a
headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers TSLA NVDA --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-sec --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSLA&region=US&lang=en-US` |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag) |
| SEC EDGAR company Atom feed | 8-K by default (`--forms 8-K,10-Q` for more); needs `sec_user_agent` in `trader/config.json` or `SEC_USER_AGENT`, else filings are skipped with a note |
| `trader/state/news.json` | what has been shown (kept 30 days) and each ticker's CIK |

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an 8-K and a headline disagree, the 8-K is
  what the company told the SEC.
- **Say what it touches.** Every item lists the tickers it touches and its
  tags: earnings, analyst, legal, deal, capital, people, risk, halt,
  regulatory, macro. Filings marked `!` carry an important 8-K item: earnings
  results (2.02), an officer change (5.02), an impairment (2.06), a delisting
  notice (3.01), past financials no longer reliable (4.02), bankruptcy (1.03)
  or a cyber incident (1.05).
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged earnings,
   halt, legal, regulatory or risk, and every MOVING ticker first.
3. Write a short brief, per ticker: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "earnings
   are this week: an overnight gap can jump past any stop").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: an earnings date inside the trade, a halt, an offering.
   Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the open.

## Example: the reel

Monday 2026-10-19, TSLA broke its opening range at 10:15 AM ET. The check at
10:15 (Yahoo Finance's TSLA feed, 16 headlines since 8:00) leads with the
morning's real stories: "Meta and Musk earnings and September pending home
sales: What to Watch" (8:00, earnings), "Tesla-SpaceX Merger Mania Strikes
Wall Street Ahead of Earnings" (8:04, earnings, deal), "Tech Stock Rebound
Nudges US Equity Futures Mostly Higher Pre-Bell" (8:02, macro). The brief:
"Tesla reports this week, so the stock can gap overnight; today's trade is
in and out before the close. No new 8-K." At 10:52 the price line reads
`TSLA 366.48 +3.31% vs yesterday's close 354.72 at 10:52 ET  MOVING`. When a
ticker has no news, say so rather than padding the brief.

## Crypto mode

The same check works for crypto, with a different data path: DexScreener
for the price and the project's own links, CoinGecko for the follower
counts and the trending list, and web-search mentions for what people are
actually saying. No keys, no wallet, paper and analysis only.

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py" --asset crypto --chain solana --pair 8GjMy6ipYsXfMWwRPu9C7ATssorjg6AL88d9xtTZsLqe --json
python3 "${CLAUDE_SKILL_DIR}/news.py" --asset crypto --chain solana --pair <pool> --mentions mentions.json --kol-tokens kol-tokens.json
python3 "${CLAUDE_SKILL_DIR}/news.py" --asset crypto --tickers BTC-USD --cg-id bitcoin
```

Pair mode (`--pair`, optionally `--token`) reads the best pool by
liquidity from DexScreener, the project's socials from the most liquid
pair, CoinGecko contract detail when the chain has contract pages
(`trader-scanner/crypto.py`, `CG_PLATFORMS`; robinhood has none), and the
CoinGecko trending list. When the requested `--pair` is not in the
returned pairs the run substitutes the most liquid pair and says so in a
WARNING, never quietly. Trending is matched by CoinGecko coin id, never
by bare symbol (meme symbols collide across clones); a token CoinGecko
does not list is community presence UNKNOWN, not an error. Yahoo
headlines and SEC/8-K are skipped with a note: there is no Yahoo ticker
for the token and filings are stock-only. `--tickers` mode (`BTC-USD` and
friends) keeps the Yahoo RSS and chart moves and adds the community check
only when `--cg-id` names a CoinGecko coin, and it fetches the DexScreener
socials for that coin too, so the official handle does not depend on
CoinGecko alone. Crypto state lives in `trader/state/news-crypto.json`,
never mixed with the stock state. Offline: `--offline DIR` reads
`dexscreener-pairs.json`, `coingecko-contract.json`, `cg-trending.json`,
`mentions.json` and (for KOL history) `token-status.json` from DIR; a
malformed fixture fails with a clear error naming the file, never a
traceback.

The community check (`community.py`, pure functions, standard library
only) answers four questions, in order:

1. **Is the X account official?** A handle counts as official when
   DexScreener socials list it; CoinGecko confirms it when its
   `twitter_screen_name` matches. DexScreener wins a mismatch.
2. **How big is the audience?** Follower counts from CoinGecko. A
   missing number stays UNKNOWN, never zero.
3. **Is anyone actually talking?** Mentions from a `--mentions` file
   (`{"mentions": [...]}` or a bare list: time, author, author_handle,
   author_followers, likes, reposts, replies, text, url, platform) inside
   a 7-day window: posts per day, median likes, reposts and replies, and
   the engagement rate (100 times median likes plus reposts per post,
   divided by official followers).
4. **Are the loudest fans clean?** `find_kols` lists authors with 10,000
   or more followers; handles are compared without a leading @ and
   case-insensitively, so "@CryptoWhale" and "cryptowhale" are one KOL
   and the official account never slips in under a different case or an
   @. `--kol-tokens` (`{"handle": ["token address", ...]}`)
   feeds `assess_kol_history`, which counts each mentioned token alive or
   dead (dead means DexScreener shows no record, or zero liquidity and
   zero 24h volume). The full check also runs standalone:
   `community.py check --pairs P --cg C --mentions M [--token-status T]`
   prints the same community JSON news.py uses.

Authenticity verdicts use explicit thresholds: followers at or above
10,000 with engagement under 0.05% flags low engagement for the follower
count (possible bought or inactive followers); median replies over median
likes above 1.0 flags reply-heavy engagement (possible reply pods or bot
replies); zero posts in the window with known followers flags a dormant
account. Two or more flags is "suspect", one is "mixed", none with known
activity is "solid", no activity data is "unknown". A follower count at or
above 100,000 with unknown account age is a note, never a flag. Every
flag is a suspect signal, never proof of fakery: a real account can trip
any of them and a fake one can pass them all.

Honest limits, stated in every report: X has no free API, so activity
comes from web-search mentions supplied by the caller, never from X
itself, and a coin with no searchable mentions looks dormant even if its
community lives where search cannot see. Follower counts come from
CoinGecko on its own schedule and can be stale. The KOL clean record is
best-effort: only tokens web search surfaced were checked.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per ticker, a pause between
  calls, and SEC's 10 requests a second limit stays far away.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
