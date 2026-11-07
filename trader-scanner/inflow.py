#!/usr/bin/env python3
"""Large fund inflow into a meme pool: whale buys and net buy surges.

    python3 inflow.py --chain robinhood --pool 0xAcad... --token 0x5cbB...
    python3 inflow.py --chain solana --pool 3ZZw... --token FHCn... --json

Free source only, no keys: GeckoTerminal recent trades for the pool
(up to 300, newest first). A trade is a BUY of the meme token when its
kind is "buy" (from_token = quote currency, to_token = the meme token);
a SELL is the reverse. Only trades inside the window count.

Alert when either fires inside the window (default 30 minutes):
  whale   one single buy >= WHALE_USD (default $5,000)
  surge   net inflow (buys - sells) >= NET_USD (default $20,000) AND
          buy volume >= BUY_RATIO x sell volume (default 1.5x)

Either way, the window must contain at least MIN_BUYERS (default 2)
distinct buyer wallets. A single wallet buying and selling to itself
is wash trading, not money flowing in, and never fires an alert.

A whale buy is a single wallet putting real money in; a surge is
sustained accumulation. Both are inflow, not a buy signal on their own:
a whale can also be the first seller. The alert names the size, never
a recommendation.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto  # noqa: E402  (free fetchers + chain maps)

WINDOW_MIN = 30
WHALE_USD = 5_000.0
NET_USD = 20_000.0
BUY_RATIO = 1.5
MIN_BUYERS = 2        # distinct buyer wallets needed for an alert
MAX_TRADES = 300


def _num(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _parse_ts(text):
    """An ISO timestamp -> aware datetime, or None."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_trades(chain, pool, pause=0):
    """GeckoTerminal recent trades for one pool -> the raw doc. The
    network name is the GeckoTerminal one (crypto.DEX_TO_GT); a chain
    GeckoTerminal does not cover raises CryptoError."""
    network = crypto.DEX_TO_GT.get((chain or "").lower())
    if not network:
        raise crypto.CryptoError("GeckoTerminal has no network for chain %s" % chain)
    if pause:
        crypto.SLEEP(pause)
    return crypto.fetch_json(
        "%s/networks/%s/pools/%s/trades" % (crypto.GT_BASE, network, pool))


def check_inflow(chain, pool, token, window_min=WINDOW_MIN,
                 whale_usd=WHALE_USD, net_usd=NET_USD, buy_ratio=BUY_RATIO):
    """One pool -> inflow facts. Never raises: every failure is an
    UNKNOWN result (alert False) with the reason in `error`."""
    out = {"chain": chain, "pool": pool, "token": token,
           "window_min": window_min, "whale_usd": whale_usd,
           "net_usd": net_usd, "largest_buy_usd": 0.0,
           "largest_buy_tx": None, "largest_buy_wallet": None,
           "buy_volume_usd": 0.0,
           "sell_volume_usd": 0.0, "net_inflow_usd": 0.0,
           "buy_count": 0, "sell_count": 0, "trades_seen": 0,
           "distinct_buyers": 0, "distinct_sellers": 0,
           "top_buyers": [],
           "alert": False, "reason": None, "error": None}
    try:
        doc = fetch_trades(chain, pool)
    except crypto.CryptoError as e:
        out["error"] = str(e)
        return out
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    now = datetime.now(timezone.utc)
    token_lower = (token or "").lower()
    buyers = {}
    sellers = set()
    for item in (doc or {}).get("data") or []:
        a = (item or {}).get("attributes") or {}
        ts = _parse_ts(a.get("block_timestamp"))
        if ts is None or (now - ts).total_seconds() > window_min * 60:
            continue
        kind = (a.get("kind") or "").lower()
        vol = _num(a.get("volume_in_usd"))
        if vol <= 0:
            continue
        out["trades_seen"] += 1
        if kind == "buy":
            # A buy of THIS token: to_token is the meme token. (A buy of
            # the quote currency in the same pool would be a sell of the
            # meme token, and GeckoTerminal already labels it "sell".)
            to_token = (a.get("to_token_address") or "").lower()
            if token_lower and to_token and to_token != token_lower:
                continue
            out["buy_count"] += 1
            out["buy_volume_usd"] += vol
            wallet = a.get("tx_from_address") or ""
            if wallet:
                row = buyers.setdefault(wallet, {"buy_volume_usd": 0.0,
                                                 "largest_buy_usd": 0.0,
                                                 "buys": 0})
                row["buy_volume_usd"] += vol
                row["largest_buy_usd"] = max(row["largest_buy_usd"], vol)
                row["buys"] += 1
            if vol > out["largest_buy_usd"]:
                out["largest_buy_usd"] = vol
                out["largest_buy_tx"] = a.get("tx_hash") or None
                out["largest_buy_wallet"] = wallet or None
        elif kind == "sell":
            out["sell_count"] += 1
            out["sell_volume_usd"] += vol
            wallet = a.get("tx_from_address") or ""
            if wallet:
                sellers.add(wallet)
    out["net_inflow_usd"] = out["buy_volume_usd"] - out["sell_volume_usd"]
    out["distinct_buyers"] = len(buyers)
    out["distinct_sellers"] = len(sellers)
    out["top_buyers"] = [
        {"wallet": w, "wallet_short": _short_wallet(w),
         "buy_volume_usd": round(r["buy_volume_usd"], 2),
         "largest_buy_usd": round(r["largest_buy_usd"], 2),
         "buys": r["buys"]}
        for w, r in sorted(buyers.items(),
                           key=lambda kv: -kv[1]["buy_volume_usd"])[:3]]
    # A single wallet buying and selling to itself is wash trading, not
    # money flowing in: the "inflow" is one actor moving funds. An alert
    # needs at least MIN_BUYERS distinct buyers in the window, so a
    # one-wallet pool never fires (TRUMP bsc, 7 Nov 2026: 46 buys + 7
    # sells, all from one wallet 0xa6...5e52).
    if out["distinct_buyers"] < MIN_BUYERS:
        out["alert"] = False
        out["reason"] = ("hanya %d wallet beli (kemungkinan wash trade)"
                         % out["distinct_buyers"])
    elif out["largest_buy_usd"] >= whale_usd:
        out["alert"] = True
        out["reason"] = "whale buy %s" % _fmt_usd(out["largest_buy_usd"])
    elif (out["net_inflow_usd"] >= net_usd
          and out["buy_volume_usd"] >= buy_ratio * max(out["sell_volume_usd"], 1.0)):
        out["alert"] = True
        out["reason"] = "net inflow %s" % _fmt_usd(out["net_inflow_usd"])
    return out


