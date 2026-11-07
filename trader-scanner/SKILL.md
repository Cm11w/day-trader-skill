---
name: trader-scanner
description: >-
  Chart Scanner. Scans thousands of charts for setups: opening-range
  breakouts, flags, oversold bounces. Builds the list of US-listed common
  stocks, ordinary shares and ADRs from Nasdaq Trader's symbol directories,
  runs a pre-market scan of daily closes (20-day closing-high breakout, bull
  flag, reclaimed the 20-day average, oversold, oversold bounce) and an
  intraday opening-range breakout scan on 5-minute bars, then writes
  trader/watchlist.json for trader-news and trader-entry. Free Yahoo data,
  bar by bar. Use when the user says "scan the market", "find me setups",
  "what is breaking out", "opening range breakouts", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb | memes] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money;
the scanner does not change that. It only saves you from flipping through
5,000 charts by hand. Crypto mode (`memes`) is separate: it scans meme pools
across chains from free DEX data, and it never touches the stock commands.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols TSLA,NVDA,AMD  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:50 AM ET -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM ET
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
python3 "${CLAUDE_SKILL_DIR}/scan.py" memes                          # crypto meme pools -> trader/watchlist-crypto.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" memes --chains solana,base     # narrow to two chains
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --universe "${CLAUDE_SKILL_DIR}/crypto-universe.txt"  # majors on Yahoo
```

Flags: `--min-price 5` and `--min-volume` (daily 1,000,000, orb 0),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-19 10:15"` (New York time) to pin the clock, `--offline PATH`
to read saved Yahoo files, `--no-write`, `--show N`, `--json`. Exit code 0
when the scan found something, 1 when it found nothing or could not run (the
reason is one plain line). For `memes`, `--now` is UTC (crypto trades 24/7)
and the filters are `--min-liquidity-usd 10000`, `--min-volume24h-usd 5000`,
`--max-age-hours 168`, `--min-marketcap-usd 0`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/watchlist-crypto.json` | written by `memes`: same shape, `mode` is `memes`; a hit is `symbol`, `name`, `chain`, `pair_address`, `token_address`, `dex`, `price_usd`, `market_cap_usd`, `fdv_usd`, `market_cap_known`, `liquidity_usd`, `volume24h_usd`, `price_change_24h_pct`, `pair_age_hours`, `source` |
| `trader/watchlist-crypto-trending.json` | written by `crypto.py watch` (only with `--out`): CoinGecko trending coins, most trending first |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 15:30) |
| `trader-scanner/crypto-universe.txt` | shipped list of crypto majors as Yahoo tickers, for `daily --universe` |
| `trader-scanner/crypto.py` | shared crypto data module: DexScreener, CoinGecko, GeckoTerminal (all free, no key) |

## The crypto meme scan (`memes`)

Crypto trades 24/7, so there is no open, no close and no session: the clock
is UTC and a pool's age is measured from its creation time. `memes` fetches
trending and new pools per chain from GeckoTerminal and boosted tokens from
DexScreener, dedupes by token address (keeping the most liquid pair), then
applies the filters: liquidity at least `--min-liquidity-usd`, 24h volume at
least `--min-volume24h-usd`, market cap at least `--min-marketcap-usd`, age
under `--max-age-hours`. Boosted tokens are fetched in pages of 30 until
the list is exhausted, so boosted token #31 and beyond are not dropped.
Hits are listed most traded first (24h volume). A pool whose market cap is
unpublished keeps `market_cap_usd` 0 with `market_cap_known` false and the
FDV in `fdv_usd`; the FDV never stands in for the market cap, so a
`--min-marketcap-usd` floor cannot be met by FDV alone, and a pool with an
unparseable creation time fails the age filter instead of passing it. A
24h change the APIs left out prints "n/a" in text output (the JSON keeps
null), never "+0.00%".

Chains (DexScreener ids, all 9 by default): solana, ethereum, base, bsc,
arbitrum, polygon, avalanche, optimism, robinhood. GeckoTerminal network ids
differ for three of them (ethereum is `eth`, avalanche is `avax`, polygon is
`polygon_pos`); `crypto.py` maps them. Robinhood is the same id on both APIs.

Data is free and needs no key: GeckoTerminal trending/new pools, DexScreener
pairs and boosts, CoinGecko markets (in `crypto.py`, for monitoring). GMGN
and CoinMarketCap need API keys and are not used. A failed source counts as
failed and the rest still comes back, so a partial scan says so. The scan
never places an order and never touches a wallet: paper and analysis only.
Meme pools are the riskiest corner of the market; a hit only means the pool
passed a liquidity, volume and age filter, nothing more.

For monitoring, `crypto.py watch` lists the CoinGecko trending coins,
most trending first, with `--limit N` (default 10) and `--out
trader/watchlist-crypto-trending.json` to save the list. It reuses the
same trending parse as `cg-trending` and adds no new fetching.

## Real vs clone (`clone_check.py`)

New meme tokens appear every minute and clones copy the symbol and name of
whatever is hot. `clone_check.py SYMBOL` searches DexScreener for every
token with that symbol or name, groups by token address (one address on
several chains is one candidate, not a clone), and scores each candidate:
oldest pair +2, highest liquidity +2, highest 24h volume +1, has
Twitter/website +1, listed on CoinGecko +3. A score gap of 2 or more
between rank 1 and 2 is a REAL verdict (strong); a gap of 0-1 is BELUM
JELAS and the output names what would settle it (official X account, a
CoinGecko listing, the deployer). Two candidates sharing a website or
Twitter handle are flagged as probably one multichain project, not clones
of each other.

DexScreener search returns at most 30 results and can miss big pools, so
`--address ADDR` fetches the token being checked directly and scores it
with the rest; it is never left out. CoinGecko has no robinhood platform,
so a robinhood-only candidate is always UNKNOWN on that signal. The score
is evidence, not certainty: a clone can be older on another chain, and
the original can lose on liquidity for a while. Never treat a REAL verdict
as a buy signal; run entry, exit, Time Machine and risk on the winner
before anything else.

## The trade signal (`signal.py`)

One token -> one signal card, in a FIXED format that never changes: 12
lines, the same labels in the same order, every time. A missing value
prints UNKNOWN; a line is never dropped, moved, or reworded. Lines 1-10
are the original format; line 11 (Bonding) was added 7 Nov 2026 at the
user's request, line 12 (Kreator) the same day. The card runs the whole pipeline and scores the token
0-100:

    python3 trader-scanner/signal.py --token 0x... --symbol CLAUS
    python3 trader-scanner/signal.py --chain robinhood --pair 0x... --symbol CLAUS
    python3 trader-scanner/signal.py --token 0x... --mentions mentions.json --json

  *SINYAL: {SYMBOL} ({chain}) - Skor {N}/100 {BAND}*
  Harga: ${price} | 24j {chg} | Vol {vol} | Lik {liq} | FDV {fdv} | MCap {mcap} | Umur pool {age}
  Keaslian: {VERDICT} - peringkat 1 (token asli, {chain}, umur {age}); pool ini peringkat {rank}
  Entry: {STATUS} - {detail}
  Stop: ${stop} | Target: ${target} (2R)
  Time Machine: target duluan {p}% | rata-rata {avg}R
  Risk: {VERDICT} - {alasan}
  Komunitas: {handle} | followers: {N} | aktivitas: {aktivitas}
  Link: {url}
  Skor: Momentum {m}/30 | Likuiditas-Keamanan {l}/25 | Keaslian {k}/20 | Entry {e}/15 | Time Machine {t}/10 | Kreator {c}
  Bonding: {progres}% (belum lulus, {launchpad}) | LULUS ({dex}) | N/A (bukan bonding launch) | UNKNOWN - tidak ada data bonding
  Kreator: {addr} | token lain: {n} | rug: {r} | {VERDICT} ({modifier}) | UNKNOWN - tidak ada data kreator (0)

Kreator: who deployed the token and whether they rugged before
(`creator.py`, free sources, no keys). Solana: RugCheck full report ->
creator + creatorTokens; each prior token (max 5) is checked for a rug
(RugCheck `rugged`, else DexScreener dead: no pairs or every pair under
$100 liquidity). EVM (incl. robinhood): GoPlus token_security ->
creator_address + honeypot_with_same_creator (1 = this creator already
made a honeypot); GoPlus does not list the creator's other tokens, so
the EVM verdict rests on that flag. Verdicts: RUG -15 (prior rug, or a
GoPlus honeypot from the same creator), CLEAN +5 (prior tokens, none
rugged), FIRST 0 (no prior tokens), UNKNOWN 0 (creator or history not
fetchable). A 0 GoPlus flag is UNKNOWN, not CLEAN: it rules out one rug
shape, not a rug history. The modifier is added to the total and the
total is clamped 0-100. Sybil limit: a rugger launching from a fresh
address each time looks like FIRST; that is not detectable from the
creator's own history alone (RugCheck's insider-network count is
reported in the JSON facts as context, never scored).

Bonding: the launchpad bonding-curve status. A pool on a bonding
launchpad (Pump.fun, BONK.fun, four.meme, Flap, Pons) is still bonding;
progress is market cap / graduation market cap, an estimate (the curve
is convex, so the market-cap ratio runs ahead of the raise ratio, but
it is monotone and hits 100% at graduation). Graduation market caps,
in quote terms so they scale with the quote price: Pump.fun / BONK.fun
410.8 SOL, four.meme 88.2 BNB, Flap 72.1 BNB, Pons 20.6 ETH. A Pump.fun
mint (ends in "pump") on a DEX has graduated (LULUS). Any other token
on a DEX is not a bonding launch (N/A). No market cap or no quote price
-> progress UNKNOWN, never invented. The Pump.fun public API is
Cloudflare-blocked from here; DexScreener reports no liquidity for
bonding pools, so the market-cap ratio is the best free signal.

The score: Momentum 30 (24h price 15 + 24h volume 15); Likuiditas-Keamanan
25 (liquidity 10 + risk check 15: PASS 15, UNKNOWN 7, FAIL 0);
Keaslian 20 (skor milik token yang ditanyakan: REAL + rank 1 20, BELUM
JELAS + rank 1 12, SATU KANDIDAT 10, rank 2 5, rank >2 0, check gagal 10
netral). A rank-2 clone never inherits the REAL verdict's points: a
robinhood Anthropic first scored 20/20 (87 KUAT) before the fix, then 5/20
(72 MENARIK) once the score followed the queried token. The card also
names the queried pool's rank when it is not rank 1. Entry 15 (TRIGGERED
15, WAITING <=5% under the 24h high 12, <=10% 9, <=20% 5, further 2, no
data 0); Time Machine 10 (target-first >=80% 10, >=65% 7, >=50% 5, >=35%
3, below 1, no data 5 neutral). Kreator modifier: prior rug -15, clean
prior tokens +5, first launch / unknown 0; total clamped 0-100. Bands:
>=80 KUAT, 60-79 MENARIK, 40-59 WASPADA, <40 LEMAH. The score is a ranking aid, not a buy signal.
Time Machine 10 (target-first >=80% 10, >=65% 7, >=50% 5, >=35% 3,
below 1, no data 5 neutral). Bands: >=80 KUAT, 60-79 MENARIK, 40-59
WASPADA, <40 LEMAH. The score is a ranking aid, not a buy signal.

The card is assembled from the existing tools, not new logic: clone_check
(keaslian), entry check (crypto-breakout), exit plan (stop/target 2R),
timemachine (10,000 replays), check.py crypto (risk), community.py
(komunitas). A CA alone is enough input: chain and pool are found from
the token address. `--pair` without `--token` resolves the token from the
pair endpoint (the watch's seen.json stores no token address). Without
--mentions, community activity is UNKNOWN, never invented. Time Machine
needs at least 1 full UTC day of history, so pools younger than that show
UNKNOWN there. The momentum-watch cron (every 5 min) sends this card
verbatim for each new pool; it never rewords, summarizes, or reorders it.

## Whale wallets (`whale.py`)

Who buys a meme token big, and what else they hold - free sources only.

- `whale.py --chain <chain> --pool <pool> --token <token>` lists the top
  buyers of one pool by buy volume in the last 30 minutes (GeckoTerminal
  trades, aggregated by `tx_from_address`). The buyer wallet is also in
  every inflow check (`top_buyers`, top 3) and the largest buyer's wallet
  is printed on the INFLOW card's "Beli terbesar" line.
- `whale.py --wallet <solana wallet>` lists the wallet's token holdings
  by USD value (Solana RPC `getTokenAccountsByOwner` + DexScreener
  prices, up to 90 mints). EVM holdings are not covered: knowing which
  tokens to check needs an explorer API key.
- **Registry** (`~/workspace/momentum-watch/whale_wallets.json`): the
  watch adds every buyer of >= $1,000 in a scanned pool to this list on
  every run, so the scanning list grows a little each time. A wallet
  that keeps reappearing (different tokens) is a whale worth following.
  `whale.py --registry` lists it ranked by total buy volume. Recorded
  per wallet: first/last seen, total buy USD, buy count, chains, and per
  token the first buy size and price (the start of a track record).
- **PnL limit:** this shows WHO buys big and WHAT they hold, not a
  PnL-ranked smart-money list. Ranking wallets by realised PnL needs
  their full trade history priced per trade; no free source provides it.
  GMGN's smart-money / wallet-score endpoints do (wallet-analysis,
  wallet-score skills), but they are gated on a GMGN API key, which
  needs a $100 GMGN balance to create.

## Large fund inflow (`inflow.py`, momentum-watch alert)

A second, independent alert: when big money flows into a meme pool, the
watch sends an INFLOW card of its own. Free source, no keys:
GeckoTerminal recent trades for the pool (up to 300, newest first). A
trade is a BUY of the meme token when its kind is "buy" (from_token =
quote currency, to_token = the meme token). Only trades inside the last
30 minutes count. It fires when either holds:

  whale   one single buy >= $5,000
  surge   net inflow (buys - sells) >= $20,000 AND buy volume >= 1.5x
          sell volume

Either way, the window must contain at least 2 distinct buyer wallets.
A single wallet buying and selling to itself is wash trading, not money
flowing in, and never fires an alert (TRUMP bsc, 7 Nov 2026: 46 buys +
7 sells, all from one wallet 0xa6...5e52). The card shows the distinct
buyer count on the "Beli 30 mnt" segment, e.g. "(3 wallet)".

The watch checks the top 10 new pools by volume each run, skips any pool
that already got a signal card that run (one alert per pool per run),
and keeps a 1-hour cooldown per pool (inflow_seen.json) unless a new
whale buy is 2x the last alerted one. The INFLOW card is 5 lines, its
own format, never merged into a signal card:

  *INFLOW BESAR: {SYMBOL} ({chain})*
  Beli terbesar: ${x} (wallet {addr}) | Beli 30 mnt: ${x} ({n} wallet) | Jual 30 mnt: ${x} | Bersih masuk: ${x}
  Harga: ${price} | 24j {chg} | Vol {vol} | Lik {liq}
  Link: {url}
  Pemicu: {whale buy $x | net inflow $x}

An inflow is money moving, not a buy signal: a whale can be the first
seller too. The alert names the size, never a recommendation.

## The universe

`universe` downloads `nasdaqlisted.txt` and `otherlisted.txt` from
nasdaqtrader.com/dynamic/SymDir and keeps a row when, in this order:

1. It is not a test issue, not an ETF, not NextShares.
2. It is a plain ticker: 1 to 5 letters, or 1 to 4 letters plus one class
   letter after a dot (BRK.B). A `$` preferred (ABR$D) is out, and so is a
   `.U`, `.W` or `.R` suffix (units, warrants, rights).
3. Out, whatever else the name says: warrants; SPAC units and rights ("Units,
   each consisting of", "Rights to receive"); notes, ETNs, debentures; a %
   coupon; "Depositary Shares" that are not American or Global (those carry a
   preferred).
4. In: the name calls it common stock, common shares or units, ordinary
   shares, shares of beneficial interest, ADS / ADR / American or Global
   depositary shares, or partnership or trust units.
5. Out: the name still says unit, right, bond, preferred or preference.
6. In: everything else (plain names like "AMETEK, Inc.").

On the Oct 19 2026 files this keeps 5,943. The reel said 5,259: that count
came from an ad hoc filter (names that say common or ordinary, no ADRs). All
5,259 of those are in this list; the 684 extra are mostly ADRs, closed-end
funds and partnership units. The Oct 20 files gave 5,942.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so 5,942
symbols take 298 requests. On Oct 20 2026 that took 145 seconds with the
polite pause (0.3 s). Liquidity comes from the spark meta: last price at
least `--min-price` and last session's volume at least `--min-volume`. Run it
before the open or after the close: during the session the volume is the
day's so far, and today's daily bar is left out until 4:00 PM.

The setups are computed from daily closes only (spark has no highs or lows),
so they are close cousins of the reel's `refs/scan.py`, which used highs and
lows:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one (KO on
Oct 16 broke its 20-day closing high, not its 20-day intraday high). Hits are
listed most traded first (price times volume).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:30,
9:35 and 9:40 five-minute bars. A hit is the first completed 5-minute bar
to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

Timing, live on Tue Oct 20 2026 (a strong morning): 100 liquid names at
10:01 AM took 35 seconds (5 spark requests, 70 v8 charts, 51 of 91 usable
charts broke out). The whole universe at 10:03 AM took about 4 minutes (298
spark requests, then 200 v8 charts): 2,448 usable charts, 1,782 candidates,
the 200 most traded checked, 119 of those broke out. For a fast answer, scan
the daily watchlist or a list of names you trade (`--watchlist`, `--symbols`).

A chart is usable only when every 5-minute bar from 9:30 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:58:07) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:50 AM, on a
weekend or holiday, after the close, or after `no_entries_after` (an expired
setup) it prints one plain line and writes nothing. The 1:00 PM early closes
are in the calendar.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 4,980 of 5,259 charts (279
  failed or had no data)". Never round a partial scan up to "the whole market".
- NYSE holidays and early closes for 2026 and 2027 (nyse.com, read 2026-10-13).

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:50 AM ET (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news TICKER` (what is moving
   it), then `/trader-entry TICKER` (the entry bar by bar). Never pick for them.

