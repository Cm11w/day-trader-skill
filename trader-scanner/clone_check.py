#!/usr/bin/env python3
"""Real vs clone: token mana dengan simbol ini yang aslinya?

    python3 clone_check.py LETRO
    python3 clone_check.py Anthropic --json
    python3 clone_check.py PEPE --chain solana
    python3 clone_check.py Anthropic --address 0xf6d41cbb39a6540a93e0d29a50b4eb720444e754
    python3 clone_check.py LETRO --offline tests/fixtures/crypto/dexscreener/search-letro.json

Meme coin baru muncul tiap menit dan clone meniru simbol/nama coin yang
sedang panas. Tool ini mencari semua token dengan simbol/nama yang sama
lewat DexScreener search, lalu menilai tiap kandidat dengan sinyal yang
bisa dicek dari sumber gratis (tanpa API key):

  paling tua        pair paling awal dibuat biasanya yang asli (+2)
  likuiditas        kandidat paling likuid biasanya yang asli (+2)
  volume 24j        volume tertinggi biasanya yang asli (+1)
  socials           Twitter/website resmi di DexScreener (+1)
  CoinGecko         terdaftar di CoinGecko = sudah melewati kurasi (+3)

Kandidat dikelompokkan per alamat token: alamat yang sama di beberapa
chain EVM (atau satu proyek multichain) dihitung SATU kandidat, bukan
clone. Dua kandidat yang memakai website/Twitter yang sama ditandai
"kemungkinan proyek sama (multichain)".

Verdict: selisih skor >= 2 antara peringkat 1 dan 2 -> REAL (kuat);
selisih 0-1 -> BELUM JELAS, dan output menyebut apa yang bisa memastikan
(akun X resmi, kontrak di CoinGecko, deployer). Satu kandidat saja ->
tidak ada pembanding, jangan langsung percaya.

Pencarian DexScreener mengembalikan max 30 hasil dan bisa melewatkan
pool besar (terbukti live 7 Nov 2026: dua pool Anthropic $10jt tidak
masuk hasil search). Karena itu ada --address: token yang ditanyakan
diambil langsung lewat endpoint token DexScreener dan dinilai bersama
para kandidat, jadi coin yang sedang dicek tidak pernah terlewat.

Batasan: skor adalah bukti, bukan kepastian. Clone bisa lebih tua di
chain lain, dan coin asli bisa kalah likuid sementara. CoinGecko tidak
punya platform robinhood, jadi kandidat robinhood-only selalu UNKNOWN
di sinyal itu; GoPlus tidak dipakai di sini (cek honeypot ada di
check.py). Exit code 0 kalau verdict tercapai, 1 kalau tidak ada
kandidat atau tidak bisa jalan.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto  # noqa: E402  (shared free-API helpers: fetch, parse, chains)

MAX_CANDIDATES = 8
MAX_CG_CHECKS = 5
SCORE_OLDEST = 2
SCORE_LIQUIDITY = 2
SCORE_VOLUME = 1
SCORE_SOCIALS = 1
SCORE_COINGECKO = 3


# ---------------------------------------------------------------- fetch

def fetch_dexscreener_search(query, pause=crypto.PAUSE):
    """DexScreener search by symbol/name -> the raw doc ({"pairs": [...]})."""
    if pause:
        crypto.SLEEP(pause)
    url = "%s/latest/dex/search?q=%s" % (crypto.DS_BASE, urllib.parse.quote(query))
    return crypto.fetch_json(url)


# ---------------------------------------------------------------- grouping

def group_candidates(pools):
    """Normalized pools -> one candidate per token address. A token on
    several chains (same address) is one candidate, not a clone."""
    by_addr = {}
    for p in pools:
        addr = (p.get("token_address") or "").lower()
        if not addr:
            continue
        c = by_addr.get(addr)
        if c is None:
            c = {"token_address": addr, "symbol": p.get("symbol") or "",
                 "name": p.get("name") or "", "chains": set(),
                 "pairs": [], "best": None, "oldest_created": None}
            by_addr[addr] = c
        c["chains"].add(p.get("chain") or "")
        c["pairs"].append(p)
        if c["best"] is None or p.get("liquidity_usd", 0) > c["best"].get("liquidity_usd", 0):
            c["best"] = p
        created = p.get("pair_created_at")
        if isinstance(created, int):
            if c["oldest_created"] is None or created < c["oldest_created"]:
                c["oldest_created"] = created
    out = []
    for c in by_addr.values():
        best = c["best"] or {}
        out.append({"token_address": c["token_address"], "symbol": c["symbol"],
                    "name": c["name"], "chains": sorted(c["chains"]),
                    "pair_address": best.get("pair_address") or "",
                    "dex": best.get("dex") or "",
                    "price_usd": best.get("price_usd") or 0.0,
                    "liquidity_usd": best.get("liquidity_usd") or 0.0,
                    "volume24h_usd": best.get("volume24h_usd") or 0.0,
                    "price_change_24h_pct": best.get("price_change_24h_pct"),
                    "oldest_created": c["oldest_created"],
                    "n_pairs": len(c["pairs"])})
    out.sort(key=lambda c: c["liquidity_usd"], reverse=True)
    return out


def _socials_of(raw_pair):
    """Twitter/Telegram/Discord/website from one raw DexScreener pair."""
    info = (raw_pair or {}).get("info") or {}
    out = {"twitter_url": None, "handle": None, "telegram_url": None,
           "discord_url": None, "website_url": None}
    for s in info.get("socials") or []:
        kind = (s or {}).get("type") or ""
        url = (s or {}).get("url") or ""
        if kind == "twitter" and url:
            out["twitter_url"] = url
            out["handle"] = crypto._handle_from_url(url)
        elif kind == "telegram" and url:
            out["telegram_url"] = url
        elif kind == "discord" and url:
            out["discord_url"] = url
    sites = info.get("websites") or []
    if sites and isinstance(sites[0], dict) and sites[0].get("url"):
        out["website_url"] = sites[0]["url"]
    return out


def attach_socials(candidates, raw_pairs):
    """Socials per candidate, from its most liquid raw pair."""
    best_raw = {}
    for p in raw_pairs or []:
        addr = (((p or {}).get("baseToken") or {}).get("address") or "").lower()
        if not addr:
            continue
        liq = crypto._num(((p or {}).get("liquidity") or {}).get("usd"))
        if addr not in best_raw or liq > best_raw[addr][0]:
            best_raw[addr] = (liq, p)
    for c in candidates:
        c.update(_socials_of(best_raw.get(c["token_address"], (0, {}))[1]))
    return candidates


# ---------------------------------------------------------------- CoinGecko

def _cg_platform(candidate):
    for chain in candidate["chains"]:
        if chain in crypto.CG_PLATFORMS:
            return chain, crypto.CG_PLATFORMS[chain]
    return None, None


def attach_coingecko(candidates, pause=crypto.PAUSE, fetch=None):
    """CoinGecko listing per candidate: True (listed), False (404),
    None (UNKNOWN: no CG platform for its chains, or the check failed).
    At most MAX_CG_CHECKS calls, most liquid first."""
    fetch = fetch or crypto.fetch_coingecko_contract
    for c in candidates[:MAX_CG_CHECKS]:
        chain, platform = _cg_platform(c)
        c["cg_chain"] = chain
        if platform is None:
            c["cg_listed"] = None
            continue
        try:
            # fetch_coingecko_contract pauses internally; do not sleep twice.
            fetch(platform, c["token_address"])
            c["cg_listed"] = True
        except crypto.CryptoError:
            c["cg_listed"] = False
    for c in candidates[MAX_CG_CHECKS:]:
        c["cg_chain"] = None
        c["cg_listed"] = None
    return candidates


# ---------------------------------------------------------------- scoring

def _age_text(created, now):
    if not isinstance(created, int):
        return "umur tak diketahui"
    hours = crypto.pool_age_hours(created, now)
    if hours is None:
        return "umur tak diketahui"
    if hours < 48:
        return "umur %.1fj" % hours
    return "umur %.1f hari" % (hours / 24.0)


def score_candidates(candidates, now=None):
    """Score each candidate; returns the list sorted by score desc, each
    with score, reasons, and the shared-socials flag."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cands = [dict(c) for c in candidates]
    dated = [c for c in cands if isinstance(c.get("oldest_created"), int)]
    if dated:
        oldest = min(c["oldest_created"] for c in dated)
    else:
        oldest = None
    max_liq = max((c["liquidity_usd"] for c in cands), default=0.0)
    max_vol = max((c["volume24h_usd"] for c in cands), default=0.0)
    for c in cands:
        score, reasons = 0, []
        if oldest is not None and c.get("oldest_created") == oldest:
            score += SCORE_OLDEST
            reasons.append("paling tua (%s)" % _age_text(c["oldest_created"], now))
        if max_liq and c["liquidity_usd"] == max_liq:
            score += SCORE_LIQUIDITY
            reasons.append("likuiditas tertinggi")
        if max_vol and c["volume24h_usd"] == max_vol:
            score += SCORE_VOLUME
            reasons.append("volume 24j tertinggi")
        if c.get("twitter_url") or c.get("website_url"):
            score += SCORE_SOCIALS
            reasons.append("punya Twitter/website")
        if c.get("cg_listed") is True:
            score += SCORE_COINGECKO
            reasons.append("terdaftar di CoinGecko")
        c["score"] = score
        c["reasons"] = reasons
        c["age_text"] = _age_text(c.get("oldest_created"), now)
    # Same website/Twitter on two candidates = probably one project
    # multichain, not a clone of each other.
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            shared = None
            if a.get("website_url") and a["website_url"] == b.get("website_url"):
                shared = a["website_url"]
            elif a.get("twitter_url") and a["twitter_url"] == b.get("twitter_url"):
                shared = a["twitter_url"]
            if shared:
                a["shared_with"] = b["token_address"]
                b["shared_with"] = a["token_address"]
    cands.sort(key=lambda c: (c["score"], c["liquidity_usd"]), reverse=True)
    return cands


