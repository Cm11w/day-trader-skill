#!/usr/bin/env python3
"""Shared crypto data for the trader skills: free public APIs, no keys.

    python3 crypto.py markets                          # CoinGecko top coins by market cap
    python3 crypto.py trending --chain solana           # GeckoTerminal trending pools
    python3 crypto.py new --chain solana                # GeckoTerminal new pools
    python3 crypto.py ohlcv --chain solana --pair ADDR  # GeckoTerminal candles for a pool
    python3 crypto.py cg-trending                      # CoinGecko trending coins
    python3 crypto.py watch --limit 10                 # CoinGecko trending coins, most trending first
    python3 crypto.py contract --chain solana --address ADDR  # CoinGecko detail + community
    python3 crypto.py security --chain base --address ADDR    # GoPlus security (EVM only)

Sources (all free, no API key):
  DexScreener   https://api.dexscreener.com - pairs: price, volume, liquidity,
                market cap / FDV, transactions, socials, chainId, dexId.
  CoinGecko     https://api.coingecko.com/api/v3 - markets and trending coins.
  GeckoTerminal https://api.geckoterminal.com/api/v2 - trending and new pools
                per network, plus OHLCV candles per pool (minute / hour / day).
  GoPlus        https://api.gopluslabs.io - token security. The EVM
                endpoint covers the EVM chains (honeypot, buy/sell tax,
                holder concentration, proxy, owner), including robinhood
                (chain id 4663, verified live 7 Nov 2026). Solana has its
                own endpoint (api/v1/solana/token_security, beta): freeze
                and mint authority, transfer fee and transfer hook, holders
                and total supply. Two more free keyless Solana sources back
                it up: RugCheck (api.rugcheck.xyz, risk list and score,
                top holders, locked liquidity) and a Jupiter sell quote
                (lite-api.jup.ag, the direct can-it-be-sold probe). No
                GMGN: it needs an API key behind a $100 balance.
Chains: DexScreener chainIds solana, ethereum, base, bsc, arbitrum, polygon,
avalanche, optimism, robinhood map to GeckoTerminal networks solana, eth,
base, bsc, arbitrum, polygon_pos, avax, optimism, robinhood. The robinhood
network exists on both (verified live 7 Nov 2026); Polygon is `polygon_pos`
on GeckoTerminal (`polygon` 404s). CoinGecko contract pages use their own
platform ids (see CG_PLATFORMS); robinhood has none. GoPlus uses numeric
EVM chain ids (see GOPLUS_CHAINS); Solana uses its own endpoint.

A pool is one normalized dict: symbol, name, chain (a DexScreener id),
pair_address, token_address, dex, price_usd, market_cap_usd, fdv_usd,
market_cap_known, liquidity_usd, volume24h_usd, price_change_24h_pct,
pair_created_at (unix seconds, the raw string when it could not be parsed,
or None), source. A number an API leaves out is 0 (price, market cap,
liquidity, volume) or None (24h change, creation time). A null market cap
is never filled in from the FDV: market_cap_usd stays 0.0 with
market_cap_known False, and the FDV is kept separately as fdv_usd, so no
check can pass a token on its FDV alone. Pair age is computed from
pair_created_at by pool_age_hours; a missing creation time has no age and
passes the age filter, but a present time that could not be parsed fails it.

All three APIs are free and unofficial and can lag or rate-limit. One retry
after a pause on a 429, then the fetch counts as failed and the caller
reports it as partial. Exit code 0 when the command found something, 1 when
it did not or could not run.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_BASE = "https://api.dexscreener.com"
CG_BASE = "https://api.coingecko.com/api/v3"
GOPLUS_BASE = "https://api.gopluslabs.io"
RUGCHECK_BASE = "https://api.rugcheck.xyz"
JUP_BASE = "https://lite-api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_PROBE_AMOUNT = 1000000  # base units of the token: a tiny sell probe
JUP_IMPACT_FAIL_PCT = 50.0  # a bigger impact on a tiny probe means unsellable
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
DS_PAIR_BATCH = 30  # DexScreener takes up to 30 token addresses a request
GT_OHLCV_TIMEFRAMES = ("minute", "hour", "day")
CG_PLATFORMS = {"solana": "solana", "ethereum": "ethereum", "base": "base",
                "bsc": "binance-smart-chain", "arbitrum": "arbitrum-one",
                "polygon": "polygon-pos", "avalanche": "avalanche",
                "optimism": "optimistic-ethereum"}
GOPLUS_CHAINS = {"ethereum": "1", "bsc": "56", "polygon": "137",
                 "arbitrum": "42161", "optimism": "10", "avalanche": "43114",
                 "base": "8453", "robinhood": "4663"}

CHAINS = [
    ("solana", "solana"),
    ("ethereum", "eth"),
    ("base", "base"),
    ("bsc", "bsc"),
    ("arbitrum", "arbitrum"),
    ("polygon", "polygon_pos"),
    ("avalanche", "avax"),
    ("optimism", "optimism"),
    ("robinhood", "robinhood"),
]
DEX_TO_GT = dict(CHAINS)
GT_TO_DEX = {gt: dex for dex, gt in CHAINS}
DEFAULT_CHAINS = [dex for dex, _ in CHAINS]


class CryptoError(Exception):
    pass


# ---------------------------------------------------------------- time

def now_utc():
    """Naive UTC now (crypto trades 24/7, so the clock is UTC, not New York)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def parse_now_utc(text):
    """ISO time -> naive UTC. A time without an offset is taken as UTC."""
    if not text:
        return now_utc()
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise CryptoError('--now wants a time like "2026-11-07 16:00" (UTC) or ...Z')
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_iso_created(text):
    """GeckoTerminal '2026-11-07T14:00:00Z' -> unix seconds, or None.
    Fractional seconds and numeric offsets are accepted too; anything else
    (including a missing value) stays None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.astimezone(datetime.timezone.utc).timestamp())


def pool_age_hours(created, now):
    """Unix seconds (or a millisecond epoch) -> age in hours at `now`, or None."""
    if created is None:
        return None
    try:
        sec = int(created)
    except (TypeError, ValueError):
        return None
    if sec > 4102444800:  # a millisecond epoch: divide down to seconds
        sec //= 1000
    epoch = datetime.datetime(1970, 1, 1)
    return (now - (epoch + datetime.timedelta(seconds=sec))).total_seconds() / 3600.0


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_json(url, timeout=20):
    """GET a URL as JSON. One retry after BACKOFF seconds on a 429; a 4xx other
    than 429 and any other failure raise CryptoError with one plain line."""
    last = "no answer"
    for attempt in (0, 1):
        try:
            return json.loads(http_get(url, timeout=timeout))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            try:
                e.close()
            except Exception:
                pass
            if e.code == 429 and attempt == 0:
                SLEEP(BACKOFF)
                continue
            raise CryptoError("%s answered %s for %s" % (url.split("/")[2], last, url.split("?")[0]))
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
            if attempt == 0:
                continue
            raise CryptoError("could not reach %s: %s" % (url.split("/")[2], last))
        except ValueError as e:
            raise CryptoError("bad JSON from %s: %s" % (url.split("/")[2], e))
    raise CryptoError("%s answered %s" % (url.split("/")[2], last))


def fetch_geckoterminal(network, kind, pause=PAUSE):
    """GeckoTerminal trending_pools or new_pools for one network -> the raw doc."""
    if kind not in ("trending_pools", "new_pools"):
        raise CryptoError("unknown GeckoTerminal list %s" % kind)
    if pause:
        SLEEP(pause)
    return fetch_json("%s/networks/%s/%s" % (GT_BASE, network, kind))


def fetch_dexscreener_boosts(pause=PAUSE):
    """Boosted and profiled tokens (DexScreener) -> [(chain, token_address)]."""
    out, seen = [], set()
    for path in ("/token-boosts/latest/v1", "/token-profiles/latest/v1"):
        if pause:
            SLEEP(pause)
        for chain, addr in parse_dexscreener_tokens(fetch_json(DS_BASE + path)):
            if (chain, addr) not in seen:
                seen.add((chain, addr))
                out.append((chain, addr))
    return out


def fetch_dexscreener_pairs(addresses, pause=PAUSE):
    """Pair data for token addresses (30 a request) -> the merged raw doc."""
    pairs = []
    for i in range(0, len(addresses), DS_PAIR_BATCH):
        if i and pause:
            SLEEP(pause)
        batch = ",".join(a for _, a in addresses[i:i + DS_PAIR_BATCH])
        doc = fetch_json("%s/latest/dex/tokens/%s" % (DS_BASE, batch))
        pairs.extend((doc or {}).get("pairs") or [])
    return {"pairs": pairs}


def fetch_coingecko_markets(vs="usd", per_page=50, page=1, pause=PAUSE):
    """CoinGecko top coins by market cap -> the raw list."""
    if pause:
        SLEEP(pause)
    return fetch_json("%s/coins/markets?vs_currency=%s&order=market_cap_desc"
                      "&per_page=%d&page=%d" % (CG_BASE, vs, per_page, page))


def fetch_ohlcv(network, pool_address, timeframe="minute", aggregate=5, limit=100,
                before=None, pause=PAUSE):
    """GeckoTerminal OHLCV candles for one pool -> the raw doc. The API
    answers newest first; parse_ohlcv sorts ascending."""
    if timeframe not in GT_OHLCV_TIMEFRAMES:
        raise CryptoError("unknown OHLCV timeframe %s: %s"
                          % (timeframe, ", ".join(GT_OHLCV_TIMEFRAMES)))
    if pause:
        SLEEP(pause)
    url = ("%s/networks/%s/pools/%s/ohlcv/%s?aggregate=%d&limit=%d&currency=usd"
           % (GT_BASE, network, pool_address, timeframe, aggregate, limit))
    if before is not None:
        url += "&before_timestamp=%d" % int(before)
    return fetch_json(url)


def fetch_ohlcv_paged(network, pool_address, timeframe="minute", aggregate=5,
                      limit=1000, pause=PAUSE, max_pages=10):
    """OHLCV candles past the API's 1000-bar cap: page backwards with
    before_timestamp until `limit` bars or the pool runs out. Returns the
    merged raw doc in the API's shape (newest first). At most max_pages
    requests, so a runaway page loop cannot hammer a free endpoint."""
    rows, seen, before = [], set(), None
    for _ in range(max_pages):
        doc = fetch_ohlcv(network, pool_address, timeframe, aggregate,
                          min(limit, 1000), before, pause)
        page = ((((doc or {}).get("data") or {}).get("attributes") or {})
                .get("ohlcv_list") or [])
        new = [r for r in page if isinstance(r, (list, tuple)) and r and r[0] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r[0])
        rows.extend(new)
        if len(rows) >= limit or len(page) < min(limit, 1000):
            break
        before = min(r[0] for r in new)
    return {"data": {"attributes": {"ohlcv_list": rows}}}


def fetch_coingecko_contract(platform, address, pause=PAUSE):
    """CoinGecko coin detail by contract address -> the raw doc. A token
    CoinGecko does not list answers 404, which fetch_json turns into a
    CryptoError (the caller reports it as UNKNOWN, not as zero)."""
    if pause:
        SLEEP(pause)
    return fetch_json("%s/coins/%s/contract/%s" % (CG_BASE, platform, address))


def fetch_coingecko_trending(pause=PAUSE):
    """CoinGecko trending search -> the raw doc."""
    if pause:
        SLEEP(pause)
    return fetch_json("%s/search/trending" % CG_BASE)


def fetch_goplus_security(chain, addresses, pause=PAUSE):
    """GoPlus token security for one EVM chain -> the raw doc. Solana uses
    its own endpoint (fetch_goplus_solana); every chain in CHAINS is covered."""
    chain_id = GOPLUS_CHAINS.get(chain)
    if chain_id is None:
        raise CryptoError("GoPlus has no security data for chain %s (EVM chains: %s)"
                          % (chain, ", ".join(sorted(GOPLUS_CHAINS))))
    if pause:
        SLEEP(pause)
    doc = fetch_json("%s/api/v1/token_security/%s?contract_addresses=%s"
                     % (GOPLUS_BASE, chain_id, ",".join(addresses)))
    if (doc or {}).get("code") != 1:
        raise CryptoError("GoPlus answered code %s" % (doc or {}).get("code"))
    return doc


def fetch_goplus_solana(mint, pause=PAUSE):
    """GoPlus Solana token security (beta endpoint, keyless) -> the raw doc.
    The result is keyed by the mint exactly as given (base58, case matters)."""
    if pause:
        SLEEP(pause)
    doc = fetch_json("%s/api/v1/solana/token_security?contract_addresses=%s"
                     % (GOPLUS_BASE, mint))
    if (doc or {}).get("code") != 1:
        raise CryptoError("GoPlus Solana answered code %s" % (doc or {}).get("code"))
    return doc


def fetch_rugcheck(mint, pause=PAUSE):
    """RugCheck full report for one Solana mint (keyless) -> the raw doc.
    The full report (not /report/summary) is used because only it carries
    topHolders; it also has the risk list, the normalised score and the
    locked-liquidity share."""
    if pause:
        SLEEP(pause)
    return fetch_json("%s/v1/tokens/%s/report" % (RUGCHECK_BASE, mint))


def fetch_jupiter_quote(mint, amount=JUP_PROBE_AMOUNT, pause=PAUSE):
    """A Jupiter sell quote (token -> SOL) for a small size -> the raw doc,
    or None when Jupiter has no route (the token cannot be sold right now).
    A 429 or a network failure raises CryptoError: the probe is UNKNOWN,
    never a green light."""
    if pause:
        SLEEP(pause)
    url = ("%s/swap/v1/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=100"
           % (JUP_BASE, mint, SOL_MINT, amount))
    try:
        return fetch_json(url)
    except CryptoError as e:
        if "HTTP 400" in str(e) or "HTTP 404" in str(e):
            return None  # no route: not a network failure, a sellability fact
        raise


def fetch_dexscreener_token(address, pause=PAUSE):
    """DexScreener pairs for one token address -> the raw doc."""
    if pause:
        SLEEP(pause)
    return fetch_json("%s/latest/dex/tokens/%s" % (DS_BASE, address))


# ---------------------------------------------------------------- bonding curves
# Launchpad bonding curves, from the venues' published curve constants
# (sumo.trade curve table, 2026-09-08) and live pool data. A curve
# graduates when cumulative net buys hit the raise target; the market cap
# at graduation is fixed in QUOTE terms, so it scales with the quote
# token's USD price and is NOT a fixed USD number:
#   Pump.fun:  30 virtual SOL / 1.073B tokens, raise 85 SOL
#              -> MC at graduation 410.8 SOL (about $30k at SOL $74)
#   Four.meme: 6.1644 virtual BNB / 1.074B tokens, raise 18 BNB
#              -> MC at graduation 88.2 BNB (about $50k at BNB $571)
#   Pons V2:   1.68 virtual ETH / 1.000B tokens, raise 4.2 ETH
#              -> MC at graduation 20.6 ETH (about $39k at ETH $1,915)
# Progress is marketCap / graduation market cap. It is an estimate: the
# constant-product curve is convex, so a mid-curve token's market-cap
# ratio runs ahead of its raise ratio, but it is monotone and hits 100%
# exactly at graduation. The Pump.fun public API (frontend-api.pump.fun)
# is Cloudflare-blocked from here, and DexScreener reports no liquidity
# for bonding pools, so the market-cap ratio is the best free signal.
# A token is a graduate only when its mint proves the launchpad origin:
# Pump.fun mints end in "pump". Any other token on a DEX is simply not
# a bonding launch, never a claimed graduate.
BONDING_CURVES = {
    # DexScreener dexId -> (launchpad name, CoinGecko quote id,
    #                       graduation market cap in quote units,
    #                       DEX it graduates to)
    "pumpfun": ("Pump.fun", "solana", 410.8, "PumpSwap"),
    "bonkfun": ("BONK.fun", "solana", 410.8, "Raydium"),
    "fourmeme": ("four.meme", "binancecoin", 88.2, "PancakeSwap"),
    "flap": ("Flap", "binancecoin", 72.1, "PancakeSwap"),
    "pons": ("Pons", "ethereum", 20.6, "Pons V4"),
    "pons-v2": ("Pons", "ethereum", 20.6, "Pons V4"),
}
_PUMP_MINT_SUFFIX = "pump"
_GRAD_DEX_DISPLAY = {"pumpswap": "PumpSwap", "raydium": "Raydium",
                     "meteora": "Meteora", "pancakeswap": "PancakeSwap",
                     "uniswap": "Uniswap"}


def fetch_quote_prices(timeout=10):
    """SOL / BNB / ETH in USD from CoinGecko simple/price (no key).
    Returns {coin id: usd}; {} on any failure. Never raises."""
    try:
        doc = fetch_json(
            "%s/simple/price?ids=solana,binancecoin,ethereum&vs_currencies=usd"
            % CG_BASE, timeout=timeout)
    except Exception:
        return {}
    out = {}
    for coin, row in (doc or {}).items():
        try:
            usd = float((row or {}).get("usd"))
        except (TypeError, ValueError):
            continue
        if usd and usd > 0:
            out[coin] = usd
    return out


def bonding_info(chain, dex, token_address, market_cap_usd, quote_prices=None):
    """Bonding-curve status for one pool -> a dict with a ready-made card
    line. A pool on a bonding launchpad is still bonding (progress from
    market cap / graduation market cap). A Pump.fun mint on a DEX has
    graduated. Any other token on a DEX is not a bonding launch. No
    market cap or no quote price -> the status is still shown, but the
    progress is UNKNOWN, never invented."""
    dex = (dex or "").strip().lower()
    curve = BONDING_CURVES.get(dex)
    if curve is not None:
        name, quote_id, grad_cap, _grad_dex = curve
        mcap = _num(market_cap_usd)
        progress = None
        if mcap and mcap > 0:
            prices = quote_prices if isinstance(quote_prices, dict) else {}
            px = prices.get(quote_id)
            if px and px > 0:
                progress = min(100.0, max(0.0, mcap / (grad_cap * px) * 100.0))
        if progress is None:
            line = "Bonding: belum lulus (%s), progres UNKNOWN" % name
        else:
            line = "Bonding: %.0f%% (belum lulus, %s)" % (progress, name)
        return {"status": "bonding", "progress": progress, "launchpad": name,
                "line": line}
    token = (token_address or "")
    if chain == "solana" and token.endswith(_PUMP_MINT_SUFFIX):
        disp = _GRAD_DEX_DISPLAY.get(dex, dex or "DEX")
        return {"status": "graduated", "progress": 100.0,
                "launchpad": "Pump.fun", "line": "Bonding: LULUS (%s)" % disp}
    if dex:
        return {"status": "na", "progress": None, "launchpad": None,
                "line": "Bonding: N/A (bukan bonding launch)"}
    return {"status": "unknown", "progress": None, "launchpad": None,
            "line": "Bonding: UNKNOWN - tidak ada data bonding"}


# ---------------------------------------------------------------- parsing

def _num(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _gt_token_id(rel):
    try:
        return ((rel or {}).get("base_token") or {}).get("data") or {}
    except AttributeError:
        return {}


def parse_geckoterminal_pools(doc, network):
    """A GeckoTerminal trending/new pools doc -> normalized pools. `network` is
    the GeckoTerminal id; the pool's chain is the DexScreener id for it. A null
    market cap stays 0.0 with market_cap_known False; the FDV is kept
    separately as fdv_usd and never stands in for the market cap."""
    chain = GT_TO_DEX.get(network, network)
    out = []
    for item in (doc or {}).get("data") or []:
        a = item.get("attributes") or {}
        rel = item.get("relationships") or {}
        name = (a.get("name") or "").strip()
        symbol = name.split("/")[0].strip() if "/" in name else name
        token_id = (_gt_token_id(rel).get("id") or "")
        token = token_id.split("_", 1)[1] if "_" in token_id else token_id
        dex = (((rel.get("dex") or {}).get("data") or {}).get("id") or "")
        change = (a.get("price_change_percentage") or {}).get("h24")
        vol = (a.get("volume_usd") or {}).get("h24")
        mcap = a.get("market_cap_usd")
        created = parse_iso_created(a.get("pool_created_at"))
        if created is None and a.get("pool_created_at"):
            created = a.get("pool_created_at")  # present but unparseable: fails the age filter
        out.append({"symbol": symbol, "name": symbol, "chain": chain,
                    "pair_address": a.get("address") or "", "token_address": token,
                    "dex": dex, "price_usd": _num(a.get("base_token_price_usd")),
                    "market_cap_usd": _num(mcap),
                    "fdv_usd": _num(a.get("fdv_usd")),
                    "market_cap_known": mcap is not None,
                    "liquidity_usd": _num(a.get("reserve_in_usd")),
                    "volume24h_usd": _num(vol),
                    "price_change_24h_pct": None if change is None else _num(change),
                    "pair_created_at": created,
                    "source": "geckoterminal"})
    return out


def parse_dexscreener_pairs(doc):
    """A DexScreener pairs doc -> normalized pools. A null market cap stays
    0.0 with market_cap_known False; the FDV is kept separately as fdv_usd
    and never stands in for the market cap."""
    out = []
    for p in (doc or {}).get("pairs") or []:
        base = p.get("baseToken") or {}
        mcap = p.get("marketCap")
        change = (p.get("priceChange") or {}).get("h24")
        vol = (p.get("volume") or {}).get("h24")
        created = p.get("pairCreatedAt")
        if created is not None:
            try:
                created = int(created) // 1000
            except (TypeError, ValueError):
                pass  # present but unparseable: keep the raw value, fails the age filter
        out.append({"symbol": (base.get("symbol") or "").upper(),
                    "name": base.get("name") or "", "chain": p.get("chainId") or "",
                    "pair_address": p.get("pairAddress") or "",
                    "token_address": base.get("address") or "",
                    "dex": p.get("dexId") or "", "price_usd": _num(p.get("priceUsd")),
                    "market_cap_usd": _num(mcap),
                    "fdv_usd": _num(p.get("fdv")),
                    "market_cap_known": mcap is not None,
                    "liquidity_usd": _num((p.get("liquidity") or {}).get("usd")),
                    "volume24h_usd": _num(vol),
                    "price_change_24h_pct": None if change is None else _num(change),
                    "pair_created_at": created, "source": "dexscreener"})
    return out


def parse_dexscreener_tokens(doc):
    """A DexScreener boosts or profiles list -> [(chain, token_address)]."""
    out = []
    if isinstance(doc, list):
        for row in doc:
            if not isinstance(row, dict):
                continue
            chain, addr = row.get("chainId") or "", row.get("tokenAddress") or ""
            if chain and addr:
                out.append((chain, addr))
    return out


def parse_coingecko_markets(doc):
    """A CoinGecko markets list -> normalized coins, biggest market cap first."""
    out = []
    for c in doc or []:
        if not isinstance(c, dict):
            continue
        change = c.get("price_change_percentage_24h")
        out.append({"id": c.get("id") or "", "symbol": (c.get("symbol") or "").upper(),
                    "name": c.get("name") or "", "price_usd": _num(c.get("current_price")),
                    "market_cap_usd": _num(c.get("market_cap")),
                    "volume24h_usd": _num(c.get("total_volume")),
                    "price_change_24h_pct": None if change is None else _num(change),
                    "market_cap_rank": c.get("market_cap_rank")})
    return out


def parse_ohlcv(doc):
    """A GeckoTerminal OHLCV doc -> bars [{t, o, h, l, c, v}], oldest first.
    A row with a missing or non-numeric open/high/low/close is dropped."""
    rows = (((doc or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            t = int(row[0])
            o, h, l, c, v = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        except (TypeError, ValueError):
            continue
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
    out.sort(key=lambda b: b["t"])
    return out


def ohlcv_to_series(bars):
    """Bars -> the series dict the scanner and entry tools already use:
    {t, c, o, h, l, v, meta}. An empty bar list gives empty columns."""
    return {"t": [b["t"] for b in bars], "c": [b["c"] for b in bars],
            "o": [b["o"] for b in bars], "h": [b["h"] for b in bars],
            "l": [b["l"] for b in bars], "v": [b["v"] for b in bars], "meta": {}}


def _int_or_none(x):
    if x is None:
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def parse_coingecko_community(doc):
    """A CoinGecko coin-detail doc -> community signals. A missing number
    stays None (UNKNOWN), never zero: zero followers and unknown followers
    are different facts."""
    doc = doc or {}
    links = doc.get("links") or {}
    comm = doc.get("community_data") or {}
    return {"coin_id": doc.get("id") or "", "symbol": (doc.get("symbol") or "").upper(),
            "name": doc.get("name") or "",
            "twitter_handle": links.get("twitter_screen_name") or None,
            "twitter_followers": _int_or_none(comm.get("twitter_followers")),
            "telegram_users": _int_or_none(comm.get("telegram_channel_user_count")),
            "reddit_subscribers": _int_or_none(comm.get("reddit_subscribers")),
            "facebook_likes": _int_or_none(comm.get("facebook_likes"))}


def _money(x):
    """A CoinGecko trending money string like "$16,319,541" -> float, else 0."""
    if isinstance(x, (int, float)):
        return float(x)
    if not isinstance(x, str):
        return 0.0
    try:
        return float(x.replace("$", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def parse_coingecko_trending(doc):
    """A CoinGecko trending doc -> coins, in trending order."""
    out = []
    for row in (doc or {}).get("coins") or []:
        item = (row or {}).get("item") or {}
        data = item.get("data") or {}
        change = (data.get("price_change_percentage_24h") or {}).get("usd")
        out.append({"id": item.get("id") or "", "symbol": (item.get("symbol") or "").upper(),
                    "name": item.get("name") or "",
                    "market_cap_rank": item.get("market_cap_rank"),
                    "price_usd": _num(data.get("price")),
                    "price_change_24h_pct": None if change is None else _num(change),
                    "market_cap_usd": _money(data.get("market_cap")),
                    "volume24h_usd": _money(data.get("total_volume"))})
    return out


def _bool01(x):
    if x is None:
        return None
    return str(x) == "1"


def parse_goplus_security(doc, address):
    """A GoPlus token_security doc -> the security record for one address, or
    None when GoPlus has no record for it. Numbers stay numeric; "1"/"0"
    flags become booleans."""
    result = (doc or {}).get("result") or {}
    rec = result.get((address or "").lower())
    if not isinstance(rec, dict):
        return None
    holders = []
    for h in rec.get("holders") or []:
        if not isinstance(h, dict):
            continue
        holders.append({"address": h.get("address") or "",
                        "percent": _num(h.get("percent")),
                        "balance": _num(h.get("balance")) if h.get("balance") not in (None, "")
                                   else None,
                        "is_locked": _bool01(h.get("is_locked")),
                        "is_contract": _bool01(h.get("is_contract"))})
    top10 = None
    if holders:
        holders.sort(key=lambda h: h["percent"], reverse=True)
        top10 = sum(h["percent"] for h in holders[:10])
    return {"is_honeypot": _bool01(rec.get("is_honeypot")),
            "buy_tax": None if rec.get("buy_tax") in (None, "") else _num(rec.get("buy_tax")),
            "sell_tax": None if rec.get("sell_tax") in (None, "") else _num(rec.get("sell_tax")),
            "cannot_buy": _bool01(rec.get("cannot_buy")),
            "is_proxy": _bool01(rec.get("is_proxy")),
            "is_open_source": _bool01(rec.get("is_open_source")),
            "owner_address": rec.get("owner_address") or None,
            "creator_address": rec.get("creator_address") or None,
            "creator_percent": None if rec.get("creator_percent") in (None, "")
                             else _num(rec.get("creator_percent")),
            "holder_count": _int_or_none(rec.get("holder_count")),
            "top_holders": holders, "top10_percent": top10}


def _sol_status(value):
    """A GoPlus Solana status object {"status": "0"/"1"} -> True/False/None."""
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if status in ("1", 1, True):
        return True
    if status in ("0", 0, False):
        return False
    return None


def _sol_fee_pct(value):
    """A GoPlus Solana transfer_fee value -> a tax percent (0-100), or None.
    An empty dict means no fee is configured (0.0); a number is read as a
    percent, the way GoPlus reports buy/sell tax on EVM."""
    if value is None:
        return 0.0
    if isinstance(value, dict):
        if not value:
            return 0.0
        for key in ("transfer_fee", "fee", "percent", "rate"):
            if key in value:
                try:
                    return float(value[key])
                except (TypeError, ValueError):
                    return None
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_goplus_solana(doc, mint):
    """A GoPlus Solana token_security doc -> the Solana record for one mint,
    or None when GoPlus has no record for it. Holder percents are fractions
    of the total supply (like the EVM record); balances and total_supply are
    in whole tokens."""
    result = (doc or {}).get("result") or {}
    rec = result.get(mint or "")
    if not isinstance(rec, dict):
        return None
    holders = []
    for h in rec.get("holders") or []:
        if not isinstance(h, dict):
            continue
        addr = h.get("account") or h.get("address") or ""
        holders.append({"address": addr, "percent": _num(h.get("percent")),
                        "balance": _num(h.get("balance")) if h.get("balance") not in (None, "")
                                   else None,
                        "is_locked": _bool01(h.get("is_locked")),
                        "is_contract": None})
    top10 = None
    if holders:
        holders.sort(key=lambda h: h["percent"], reverse=True)
        top10 = sum(h["percent"] for h in holders[:10])
    hook = rec.get("transfer_hook")
    return {"freezable": _sol_status(rec.get("freezable")),
            "mintable": _sol_status(rec.get("mintable")),
            "transfer_hook": bool(hook) if hook is not None else None,
            "transfer_fee_pct": _sol_fee_pct(rec.get("transfer_fee")),
            "holder_count": _int_or_none(rec.get("holder_count")),
            "top_holders": holders, "top10_percent": top10,
            "total_supply": _num(rec.get("total_supply")) if rec.get("total_supply") not in (None, "")
                            else None}


def parse_rugcheck(doc):
    """A RugCheck full report -> the RugCheck record, or None when the doc
    is not a report. RugCheck percents are already 0-100 (not fractions);
    they are normalised to fractions here so the merged record matches the
    GoPlus shape. The holder address is the owner's wallet."""
    if not isinstance(doc, dict) or not doc.get("mint"):
        return None
    risks = []
    danger = []
    for r in doc.get("risks") or []:
        if not isinstance(r, dict):
            continue
        name = (r.get("name") or "").strip()
        level = (r.get("level") or "").strip().lower()
        if name:
            risks.append({"name": name, "level": level})
            if level == "danger":
                danger.append(name)
    holders = []
    for h in doc.get("topHolders") or []:
        if not isinstance(h, dict):
            continue
        addr = h.get("owner") or h.get("address") or ""
        pct = h.get("pct")
        try:
            pct = float(pct) / 100.0 if pct is not None else 0.0
        except (TypeError, ValueError):
            pct = 0.0
        holders.append({"address": addr, "percent": pct,
                        "balance": _num(h.get("uiAmount")) if h.get("uiAmount") not in (None, "")
                                   else None,
                        "is_locked": None, "is_contract": None})
    if holders:
        holders.sort(key=lambda h: h["percent"], reverse=True)
    score = doc.get("score_normalised")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {"score_normalised": score, "risks": risks, "danger": danger,
            "lp_locked_pct": _num(doc.get("lpLockedPct")) if doc.get("lpLockedPct") not in (None, "")
                             else None,
            "top_holders": holders}


