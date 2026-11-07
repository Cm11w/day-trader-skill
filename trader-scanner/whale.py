#!/usr/bin/env python3
"""Whale wallets: who buys a meme token big, and what else they hold.

    python3 whale.py --chain solana --pool 3ZZw... --token CfL8...
    python3 whale.py --chain robinhood --pool 0xAcad... --token 0x5cbB... --json
    python3 whale.py --wallet 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM

Free sources only, no keys:
  top buyers  GeckoTerminal recent trades for the pool (up to 300,
              newest first): buys aggregated by tx_from_address, ranked
              by total buy volume inside the window. A wallet that buys
              big is a whale for this pool, whatever its history.
  holdings    Solana RPC getTokenAccountsByOwner (the wallet's token
              accounts) + DexScreener prices for the top mints, ranked
              by USD value. Shows what else the whale holds right now.
              EVM holdings are not covered: knowing which tokens to
              check needs an explorer API (BscScan/Etherscan key).

PnL limit: this module shows WHO buys big and WHAT they hold, not a
PnL-ranked "smart money" list. Ranking wallets by realised PnL needs
their full trade history priced at each trade; the free sources above
do not provide it. GMGN's smart-money / wallet-score endpoints do
(wallet-analysis, wallet-score skills), but they are gated on a GMGN
API key, which needs a $100 GMGN balance to create. Until then, treat
a big buyer as a whale to watch, not as a proven winner.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto  # noqa: E402
import inflow  # noqa: E402

SOL_RPC = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOP_BUYERS = 5
HOLDINGS_FETCH = 90   # mints priced per wallet (3 DexScreener batches of 30)
HOLDINGS_SHOW = 10
DEFAULT_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "momentum-watch", "whale_wallets.json")


def _short(addr):
    if not addr:
        return None
    addr = str(addr)
    return addr if len(addr) <= 12 else "%s...%s" % (addr[:4], addr[-4:])


def _num(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def top_buyers(chain, pool, token, window_min=30, limit=TOP_BUYERS):
    """Top buyers of one pool by buy volume inside the window. Returns
    [{wallet, wallet_short, buy_volume_usd, largest_buy_usd, buys}].
    Never raises: a fetch failure returns an empty list + error."""
    try:
        doc = inflow.fetch_trades(chain, pool)
    except Exception as e:
        return [], str(e)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    token_lower = (token or "").lower()
    agg = defaultdict(lambda: {"buy_volume_usd": 0.0, "largest_buy_usd": 0.0,
                               "buys": 0})
    for item in (doc or {}).get("data") or []:
        a = (item or {}).get("attributes") or {}
        ts = inflow._parse_ts(a.get("block_timestamp"))
        if ts is None or (now - ts).total_seconds() > window_min * 60:
            continue
        if (a.get("kind") or "").lower() != "buy":
            continue
        vol = _num(a.get("volume_in_usd"))
        if vol <= 0:
            continue
        to_token = (a.get("to_token_address") or "").lower()
        if token_lower and to_token and to_token != token_lower:
            continue
        wallet = a.get("tx_from_address") or ""
        if not wallet:
            continue
        row = agg[wallet]
        row["buy_volume_usd"] += vol
        row["largest_buy_usd"] = max(row["largest_buy_usd"], vol)
        row["buys"] += 1
    out = []
    for wallet, row in sorted(agg.items(),
                              key=lambda kv: -kv[1]["buy_volume_usd"])[:limit]:
        out.append({"wallet": wallet, "wallet_short": _short(wallet),
                    "buy_volume_usd": round(row["buy_volume_usd"], 2),
                    "largest_buy_usd": round(row["largest_buy_usd"], 2),
                    "buys": row["buys"]})
    return out, None


def _sol_rpc(method, params):
    """One Solana JSON-RPC call -> the result, or raises CryptoError."""
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode("utf-8")
    req = urllib.request.Request(
        SOL_RPC, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        raise crypto.CryptoError("Solana RPC %s: %s" % (method, e))
    if doc.get("error"):
        raise crypto.CryptoError("Solana RPC %s: %s" % (method, doc["error"]))
    return doc.get("result")


def wallet_holdings_solana(wallet, limit=HOLDINGS_SHOW):
    """A Solana wallet's token holdings, ranked by USD value. Returns
    [{mint, mint_short, amount, price_usd, value_usd}]. Prices come from
    DexScreener (batch of up to 30 mints); a mint with no price is kept
    with value None and sorts last. Never raises: a failure returns an
    empty list + error."""
    try:
        res = _sol_rpc("getTokenAccountsByOwner", [
            wallet, {"programId": TOKEN_PROGRAM},
            {"encoding": "jsonParsed"}])
    except crypto.CryptoError as e:
        return [], str(e)
    rows = []
    for acc in (res or {}).get("value") or []:
        info = ((acc.get("account") or {}).get("data") or {}).get("parsed",
                                                                  {}).get("info") or {}
        mint = info.get("mint")
        amount = _num((info.get("tokenAmount") or {}).get("uiAmount"))
        if mint and amount > 0:
            rows.append({"mint": mint, "amount": amount})
    if not rows:
        return [], None
    # Price the mints (fetch_dexscreener_pairs batches 30 a request
    # internally); a wallet with far more holdings than HOLDINGS_FETCH
    # only gets its first ones priced.
    rows = rows[:HOLDINGS_FETCH]
    prices = {}
    try:
        doc = crypto.fetch_dexscreener_pairs(
            [("solana", r["mint"]) for r in rows], pause=0)
        for p in (doc or {}).get("pairs") or []:
            addr = ((p.get("baseToken") or {}).get("address") or "")
            px = _num(p.get("priceUsd"))
            if addr and px:
                prices[addr] = max(prices.get(addr, 0.0), px)
    except Exception:
        pass
    out = []
    for r in rows:
        px = prices.get(r["mint"])
        out.append({"mint": r["mint"], "mint_short": _short(r["mint"]),
                    "amount": r["amount"], "price_usd": px,
                    "value_usd": round(r["amount"] * px, 2) if px else None})
    out.sort(key=lambda r: (r["value_usd"] is not None, r["value_usd"] or 0),
             reverse=True)
    return out[:limit], None


# ---------------------------------------------------------------- registry
# The whale wallet registry: every wallet that ever made a big buy in a
# scanned pool is recorded here, and the list grows a little every run.
# It is the scanning list: a wallet that keeps reappearing (buying
# different tokens) is a whale worth following, and its token history is
# the start of a track record. The file lives next to the watch state so
# the watch can update it on every run.


def load_registry(path=DEFAULT_REGISTRY):
    """Registry dict {wallet: entry}; missing/corrupt file -> {}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_registry(reg, path=DEFAULT_REGISTRY):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2, sort_keys=True)