def verdict(scored):
    """(verdict, confidence, note) for a scored list."""
    if not scored:
        return "TIDAK ADA", "tidak ada", "tidak ada kandidat yang ditemukan"
    if len(scored) == 1:
        return ("SATU KANDIDAT", "lemah",
                "hanya 1 kandidat ditemukan, tidak ada pembanding, cek manual")
    top, second = scored[0], scored[1]
    if top["score"] - second["score"] >= 2:
        return ("REAL", "kuat",
                "skor %d vs %d, selisih jelas" % (top["score"], second["score"]))
    return ("BELUM JELAS", "lemah",
            "skor %d vs %d, terlalu dekat; yang bisa memastikan: akun X resmi, "
            "kontrak di CoinGecko, atau deployer" % (top["score"], second["score"]))


# ---------------------------------------------------------------- output

def _short(addr):
    return addr[:6] + "..." + addr[-4:] if len(addr) > 12 else addr


def _money(x):
    if x >= 1_000_000:
        return "$%.2fjt" % (x / 1_000_000)
    if x >= 1_000:
        return "$%.1frb" % (x / 1_000)
    return "$%.0f" % x


def _pct(change):
    if change is None:
        return "24j ?"
    return "24j %+.1f%%" % change


def _cg_text(c):
    if c.get("cg_listed") is True:
        return "CoinGecko: terdaftar"
    if c.get("cg_listed") is False:
        return "CoinGecko: tidak terdaftar"
    if c.get("cg_chain") is None and c.get("chains") == ["robinhood"]:
        return "CoinGecko: tak ada platform robinhood"
    return "CoinGecko: UNKNOWN"