## Example: the reel

Monday Oct 19 2026, scan at 10:15 AM over the reel's 234-ticker wall
(`tests/fixtures/trader-scanner/wall5m-2026-10-19.json.gz`, real Yahoo charts):

```
python3 scan.py orb --offline wall5m-2026-10-19.json.gz --now "2026-10-19 10:15" --min-price 0
Fetched 233 of 234 charts (1 failed or had no data); 231 usable; 95 candidates, 95 confirmed with real highs and lows.
42 of 231 charts closed above their opening-range high by 10:15 AM:
   9:50 AM  AMC         $2.97  range 2.90 to 2.96  +0.50% over
  ...
  10:15 AM  TSLA      $364.18  range 354.47 to 362.18  +0.55% over
```

The reel's wall had no price floor (AMC at $2.97 is one of the 42), hence
`--min-price 0`; with the default $5 floor it is 41. TSLA's range is the 9:30
to 9:40 bars, high 362.18, low 354.47; the 10:10 bar closed at 364.18, 0.55 %
over the high, at 10:15 AM. The user picks TSLA and Claude hands it to
`/trader-news TSLA`, then `/trader-entry TSLA`. In the reel the universe was
5,259 US stocks; this tool's rule gives 5,943 on the same files.

The pre-market scan that morning (closes through Fri Oct 16, the reel's daily
charts) tags BLK, CI, XOM and HD a 20-day breakout, CCL a bull flag, F, JPM
and GE a reclaim of the 20-day average, and BAC, MS, RIVN and PLUG oversold,
as the reel did. On closes it also tags KO (breakout), RCL (flag) and PNC (an
oversold bounce).

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