def record_buy(reg, wallet, chain, pool, token, symbol, buy_usd,
               price_usd=None, now=None):
    """Add one big buy to a wallet's registry entry (creates the wallet
    when new). Returns the entry. Tokens are keyed by chain:token so the
    same token on two chains counts twice."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = reg.setdefault(wallet, {
        "wallet": wallet, "wallet_short": _short(wallet),
        "first_seen": now, "last_seen": now, "total_buy_usd": 0.0,
        "buys": 0, "chains": [], "tokens": {}})
    entry["last_seen"] = now
    entry["total_buy_usd"] = round(entry["total_buy_usd"] + buy_usd, 2)
    entry["buys"] += 1
    if chain and chain not in entry["chains"]:
        entry["chains"].append(chain)
    key = "%s:%s" % (chain or "-", token or pool or "-")
    tok = entry["tokens"].setdefault(key, {
        "symbol": symbol, "chain": chain, "pool": pool, "token": token,
        "first_buy_usd": 0.0, "buys": 0, "first_seen": now,
        "first_price_usd": price_usd})
    tok["first_buy_usd"] = round(tok["first_buy_usd"] + buy_usd, 2)
    tok["buys"] += 1
    if tok.get("first_price_usd") is None and price_usd is not None:
        tok["first_price_usd"] = price_usd
    return entry


def top_wallets(reg, limit=10):
    """Wallets ranked by total buy volume. Each row: wallet, wallet_short,
    total_buy_usd, buys, tokens (distinct tokens bought), chains."""
    out = []
    for w, e in reg.items():
        out.append({"wallet": w, "wallet_short": e.get("wallet_short") or _short(w),
                    "total_buy_usd": round(e.get("total_buy_usd", 0.0), 2),
                    "buys": e.get("buys", 0),
                    "tokens": len(e.get("tokens") or {}),
                    "chains": e.get("chains") or [],
                    "first_seen": e.get("first_seen"),
                    "last_seen": e.get("last_seen")})
    out.sort(key=lambda r: -r["total_buy_usd"])
    return out[:limit]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chain", help="solana, bsc, base, robinhood, ...")
    ap.add_argument("--pool", help="pool / pair address")
    ap.add_argument("--token", help="token mint / contract address")
    ap.add_argument("--wallet", help="wallet address (holdings, Solana only)")
    ap.add_argument("--registry", nargs="?", const=DEFAULT_REGISTRY,
                    help="list the whale wallet registry (grows every run)")
    ap.add_argument("--window-min", type=int, default=30)
    ap.add_argument("--limit", type=int, default=TOP_BUYERS)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.registry is not None:
        reg = load_registry(a.registry)
        wallets = top_wallets(reg, limit=a.limit)
        if a.json:
            print(json.dumps({"registry": a.registry,
                              "wallets": len(reg),
                              "top": wallets}, indent=2, sort_keys=True))
        else:
            print("Registry whale wallet: %d wallet" % len(reg))
            for w in wallets:
                print("  %s - total beli $%.2f (%d beli, %d token, chain: %s)"
                      % (w["wallet_short"], w["total_buy_usd"], w["buys"],
                         w["tokens"], ", ".join(w["chains"])))
        return 0
    if a.wallet:
        holdings, err = wallet_holdings_solana(a.wallet, limit=a.limit)
        out = {"wallet": a.wallet, "wallet_short": _short(a.wallet),
               "holdings": holdings, "error": err}
        if a.json:
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            if err:
                print("holdings gagal: %s" % err)
            elif not holdings:
                print("wallet %s tidak memegang token apa pun" % _short(a.wallet))
            else:
                print("Holdings %s (top %d by USD):" % (_short(a.wallet), len(holdings)))
                for h in holdings:
                    val = ("$%.2f" % h["value_usd"]) if h["value_usd"] is not None else "harga UNKNOWN"
                    print("  %s - %s (%s)" % (h["mint_short"], val, h["amount"]))
        return 0
    if not (a.chain and a.pool and a.token):
        ap.error("--chain/--pool/--token are required without --wallet")
    buyers, err = top_buyers(a.chain, a.pool, a.token,
                             window_min=a.window_min, limit=a.limit)
    out = {"chain": a.chain, "pool": a.pool, "token": a.token,
           "window_min": a.window_min, "top_buyers": buyers, "error": err}
    if a.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        if err:
            print("top buyers gagal: %s" % err)
        elif not buyers:
            print("tidak ada pembelian besar dalam %d menit terakhir" % a.window_min)
        else:
            print("Top buyers %d menit terakhir:" % a.window_min)
            for b in buyers:
                print("  %s - $%.2f (%d beli, terbesar $%.2f)"
                      % (b["wallet_short"], b["buy_volume_usd"], b["buys"],
                         b["largest_buy_usd"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