def parse_jupiter_quote(doc):
    """A Jupiter quote doc -> the sellability probe. None (no route) or a
    doc without a route plan means the token cannot be sold right now."""
    if not isinstance(doc, dict) or doc.get("error"):
        return {"sellable": False, "price_impact_pct": None,
                "out_amount": None, "route_label": None}
    routes = doc.get("routePlan") or []
    if not routes:
        return {"sellable": False, "price_impact_pct": None,
                "out_amount": None, "route_label": None}
    label = ((routes[0] or {}).get("swapInfo") or {}).get("label")
    impact = doc.get("priceImpactPct")
    try:
        impact = float(impact) if impact is not None else None
    except (TypeError, ValueError):
        impact = None
    return {"sellable": True, "price_impact_pct": impact,
            "out_amount": _int_or_none(doc.get("outAmount")),
            "route_label": label or None}


def merge_solana_security(goplus, rugcheck, jupiter):
    """The three parsed Solana sources -> one flat record in the GoPlus
    shape (plus Solana extras), or None when no source has data. Holders
    come from GoPlus first, RugCheck when GoPlus has none. The honeypot
    verdict needs both legs to pass: no transfer hook (GoPlus) AND a real
    sell route (Jupiter). A failed probe is UNKNOWN, never a pass."""
    if goplus is None and rugcheck is None and jupiter is None:
        return None
    holders = []
    if goplus is not None:
        holders = list(goplus.get("top_holders") or [])
    if not holders and rugcheck is not None:
        holders = list(rugcheck.get("top_holders") or [])
    top10 = None
    if holders:
        holders.sort(key=lambda h: h.get("percent") or 0, reverse=True)
        top10 = sum(h.get("percent") or 0 for h in holders[:10])
    hook = (goplus or {}).get("transfer_hook")
    sellable = (jupiter or {}).get("sellable")
    impact = (jupiter or {}).get("price_impact_pct")
    if hook is True or sellable is False or (impact is not None and impact > JUP_IMPACT_FAIL_PCT):
        honeypot = True
    elif hook is False and sellable is True:
        honeypot = False
    else:
        honeypot = None
    fee = (goplus or {}).get("transfer_fee_pct") if goplus is not None else None
    return {"chain": "solana", "is_honeypot": honeypot,
            "buy_tax": fee, "sell_tax": fee,
            "cannot_buy": None, "is_proxy": None,
            "holder_count": (goplus or {}).get("holder_count") if goplus is not None else None,
            "top_holders": holders, "top10_percent": top10,
            "total_supply": (goplus or {}).get("total_supply") if goplus is not None else None,
            "freezable": (goplus or {}).get("freezable") if goplus is not None else None,
            "mintable": (goplus or {}).get("mintable") if goplus is not None else None,
            "transfer_hook": hook,
            "rugcheck_score": (rugcheck or {}).get("score_normalised") if rugcheck is not None else None,
            "rugcheck_danger": list((rugcheck or {}).get("danger") or []) if rugcheck is not None else [],
            "sellable": sellable, "sell_impact_pct": impact}