def render_human(query, scored, vd):
    lines = ["%s: %d kandidat dengan simbol/nama sama" % (query.upper(), len(scored))]
    for i, c in enumerate(scored, 1):
        mark = "  <-- REAL" if i == 1 and vd[0] == "REAL" else ""
        if c.get("queried"):
            mark += "  <-- YANG DITANYAKAN"
        lines.append("%d. %s (%s) skor %d%s" % (
            i, c["symbol"], ", ".join(c["chains"]), c["score"], mark))
        lines.append("   token %s | %s | lik %s | vol 24j %s | %s | $%.6g" % (
            _short(c["token_address"]), c["age_text"], _money(c["liquidity_usd"]),
            _money(c["volume24h_usd"]), _pct(c.get("price_change_24h_pct")),
            c["price_usd"]))
        lines.append("   alasan: %s" % ("; ".join(c["reasons"]) or "tidak ada sinyal kuat"))
        lines.append("   %s%s" % (_cg_text(c),
                     " | Twitter: %s" % c["handle"] if c.get("handle") else ""))
        if c.get("shared_with"):
            lines.append("   kemungkinan proyek sama (multichain) dengan %s"
                         % _short(c["shared_with"]))
        lines.append("   https://dexscreener.com/%s/%s"
                     % (c["chains"][0], c["pair_address"]))
    lines.append("Verdict: %s (%s): %s" % (vd[0], vd[1], vd[2]))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def _read_json_file(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Real vs clone: token mana yang aslinya?")
    ap.add_argument("symbol", help="simbol atau nama token, mis. LETRO")
    ap.add_argument("--address", help="alamat token yang sedang dicek: diambil "
                    "langsung dan dinilai bersama para kandidat")
    ap.add_argument("--chain", help="batasi ke satu chain (mis. solana)")
    ap.add_argument("--limit", type=int, default=MAX_CANDIDATES,
                    help="kandidat terbanyak yang dinilai (%d)" % MAX_CANDIDATES)
    ap.add_argument("--offline", metavar="FILE",
                    help="file search.json DexScreener (cg.json di folder yang sama "
                         "untuk peta CoinGecko), bukan network")
    ap.add_argument("--pause", type=float, default=crypto.PAUSE)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.limit <= 0:
        print("limit harus >= 1", file=sys.stderr)
        return 1
    if a.pause < 0:
        print("pause tidak boleh negatif", file=sys.stderr)
        return 1
    try:
        if a.offline:
            doc = _read_json_file(a.offline)
            cg_map = {}
            cg_path = os.path.join(os.path.dirname(a.offline), "cg.json")
            if os.path.exists(cg_path):
                cg_map = _read_json_file(cg_path)
        else:
            doc = fetch_dexscreener_search(a.symbol, pause=a.pause)
            cg_map = None
    except (OSError, ValueError, crypto.CryptoError) as e:
        print("clone-check gagal: %s" % e, file=sys.stderr)
        return 1
    pools = crypto.parse_dexscreener_pairs(doc)
    query = a.symbol.upper()
    pools = [p for p in pools
             if p.get("symbol") == query or query.lower() in (p.get("name") or "").lower()]
    if a.chain:
        pools = [p for p in pools if p.get("chain") == a.chain]
    candidates = group_candidates(pools)
    raw_pairs = list((doc or {}).get("pairs") or [])
    if a.address:
        # Token yang ditanyakan diambil langsung: search bisa melewatkannya.
        try:
            if a.offline:
                token_path = os.path.join(os.path.dirname(a.offline), "token.json")
                qdoc = _read_json_file(token_path)
            else:
                qdoc = crypto.fetch_dexscreener_token(a.address, pause=a.pause)
        except (OSError, ValueError, crypto.CryptoError) as e:
            print("clone-check gagal mengambil token %s: %s" % (a.address, e),
                  file=sys.stderr)
            return 1
        qpools = crypto.parse_dexscreener_pairs(qdoc)
        if a.chain:
            qpools = [p for p in qpools if p.get("chain") == a.chain]
        raw_pairs.extend((qdoc or {}).get("pairs") or [])
        by_addr = {c["token_address"]: c for c in candidates}
        for qc in group_candidates(qpools):
            qc["queried"] = True
            by_addr[qc["token_address"]] = qc
        candidates = list(by_addr.values())
    queried = [c for c in candidates if c.get("queried")]
    rest = sorted((c for c in candidates if not c.get("queried")),
                  key=lambda c: c["liquidity_usd"], reverse=True)
    candidates = queried + rest[:max(0, a.limit - len(queried))]
    if not candidates:
        print("tidak ada kandidat %s yang ditemukan" % a.symbol, file=sys.stderr)
        return 1
    attach_socials(candidates, raw_pairs)
    if cg_map is not None:
        for c in candidates:
            chain, platform = _cg_platform(c)
            c["cg_chain"] = chain
            key = "%s:%s" % (platform, c["token_address"]) if platform else None
            c["cg_listed"] = cg_map.get(key) if key else None
    else:
        attach_coingecko(candidates, pause=a.pause)
    scored = score_candidates(candidates)
    vd = verdict(scored)
    if a.json:
        print(json.dumps({"query": a.symbol, "candidates": scored,
                          "verdict": {"label": vd[0], "confidence": vd[1],
                                      "note": vd[2]}}, indent=2))
    else:
        print(render_human(a.symbol, scored, vd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