def _short_wallet(addr):
    if not addr:
        return None
    addr = str(addr)
    return addr if len(addr) <= 12 else "%s...%s" % (addr[:4], addr[-4:])


def _fmt_usd(x):
    if x is None:
        return "UNKNOWN"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if x >= 1_000_000:
        return "$%.1fjt" % (x / 1_000_000)
    if x >= 1_000:
        return "$%.1frb" % (x / 1_000)
    return "$%.0f" % x


def _fmt_price(x):
    if x is None:
        return "UNKNOWN"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "$%.6g" % x


def inflow_card(symbol, chain, pool, token, inflow, price_usd=None,
                change_24h=None, volume24h=None, liquidity=None):
    """The inflow alert card (5 lines). pool/token are addresses; inflow
    is a check_inflow result. Stats are the scan's pool numbers, UNKNOWN
    when not given."""
    wallet = inflow.get("largest_buy_wallet") or ""
    wallet_short = _short_wallet(wallet) or "-"
    chg = ("%.1f%%" % change_24h) if change_24h is not None else "UNKNOWN"
    return "\n".join([
        "*INFLOW BESAR: %s (%s)*" % (symbol or "UNKNOWN", chain or "-"),
        "Beli terbesar: %s (wallet %s) | Beli %d mnt: %s (%d wallet) | Jual %d mnt: %s | Bersih masuk: %s"
        % (_fmt_usd(inflow.get("largest_buy_usd")), wallet_short,
           inflow.get("window_min", WINDOW_MIN), _fmt_usd(inflow.get("buy_volume_usd")),
           inflow.get("distinct_buyers", 0),
           inflow.get("window_min", WINDOW_MIN), _fmt_usd(inflow.get("sell_volume_usd")),
           _fmt_usd(inflow.get("net_inflow_usd"))),
        "Harga: %s | 24j %s | Vol %s | Lik %s" % (
            _fmt_price(price_usd), chg, _fmt_usd(volume24h), _fmt_usd(liquidity)),
        "Link: https://dexscreener.com/%s/%s" % (chain or "-", pool or "-"),
        "Pemicu: %s" % (inflow.get("reason") or "tidak ada inflow besar"),
    ])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chain", required=True, help="solana, bsc, base, robinhood, ...")
    ap.add_argument("--pool", required=True, help="pool / pair address")
    ap.add_argument("--token", required=True, help="token mint / contract address")
    ap.add_argument("--window-min", type=int, default=WINDOW_MIN)
    ap.add_argument("--whale-usd", type=float, default=WHALE_USD)
    ap.add_argument("--net-usd", type=float, default=NET_USD)
    ap.add_argument("--json", action="store_true", help="print the facts dict")
    a = ap.parse_args(argv)
    out = check_inflow(a.chain, a.pool, a.token, window_min=a.window_min,
                       whale_usd=a.whale_usd, net_usd=a.net_usd)
    if a.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print("alert: %s (%s)" % (out["alert"], out.get("reason") or out.get("error")
                                    or "tidak ada inflow besar"))
        print("beli terbesar: %s | beli: %s | jual: %s | bersih: %s"
              % (_fmt_usd(out["largest_buy_usd"]), _fmt_usd(out["buy_volume_usd"]),
                 _fmt_usd(out["sell_volume_usd"]), _fmt_usd(out["net_inflow_usd"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