def fetch_solana_security(mint, pause=PAUSE):
    """The three free Solana security sources -> (merged record, errors).
    A failed source degrades to UNKNOWN for its part, never to a green
    light. The record is None only when no source answers at all."""
    goplus = rugcheck = jupiter = None
    errors = {}
    try:
        goplus = parse_goplus_solana(fetch_goplus_solana(mint, pause), mint)
    except CryptoError as e:
        errors["goplus"] = str(e)
    try:
        rugcheck = parse_rugcheck(fetch_rugcheck(mint, pause))
    except CryptoError as e:
        errors["rugcheck"] = str(e)
    try:
        jupiter = parse_jupiter_quote(fetch_jupiter_quote(mint, pause=pause))
    except CryptoError as e:
        errors["jupiter"] = str(e)
    rec = merge_solana_security(goplus, rugcheck, jupiter)
    if rec is not None:
        rec["errors"] = errors
    return rec, errors


_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_HANDLE_RESERVED = {"home", "explore", "notifications", "messages", "settings",
                    "search", "i", "login", "signup", "compose", "intent", "hashtag",
                    "jobs", "lists", "topics", "verified", "account", "privacy",
                    "tos", "about", "download", "business", "developers", "help"}


def _handle_from_url(url):
    """An x.com / twitter.com profile URL -> the handle, or None. Only the
    first path segment of a profile URL counts: /home, /status/123 and
    bare domains are not handles, and reserved words never are."""
    if not url:
        return None
    clean = url.strip().split("?")[0].split("#")[0].rstrip("/")
    m = re.match(r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/(.+)$", clean, re.IGNORECASE)
    if not m:
        return None
    parts = [p for p in m.group(1).split("/") if p]
    if not parts:
        return None
    handle = parts[0].lstrip("@")
    if not _HANDLE_RE.match(handle) or handle.lower() in _HANDLE_RESERVED:
        return None
    return handle


def parse_dexscreener_socials(doc):
    """A DexScreener pairs doc -> the socials of the most liquid pair:
    {twitter_url, handle, telegram_url, discord_url, website_url}. A missing
    link stays None (UNKNOWN), never an invented handle."""
    pairs = (doc or {}).get("pairs") or []
    best = None
    for p in pairs:
        liq = _num((p.get("liquidity") or {}).get("usd"))
        if best is None or liq > best[0]:
            best = (liq, p)
    out = {"twitter_url": None, "handle": None, "telegram_url": None,
           "discord_url": None, "website_url": None}
    if best is None:
        return out
    info = best[1].get("info") or {}
    for s in info.get("socials") or []:
        kind = (s or {}).get("type") or ""
        url = (s or {}).get("url") or ""
        if kind == "twitter" and url:
            out["twitter_url"] = url
            out["handle"] = _handle_from_url(url)
        elif kind == "telegram" and url:
            out["telegram_url"] = url
        elif kind == "discord" and url:
            out["discord_url"] = url
    sites = info.get("websites") or []
    if sites and isinstance(sites[0], dict) and sites[0].get("url"):
        out["website_url"] = sites[0]["url"]
    return out


# ---------------------------------------------------------------- filters

def apply_filters(pools, min_liquidity_usd=10000.0, min_volume24h_usd=5000.0,
                  max_age_hours=168.0, min_marketcap_usd=0.0, now=None):
    """Drop pools under the liquidity / volume / market cap floors or older
    than the age cap. A missing creation time has no age and passes; a
    present time that could not be parsed fails the age filter. A market
    cap the APIs never reported cannot satisfy a market-cap floor: only a
    known market cap counts (the FDV never stands in for it)."""
    now = now or now_utc()
    out = []
    for p in pools:
        if _num(p.get("liquidity_usd")) < min_liquidity_usd:
            continue
        if _num(p.get("volume24h_usd")) < min_volume24h_usd:
            continue
        if _num(p.get("market_cap_usd")) < min_marketcap_usd:
            continue
        if min_marketcap_usd > 0 and not p.get("market_cap_known", True):
            continue
        created = p.get("pair_created_at")
        age = pool_age_hours(created, now)
        if created is not None and age is None:
            continue
        if age is not None and age > max_age_hours:
            continue
        out.append(p)
    return out


def dedupe_pools(pools):
    """One entry per token (chain + token address, else chain + pair address),
    keeping the most liquid pair. A pool with neither address is kept on its
    own instead of collapsing into one entry."""
    best = {}
    for i, p in enumerate(pools):
        key = (p.get("chain") or "", p.get("token_address") or p.get("pair_address") or "")
        if not key[1]:
            key = (key[0], "", i)
        cur = best.get(key)
        if cur is None or _num(p.get("liquidity_usd")) > _num(cur.get("liquidity_usd")):
            best[key] = p
    return list(best.values())


def sort_pools(pools):
    """Most traded first (24h volume), then by symbol."""
    return sorted(pools, key=lambda p: (-p.get("volume24h_usd", 0.0), p.get("symbol") or ""))


def to_hit(pool, now):
    """A normalized pool -> the watchlist hit dict, with the age filled in."""
    age = pool_age_hours(pool.get("pair_created_at"), now)
    return {"symbol": pool.get("symbol") or "", "name": pool.get("name") or "",
            "chain": pool.get("chain") or "", "pair_address": pool.get("pair_address") or "",
            "token_address": pool.get("token_address") or "", "dex": pool.get("dex") or "",
            "price_usd": pool.get("price_usd", 0.0),
            "market_cap_usd": pool.get("market_cap_usd", 0.0),
            "fdv_usd": pool.get("fdv_usd", 0.0),
            "market_cap_known": pool.get("market_cap_known", True),
            "liquidity_usd": pool.get("liquidity_usd", 0.0),
            "volume24h_usd": pool.get("volume24h_usd", 0.0),
            "price_change_24h_pct": pool.get("price_change_24h_pct"),
            "pair_age_hours": None if age is None else round(age, 1),
            "source": pool.get("source") or ""}


# ---------------------------------------------------------------- fetching a scan

def fetch_memes(chains, pause=PAUSE):
    """Trending + new pools per chain (GeckoTerminal), plus boosted tokens
    (DexScreener) -> (pools, failed). A failed source counts as failed and
    the rest still comes back, so a partial scan says so."""
    pools, failed = [], 0
    for chain in chains:
        network = DEX_TO_GT[chain]
        for kind in ("trending_pools", "new_pools"):
            try:
                pools.extend(parse_geckoterminal_pools(fetch_geckoterminal(network, kind, pause),
                                                       network))
            except CryptoError:
                failed += 1
    try:
        boosted = fetch_dexscreener_boosts(pause)
    except CryptoError:
        boosted, failed = [], failed + 1
    if boosted:
        try:
            pairs = parse_dexscreener_pairs(fetch_dexscreener_pairs(boosted, pause))
            pools.extend(p for p in pairs if p.get("chain") in chains)
        except CryptoError:
            failed += 1
    return pools, failed


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as e:
        raise CryptoError("no file at %s" % path)
    except ValueError as e:
        raise CryptoError("%s is not valid JSON: %s" % (path, e))


def load_offline_memes(directory, chains):
    """An offline fixture folder -> (pools, failed). Per chain it reads
    geckoterminal/<network>_trending.json and <network>_new.json; a missing
    file counts as failed. dexscreener/pairs.json is the second source when
    present (the live boosts step is skipped offline)."""
    pools, failed = [], 0
    for chain in chains:
        network = DEX_TO_GT[chain]
        for kind in ("trending", "new"):
            path = os.path.join(directory, "geckoterminal", "%s_%s.json" % (network, kind))
            if not os.path.exists(path):
                failed += 1
                continue
            pools.extend(parse_geckoterminal_pools(_read_json_file(path), network))
    pairs_path = os.path.join(directory, "dexscreener", "pairs.json")
    if os.path.exists(pairs_path):
        pairs = parse_dexscreener_pairs(_read_json_file(pairs_path))
        pools.extend(p for p in pairs if p.get("chain") in chains)
    return pools, failed


# ---------------------------------------------------------------- command line

def _pct_text(change):
    """A 24h change for text output: an unknown change prints as unknown,
    never as +0.00% (JSON keeps the None)."""
    return ("%+.2f%%" % change) if change is not None else "n/a"


def _positive_int(value, name):
    """A CLI count that must be a whole number above 0, or a CryptoError."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CryptoError("--%s must be a whole number above 0 (got %r)" % (name, value))
    return value


def _non_negative_float(value, name):
    """A CLI float that must be finite and at least 0, or a CryptoError."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise CryptoError("--%s must be a number (got %r)" % (name, value))
    if math.isnan(x) or math.isinf(x) or x < 0:
        raise CryptoError("--%s must be a finite number at or above 0 (got %r)" % (name, value))
    return x


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crypto data (shared by the trader skills).")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("markets", help="CoinGecko top coins by market cap")
    p.add_argument("--per-page", type=int, default=10)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--offline", metavar="FILE", help="a markets JSON file instead of the network")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    for name, help_text in (("trending", "GeckoTerminal trending pools for one chain"),
                            ("new", "GeckoTerminal new pools for one chain")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--chain", default="solana")
        p.add_argument("--offline", metavar="FILE", help="a pools JSON file instead of the network")
        p.add_argument("--pause", type=float, default=PAUSE)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("ohlcv", help="GeckoTerminal OHLCV candles for one pool")
    p.add_argument("--chain", default="solana")
    p.add_argument("--pair", required=True, help="pool address")
    p.add_argument("--timeframe", default="minute", help="minute, hour or day")
    p.add_argument("--aggregate", type=int, default=5)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--before", type=int, help="unix seconds: candles before this time")
    p.add_argument("--offline", metavar="FILE", help="an OHLCV JSON file instead of the network")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("cg-trending", help="CoinGecko trending coins")
    p.add_argument("--offline", metavar="FILE", help="a trending JSON file instead of the network")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("watch", help="CoinGecko trending coins, most trending first")
    p.add_argument("--limit", type=int, default=10, help="keep only the first N coins (%d)" % 10)
    p.add_argument("--out", metavar="FILE",
                   help="write the trending list to this file (default: no file is written)")
    p.add_argument("--offline", metavar="FILE", help="a trending JSON file instead of the network")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("contract", help="CoinGecko coin detail by contract address")
    p.add_argument("--chain", default="solana")
    p.add_argument("--address", required=True, help="token contract address")
    p.add_argument("--offline", metavar="FILE", help="a coin-detail JSON file instead of the network")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("security", help="token security: GoPlus (EVM chains incl. robinhood), "
                                         "Solana (GoPlus + RugCheck + Jupiter)")
    p.add_argument("--chain", default="base")
    p.add_argument("--address", required=True, help="token contract address")
    p.add_argument("--offline", metavar="FILE", help="a GoPlus JSON file instead of the network")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        # A NaN or negative count silently disables a filter or a fetch, so
        # the numeric args are checked before anything runs.
        if a.cmd == "markets":
            _positive_int(a.per_page, "per-page")
            _positive_int(a.page, "page")
        elif a.cmd == "ohlcv":
            _positive_int(a.limit, "limit")
            _positive_int(a.aggregate, "aggregate")
        elif a.cmd == "watch":
            _positive_int(a.limit, "limit")
        _non_negative_float(a.pause, "pause")
        if a.cmd == "markets":
            if a.offline:
                coins = parse_coingecko_markets(_read_json_file(a.offline))
            else:
                coins = parse_coingecko_markets(fetch_coingecko_markets(per_page=a.per_page,
                                                                        page=a.page,
                                                                        pause=a.pause))
            if a.json:
                print(json.dumps({"coins": coins}, indent=2))
            else:
                for c in coins:
                    print("  %-6s %12s  mcap $%s  %s" % (
                        c["symbol"], "${:,.8g}".format(c["price_usd"]),
                        "{:,.0f}".format(c["market_cap_usd"]),
                        _pct_text(c["price_change_24h_pct"])))
            return 0 if coins else 1
        if a.cmd == "ohlcv":
            if a.chain not in DEX_TO_GT:
                raise CryptoError("unknown chain %s: %s" % (a.chain, ", ".join(DEFAULT_CHAINS)))
            if a.offline:
                bars = parse_ohlcv(_read_json_file(a.offline))
            else:
                bars = parse_ohlcv(fetch_ohlcv(DEX_TO_GT[a.chain], a.pair, a.timeframe,
                                               a.aggregate, a.limit, a.before, a.pause))
            if a.json:
                print(json.dumps({"chain": a.chain, "pair": a.pair, "timeframe": a.timeframe,
                                  "aggregate": a.aggregate, "bars": bars}, indent=2))
            else:
                for b in bars:
                    print("  %s  o %s  h %s  l %s  c %s  v %s" % (
                        datetime.datetime.fromtimestamp(b["t"], datetime.timezone.utc
                                                        ).strftime("%Y-%m-%d %H:%M"),
                        "{:,.8g}".format(b["o"]), "{:,.8g}".format(b["h"]),
                        "{:,.8g}".format(b["l"]), "{:,.8g}".format(b["c"]),
                        "{:,.2f}".format(b["v"])))
            return 0 if bars else 1
        if a.cmd == "cg-trending":
            if a.offline:
                coins = parse_coingecko_trending(_read_json_file(a.offline))
            else:
                coins = parse_coingecko_trending(fetch_coingecko_trending(a.pause))
            if a.json:
                print(json.dumps({"coins": coins}, indent=2))
            else:
                for c in coins:
                    print("  %-8s %12s  %s  mcap $%s" % (
                        c["symbol"], "${:,.8g}".format(c["price_usd"]),
                        _pct_text(c["price_change_24h_pct"]),
                        "{:,.0f}".format(c["market_cap_usd"])))
            return 0 if coins else 1
        if a.cmd == "contract":
            platform = CG_PLATFORMS.get(a.chain)
            if platform is None:
                raise CryptoError("CoinGecko has no contract pages for chain %s" % a.chain)
            if a.offline:
                got = parse_coingecko_community(_read_json_file(a.offline))
            else:
                got = parse_coingecko_community(fetch_coingecko_contract(platform, a.address,
                                                                         a.pause))
            if a.json:
                print(json.dumps(got, indent=2))
            else:
                print("  %s (%s)  X: %s  followers: %s  telegram: %s" % (
                    got["name"], got["symbol"], got["twitter_handle"] or "unknown",
                    got["twitter_followers"] if got["twitter_followers"] is not None else "unknown",
                    got["telegram_users"] if got["telegram_users"] is not None else "unknown"))
            return 0 if got["coin_id"] else 1
        if a.cmd == "security":
            if a.chain == "solana":
                if a.offline:
                    got = parse_goplus_solana(_read_json_file(a.offline), a.address)
                    if got is None:
                        raise CryptoError("GoPlus Solana has no record for %s" % a.address)
                    got = merge_solana_security(got, None, None)
                else:
                    got, _errs = fetch_solana_security(a.address, a.pause)
                    if got is None:
                        raise CryptoError("no Solana security source answered for %s" % a.address)
                if a.json:
                    print(json.dumps(got, indent=2))
                else:
                    print("  honeypot: %s  buy tax: %s%%  sell tax: %s%%  holders: %s  top10: %s%%" % (
                        got["is_honeypot"], got["buy_tax"], got["sell_tax"], got["holder_count"],
                        round(got["top10_percent"] * 100, 2) if got["top10_percent"] is not None
                        else "unknown"))
                    print("  freezable: %s  mintable: %s  transfer hook: %s  sellable: %s  "
                          "rugcheck score: %s" % (
                              got["freezable"], got["mintable"], got["transfer_hook"],
                              got["sellable"], got["rugcheck_score"]))
                return 0
            if a.chain not in GOPLUS_CHAINS:
                raise CryptoError("GoPlus has no security data for chain %s (EVM chains: %s)"
                                  % (a.chain, ", ".join(sorted(GOPLUS_CHAINS))))
            if a.offline:
                got = parse_goplus_security(_read_json_file(a.offline), a.address)
            else:
                got = parse_goplus_security(fetch_goplus_security(a.chain, [a.address], a.pause),
                                            a.address)
            if got is None:
                raise CryptoError("GoPlus has no record for %s" % a.address)
            if a.json:
                print(json.dumps(got, indent=2))
            else:
                print("  honeypot: %s  buy tax: %s%%  sell tax: %s%%  holders: %s  top10: %s%%" % (
                    got["is_honeypot"], got["buy_tax"], got["sell_tax"], got["holder_count"],
                    round(got["top10_percent"] * 100, 2) if got["top10_percent"] is not None
                    else "unknown"))
            return 0
        if a.cmd == "watch":
            if a.offline:
                coins = parse_coingecko_trending(_read_json_file(a.offline))
            else:
                coins = parse_coingecko_trending(fetch_coingecko_trending(a.pause))
            coins = coins[:a.limit]
            doc = {"coins": coins, "source": "coingecko", "count": len(coins)}
            if a.out:
                d = os.path.dirname(a.out)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(a.out, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=2)
                    fh.write("\n")
            if a.json:
                print(json.dumps(doc, indent=2))
            else:
                for c in coins:
                    print("  %-8s %12s  %s  mcap $%s" % (
                        c["symbol"], "${:,.8g}".format(c["price_usd"]),
                        _pct_text(c["price_change_24h_pct"]),
                        "{:,.0f}".format(c["market_cap_usd"])))
                if a.out:
                    print("Wrote %s (%d coin%s)." % (a.out, len(coins), "" if len(coins) == 1 else "s"))
            return 0 if coins else 1
        chain = a.chain
        if chain not in DEX_TO_GT:
            raise CryptoError("unknown chain %s: %s" % (chain, ", ".join(DEFAULT_CHAINS)))
        kind = "trending_pools" if a.cmd == "trending" else "new_pools"
        if a.offline:
            pools = parse_geckoterminal_pools(_read_json_file(a.offline), DEX_TO_GT[chain])
        else:
            pools = parse_geckoterminal_pools(fetch_geckoterminal(DEX_TO_GT[chain], kind, a.pause),
                                              DEX_TO_GT[chain])
        pools = sort_pools(pools)
        if a.json:
            print(json.dumps({"chain": chain, "pools": [to_hit(p, now_utc()) for p in pools]},
                             indent=2))
        else:
            for p in pools:
                print("  %-8s %12s  mcap $%s  vol24h $%s" % (
                    p["symbol"], "${:,.8g}".format(p["price_usd"]),
                    "{:,.0f}".format(p["market_cap_usd"]), "{:,.0f}".format(p["volume24h_usd"])))
        return 0 if pools else 1
    except CryptoError as e:
        print("crypto.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
