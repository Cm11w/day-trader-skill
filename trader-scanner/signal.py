#!/usr/bin/env python3
"""Sinyal trade meme: satu kartu sinyal, FORMAT TETAP, skor 0-100.

    python3 signal.py --token 0x1630... --symbol CLAUS
    python3 signal.py --token 0x... --symbol PEPE --chain solana --pair ADDR
    python3 signal.py --token 0x... --mentions mentions.json --json
    python3 signal.py --token 0x... --offline tests/fixtures/crypto/signal

Satu token -> satu kartu, selalu 12 baris, selalu urutan dan label yang
sama, apapun datanya. Data yang tidak ada ditulis UNKNOWN, barisnya tidak
pernah dihapus atau dipindah. 10 baris pertama adalah format asli; baris
Bonding ditambahkan di akhir (7 Nov 2026) atas permintaan user, dan baris
Kreator ditambahkan setelahnya (7 Nov 2026) atas permintaan user:

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

  Top 10 Holder:
  1. {alamat} - {pct}%
  ... (sampai 10; atau satu baris UNKNOWN bila tidak ada data holder)

Skor 0-100 (transparan, tiap komponen bisa dilacak ke tool asalnya):
  Momentum 30          harga 24j 15 (naik >=200% penuh) + volume 24j 15
  Likuiditas-Keamanan  likuiditas 10 + risk check 15 (PASS penuh, UNKNOWN 7, FAIL 0)
  Keaslian 20          clone REAL + rank 1 20, BELUM JELAS + rank 1 12,
                       SATU KANDIDAT 10, rank 2 5, rank >2 0, check gagal
                       10 (netral, ditandai). The score belongs to the
                       QUERIED token: a rank-2 clone never inherits the
                       REAL verdict's points. A BELUM JELAS rank-1 token
                       that is listed on CoinMarketCap scores 15 instead
                       of 12: the listing resolves the uncertainty upward.
  Entry 15             TRIGGERED 15; WAITING <=5% di bawah high 12, <=10% 9,
                       <=20% 5, lebih jauh 2; tanpa data 0
  Time Machine 10      target duluan >=80% 10, >=65% 7, >=50% 5, >=35% 3,
                       di bawah itu 1; tanpa data 5 (netral, ditandai)
  Kreator (modifier)   kreator pernah rug -15; token lain bersih +5;
                       pertama kali / tidak diketahui 0. Total dijepit
                       0-100, jadi modifier tidak pernah menembus band.
  Band: >=80 KUAT, 60-79 MENARIK, 40-59 WASPADA, <40 LEMAH.

Kartu ini merangkai tool yang sudah ada (bukan logika baru): clone_check
(keaslian), entry check (crypto-breakout), exit plan (stop/target 2R),
timemachine (10.000 replay), check.py crypto (risk), community.py
(komunitas), dan CoinMarketCap (keaslian + komunitas + harga). Skor BUKAN
rekomendasi beli: sinyal kuat pun bisa rugi, dan skor tidak menggantikan
preview + Ya untuk eksekusi apa pun.

Blok Top 10 Holder selalu tampil setelah kartu (pesan yang sama), dengan
alamat dipendekkan (4 karakter awal ... 4 karakter akhir) dan persen dari
circulating supply. Daftar holder datang dari risk check (GoPlus EVM dan
Solana, RugCheck untuk Solana); persen API adalah persen dari total
supply, dan dikonversi ke persen circulating supply bila supply beredar
diketahui (CoinMarketCap dulu, lalu CoinGecko). Bila tidak bisa
dikonversi, persen API dipakai apa adanya. Tanpa data holder, bloknya
satu baris UNKNOWN.

CA (contract address) cukup sebagai input: chain dan pool ditemukan dari
DexScreener lewat alamat tokennya. --chain/--pair hanya untuk mengunci
satu pool tertentu. Komunitas tanpa --mentions dilaporkan UNKNOWN, bukan
dikarang (X tidak punya API gratis).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto  # noqa: E402  (DexScreener fetch + parse + chain maps)
import creator  # noqa: E402  (creator rug history: RugCheck + GoPlus)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY_PY = os.path.join(ROOT, "trader-entry", "entry.py")
EXIT_PY = os.path.join(ROOT, "trader-exit", "exit.py")
TM_PY = os.path.join(ROOT, "trader-timemachine", "timemachine.py")
RISK_PY = os.path.join(ROOT, "trader-risk", "check.py")
COMMUNITY_PY = os.path.join(ROOT, "trader-news", "community.py")
CLONE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clone_check.py")
CMC_PY = os.path.join(os.path.expanduser("~"), "workspace", "custom-connectors",
                      "custom.coinmarketcap", "bin", "cmc.py")

BANDS = ((80, "KUAT"), (60, "MENARIK"), (40, "WASPADA"), (0, "LEMAH"))


# ---------------------------------------------------------------- runner

def _run_cmd(argv, timeout=120):
    """Run a sub-tool, return (exit_code, stdout, stderr). Swappable in tests."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ds" % timeout
    except OSError as e:
        return 127, "", str(e)


def _run_json(argv, timeout=120):
    """Run a sub-tool that prints JSON; return (doc, error). A nonzero exit
    with valid JSON is still a result (risk check exits 1 on FAIL)."""
    code, out, err = _run_cmd(argv, timeout=timeout)
    try:
        return json.loads(out), None
    except ValueError:
        return None, (err.strip() or out.strip() or "exit %d" % code)[-200:]


# ---------------------------------------------------------------- formatting

def _money(x):
    if x is None:
        return "UNKNOWN"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if x >= 1_000_000:
        return "$%.2fjt" % (x / 1_000_000)
    if x >= 1_000:
        return "$%.1frb" % (x / 1_000)
    return "$%.0f" % x


def _price(x):
    if x is None:
        return "UNKNOWN"
    try:
        return "$%.6g" % float(x)
    except (TypeError, ValueError):
        return "UNKNOWN"


def _pct(x):
    if x is None:
        return "UNKNOWN"
    try:
        return "%+.1f%%" % float(x)
    except (TypeError, ValueError):
        return "UNKNOWN"


def _age(hours):
    if hours is None:
        return "UNKNOWN"
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if hours < 48:
        return "%.1fj" % hours
    return "%.1f hari" % (hours / 24.0)


def _short(addr):
    if not addr:
        return "-"
    return addr[:6] + "..." + addr[-4:] if len(addr) > 12 else addr


def _fmt_int(x):
    """1,200 -> '1.200' (pemisah ribuan Indonesia), atau UNKNOWN."""
    if x is None:
        return "UNKNOWN"
    try:
        return "{:,}".format(int(x)).replace(",", ".")
    except (TypeError, ValueError):
        return "UNKNOWN"


# ---------------------------------------------------------------- pool facts

def resolve_token(chain, pair):
    """A pair address -> its token address, via the DexScreener pair
    endpoint. Lets the watch worker call signal.py with --chain/--pair only
    (seen.json stores no token address)."""
    if not chain or not pair:
        raise ValueError("butuh --chain dan --pair untuk mencari alamat token")
    try:
        doc = crypto.fetch_json("%s/latest/dex/pairs/%s/%s" % (crypto.DS_BASE, chain, pair))
    except crypto.CryptoError as e:
        raise ValueError(str(e))
    pairs = (doc or {}).get("pairs") or []
    if not pairs:
        raise ValueError("DexScreener tidak punya pool %s di %s" % (pair, chain))
    addr = ((pairs[0] or {}).get("baseToken") or {}).get("address")
    if not addr:
        raise ValueError("pool %s tidak punya alamat token" % pair)
    return addr


def fetch_pool(token, chain=None, pair=None, offline=None):
    """Pool facts for a token address -> (pool dict, pairs doc, error)."""
    if offline:
        path = os.path.join(offline, "dexscreener-pairs.json")
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            return None, None, "tidak bisa baca %s: %s" % (path, e)
    else:
        try:
            doc = crypto.fetch_dexscreener_token(token)
        except crypto.CryptoError as e:
            return None, None, str(e)
    pools = crypto.parse_dexscreener_pairs(doc)
    if chain:
        pools = [p for p in pools if p.get("chain") == chain]
    if pair:
        want = pair.lower()
        pools = [p for p in pools
                 if str(p.get("pair_address") or "").lower() == want]
    if not pools:
        return None, doc, "DexScreener tidak punya pool untuk token ini"
    pools.sort(key=lambda p: p.get("liquidity_usd") or 0, reverse=True)
    return pools[0], doc, None


# ---------------------------------------------------------------- sub-tools

def run_clone(symbol, token, timeout=120):
    doc, err = _run_json([sys.executable, CLONE_PY, symbol, "--address", token,
                          "--json"], timeout=timeout)
    if err is not None:
        return {"error": err}
    vd = (doc or {}).get("verdict") or {}
    cands = (doc or {}).get("candidates") or []
    top = cands[0] if cands else {}
    queried = [c for c in cands if c.get("queried")]
    rank = None
    for i, c in enumerate(cands, 1):
        if c.get("queried"):
            rank = i
    return {"verdict": vd.get("label") or "UNKNOWN",
            "confidence": vd.get("confidence") or "",
            "top_score": top.get("score"),
            "second_score": cands[1].get("score") if len(cands) > 1 else None,
            "top_chain": (top.get("chains") or [None])[0],
            "top_age": top.get("age_text"),
            "queried_rank": rank,
            "shared": bool(top.get("shared_with")),
            "n": len(cands)}


def run_entry(chain, pair, tmpdir, timeout=120):
    doc, err = _run_json([sys.executable, ENTRY_PY, "check", "--asset", "crypto",
                          "--chain", chain, "--pair", pair, "--interval", "5m",
                          "--json", "--write", os.path.join(tmpdir, "entry.json")],
                         timeout=timeout)
    if err is not None:
        return {"error": err}
    last = (doc or {}).get("last") or {}
    return {"status": (doc or {}).get("status") or "UNKNOWN",
            "level": (doc or {}).get("level"),
            "close": last.get("close"),
            "vs_level_pct": last.get("vs_level_pct"),
            "price": ((doc or {}).get("trigger") or {}).get("price"),
            "stop": (doc or {}).get("stop"),
            "target": (doc or {}).get("target"),
            "reason": (doc or {}).get("reason") or ""}


def run_exit(chain, pair, entry_price, tmpdir, timeout=120):
    if entry_price is None:
        return {"error": "tidak ada harga entry untuk rencana exit"}
    doc, err = _run_json([sys.executable, EXIT_PY, "plan", "--asset", "crypto",
                          "--chain", chain, "--pair", pair, "--entry", str(entry_price),
                          "--json", "--write", os.path.join(tmpdir, "plan.json")],
                         timeout=timeout)
    if err is not None:
        return {"error": err}
    return {"stop": (doc or {}).get("stop"), "target": (doc or {}).get("target"),
            "target_r": (doc or {}).get("target_r")}


def run_tm(chain, pair, tmpdir, replays=10000, timeout=240):
    plan = os.path.join(tmpdir, "plan.json")
    if not os.path.exists(plan):
        return {"error": "tidak ada rencana exit untuk Time Machine"}
    doc, err = _run_json([sys.executable, TM_PY, "run", "--asset", "crypto",
                          "--chain", chain, "--pair", pair, "--plan", plan,
                          "--replays", str(replays), "--json",
                          "--write", os.path.join(tmpdir, "tm.json")],
                         timeout=timeout)
    if err is not None:
        return {"error": err}
    mc = (doc or {}).get("mc") or {}
    return {"p_target": mc.get("p_target"), "avg_R": mc.get("avg_R")}


def run_risk(chain, pair, token, offline=None, timeout=120):
    argv = [sys.executable, RISK_PY, "crypto", "--chain", chain, "--pair", pair,
            "--token", token, "--json"]
    if offline:
        argv += ["--offline", offline]
    doc, err = _run_json(argv, timeout=timeout)
    if err is not None:
        return {"error": err}
    checks = (doc or {}).get("checks") or []
    bad = [c for c in checks if not c.get("pass")]
    reasons = []
    for c in bad:
        reasons.append((c.get("detail") or c.get("name") or "").strip())
    unknowns = (doc or {}).get("unknowns") or []
    reasons.extend(str(u) for u in unknowns)
    # The same gap is often reported twice (a failed check + an unknown):
    # keep the longest wording of each, in order.
    deduped = []
    for r in reasons:
        if not r:
            continue
        if any(r in other for other in deduped):
            continue
        deduped = [other for other in deduped if other not in r]
        deduped.append(r)
    # The holder list for the Top 10 Holder block: the security record's
    # top holders (GoPlus EVM and Solana, RugCheck for Solana), already
    # sorted by percent of total supply, biggest first.
    holders = []
    sec = (doc or {}).get("security") or {}
    for h in sec.get("top_holders") or []:
        if not isinstance(h, dict):
            continue
        addr = h.get("address") or ""
        pct = h.get("percent")
        try:
            pct = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct = None
        bal = h.get("balance")
        try:
            bal = float(bal) if bal not in (None, "") else None
        except (TypeError, ValueError):
            bal = None
        if addr:
            holders.append({"address": addr, "percent": pct, "balance": bal})
    return {"verdict": (doc or {}).get("verdict") or "UNKNOWN",
            "reasons": deduped, "holders": holders,
            "total_supply": sec.get("total_supply")}


def run_creator(chain, token, offline=None):
    """Creator rug history for the token (creator.check_creator). Offline
    mode never touches the network: the verdict is UNKNOWN. Never raises:
    every failure is an UNKNOWN verdict with the reason."""
    if offline:
        return {"creator": None, "prior": None, "rugs": None, "checked": 0,
                "verdict": "UNKNOWN", "modifier": 0, "source": None,
                "insider_networks": None, "creator_short": None,
                "error": "offline mode: no creator data"}
    try:
        return creator.check_creator(chain, token)
    except Exception as e:
        return {"creator": None, "prior": None, "rugs": None, "checked": 0,
                "verdict": "UNKNOWN", "modifier": 0, "source": None,
                "insider_networks": None, "creator_short": None,
                "error": "%s: %s" % (type(e).__name__, e)}


def run_bonding(f, offline=None):
    """Bonding-curve status for the pool (crypto.bonding_info). Quote
    prices are fetched live only when the pool is on a bonding curve;
    offline mode and any fetch failure leave the progress UNKNOWN while
    the status is still shown. Never raises."""
    dex = (f.get("dex") or "").strip().lower()
    if dex not in crypto.BONDING_CURVES or offline:
        return crypto.bonding_info(f.get("chain"), f.get("dex"),
                                   f.get("token"), f.get("market_cap"), {})
    try:
        prices = crypto.fetch_quote_prices()
    except Exception:
        prices = {}
    return crypto.bonding_info(f.get("chain"), f.get("dex"),
                               f.get("token"), f.get("market_cap"), prices)


def run_community(pairs_doc, chain, token, mentions=None, offline=None, timeout=120):
    tmpdir = tempfile.mkdtemp(prefix="signal-comm-")
    pairs_path = os.path.join(tmpdir, "pairs.json")
    with open(pairs_path, "w", encoding="utf-8") as fh:
        json.dump(pairs_doc, fh)
    argv = [sys.executable, COMMUNITY_PY, "check", "--pairs", pairs_path]
    cg = None
    if offline:
        cg_path = os.path.join(offline, "coingecko.json")
        if os.path.exists(cg_path):
            argv += ["--cg", cg_path]
            try:
                with open(cg_path, encoding="utf-8") as fh:
                    cg = json.load(fh)
            except (OSError, ValueError):
                cg = None
    else:
        platform = crypto.CG_PLATFORMS.get(chain)
        if platform:
            try:
                cg = crypto.fetch_coingecko_contract(platform, token)
            except crypto.CryptoError:
                cg = None
            if cg is not None:
                cg_path = os.path.join(tmpdir, "cg.json")
                with open(cg_path, "w", encoding="utf-8") as fh:
                    json.dump(cg, fh)
                argv += ["--cg", cg_path]
    if mentions:
        argv += ["--mentions", mentions]
    doc, err = _run_json(argv, timeout=timeout)
    if err is not None:
        return {"error": err}
    official = (doc or {}).get("official") or {}
    presence = (doc or {}).get("presence") or {}
    activity = (doc or {}).get("activity") or {}
    posts = activity.get("posts")
    # Fixed vocabulary: derive the level from the post count, never from a
    # free-text level, so the card's wording never changes. Without a
    # mentions file there was no look at all: UNKNOWN, not "no activity".
    if mentions is None:
        level = "UNKNOWN"
    elif isinstance(posts, int):
        level = "AKTIF" if posts >= 10 else "SEPI" if posts >= 1 else "TIDAK ADA"
    else:
        level = "UNKNOWN"
    # Supply for the Top 10 Holder conversion (percent of circulating
    # supply): CoinGecko is the fallback when CoinMarketCap has no data.
    md = (cg or {}).get("market_data") or {}
    circ = md.get("circulating_supply")
    total = md.get("total_supply")
    try:
        circ = float(circ) if circ is not None else None
    except (TypeError, ValueError):
        circ = None
    try:
        total = float(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return {"handle": official.get("handle"),
            "followers": presence.get("twitter_followers"),
            "activity": level, "posts": posts,
            "kols": len((doc or {}).get("kols") or []),
            "circulating_supply": circ, "total_supply": total}


def _parse_cmc_map(doc, token):
    """A CMC map doc -> (listed, cmc_id). Listed only when one entry's
    platform token address is this exact token (case-insensitive for EVM):
    several coins can share one symbol, and the symbol alone proves nothing."""
    data = (doc or {}).get("data") or []
    for coin in data:
        if not isinstance(coin, dict):
            continue
        platform = coin.get("platform") or {}
        addr = platform.get("token_address") or ""
        if addr and token and addr.lower() == token.lower():
            return True, coin.get("id")
    return False, None


def _parse_cmc_quotes(doc):
    """A CMC quotes doc -> (price_usd, market_cap_usd, circulating_supply,
    total_supply). The data is keyed by symbol or by id string; either way
    there is one coin in it (quotes run by id)."""
    data = (doc or {}).get("data") or {}
    coin = None
    if isinstance(data, dict):
        vals = list(data.values())
        coin = vals[0] if vals and isinstance(vals[0], dict) else None
    elif isinstance(data, list) and data:
        coin = data[0] if isinstance(data[0], dict) else None
    if not coin:
        return None, None, None, None
    quote = (coin.get("quote") or {}).get("USD") or {}
    price = quote.get("price")
    mcap = quote.get("market_cap")
    circ = coin.get("circulating_supply")
    total = coin.get("total_supply")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    try:
        mcap = float(mcap) if mcap is not None else None
    except (TypeError, ValueError):
        mcap = None
    try:
        circ = float(circ) if circ is not None else None
    except (TypeError, ValueError):
        circ = None
    try:
        total = float(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return price, mcap, circ, total


def _parse_cmc_metadata(doc):
    """A CMC info doc -> the official Twitter handle, or None. The data is
    keyed by symbol or by id string; either way there is one coin in it."""
    data = (doc or {}).get("data") or {}
    coin = None
    if isinstance(data, dict):
        vals = list(data.values())
        coin = vals[0] if vals and isinstance(vals[0], dict) else None
    elif isinstance(data, list) and data:
        coin = data[0] if isinstance(data[0], dict) else None
    if not coin:
        return None
    urls = (coin.get("urls") or {}).get("twitter") or []
    if urls:
        return crypto._handle_from_url(urls[0])
    return None


def _read_cmc_fixture(offline, name):
    """One CMC fixture JSON from the offline folder, or None."""
    path = os.path.join(offline, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def run_cmc(symbol, token, chain, offline=None, timeout=120):
    """CoinMarketCap enrichment -> {listed, cmc_id, price_usd,
    market_cap_usd, circulating_supply, total_supply, twitter_handle,
    error}. A symbol CMC does not list answers 400 with credit_count 0:
    that means not listed, not an error. Quotes and metadata run only
    when the token is listed, so a fresh meme costs 1 credit (the map
    call) and a listed coin costs 3."""
    out = {"listed": None, "cmc_id": None, "price_usd": None,
           "market_cap_usd": None, "circulating_supply": None,
           "total_supply": None, "twitter_handle": None, "error": None}
    if not symbol:
        return out
    if offline:
        map_doc = _read_cmc_fixture(offline, "cmc-map.json")
        if map_doc is None:
            return out
        listed, cmc_id = _parse_cmc_map(map_doc, token)
        out["listed"] = listed
        out["cmc_id"] = cmc_id
        if listed:
            price, mcap, circ, total = _parse_cmc_quotes(
                _read_cmc_fixture(offline, "cmc-quotes.json") or {})
            out.update(price_usd=price, market_cap_usd=mcap,
                       circulating_supply=circ, total_supply=total)
            out["twitter_handle"] = _parse_cmc_metadata(
                _read_cmc_fixture(offline, "cmc-metadata.json") or {})
        return out
    doc, err = _run_json([sys.executable, CMC_PY, "map", "--symbol", symbol],
                         timeout=timeout)
    if err is not None:
        # CMC answers 400 "Invalid value for symbol" for a symbol it does
        # not list: that is a fact (not listed), not a failure.
        if "HTTP Error 400" in err or "Invalid value for" in err:
            out["listed"] = False
            return out
        out["error"] = err
        return out
    listed, cmc_id = _parse_cmc_map(doc, token)
    out["listed"] = listed
    out["cmc_id"] = cmc_id
    if not listed:
        return out
    qdoc, qerr = _run_json([sys.executable, CMC_PY, "quotes", "--id", str(cmc_id)],
                           timeout=timeout)
    if qerr is None:
        price, mcap, circ, total = _parse_cmc_quotes(qdoc)
        out.update(price_usd=price, market_cap_usd=mcap,
                   circulating_supply=circ, total_supply=total)
    mdoc, merr = _run_json([sys.executable, CMC_PY, "metadata", "--id", str(cmc_id)],
                           timeout=timeout)
    if merr is None:
        out["twitter_handle"] = _parse_cmc_metadata(mdoc)
    return out


def _holder_circ_pct(holder, circ, total):
    """One holder -> percent of the circulating supply (0-100), or None.
    The API percent is of the total supply, so it is converted when the
    circulating supply is known: balance / circulating supply when the
    balance is there, else percent * total / circulating. Without a
    circulating supply the API percent is used as-is; without any number
    the holder has no percent."""
    pct = holder.get("percent")
    bal = holder.get("balance")
    if circ and circ > 0 and bal is not None:
        try:
            return float(bal) / float(circ) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            return None
        if circ and total and circ > 0 and total > 0:
            try:
                return pct * float(total) / float(circ) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        return pct * 100.0
    return None


def holder_rows(f):
    """The facts -> the Top 10 Holder rows [{address, pct}], pct in percent
    of the circulating supply. The holder list comes from the risk check
    (GoPlus, RugCheck for Solana); the supply comes from CoinMarketCap
    first, then CoinGecko. Empty when there is no holder data."""
    risk = f.get("risk") or {}
    holders = risk.get("holders") or []
    if not holders:
        return []
    cmc = f.get("cmc") or {}
    comm = f.get("community") or {}
    circ = cmc.get("circulating_supply") or comm.get("circulating_supply")
    total = cmc.get("total_supply")
    if total is None:
        total = comm.get("total_supply")
    if total is None:
        total = risk.get("total_supply")
    rows = []
    for h in holders:
        addr = (h or {}).get("address") or ""
        if not addr:
            continue
        pct = _holder_circ_pct(h, circ, total)
        if pct is None:
            continue
        rows.append({"address": addr, "pct": pct})
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows[:10]


def _short_addr(addr):
    """An address shortened for a phone screen: 4 chars ... 4 chars."""
    if not addr:
        return "-"
    return addr[:4] + "..." + addr[-4:] if len(addr) > 8 else addr


def _pct2(x):
    """A holder percent: two decimals, four when it is under 0.01%."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "%.2f" % x if x >= 0.01 else "%.4f" % x


def render_holders(f):
    """The Top 10 Holder block: always present, after the card. One line
    per holder with a shortened address and its percent of the circulating
    supply; one UNKNOWN line when there is no holder data."""
    rows = f.get("holders") or holder_rows(f)
    if not rows:
        return "Top 10 Holder: UNKNOWN - tidak ada data holder"
    rows = sorted(rows, key=lambda r: r["pct"], reverse=True)[:10]
    lines = ["Top 10 Holder:"]
    for i, r in enumerate(rows, 1):
        lines.append("%d. %s - %s%%" % (i, _short_addr(r["address"]), _pct2(r["pct"])))
    return "\n".join(lines)


def apply_cmc_handle(community, cmc_handle):
    """A CoinMarketCap Twitter handle fills the Community row only when
    DexScreener and CoinGecko have none. Followers and activity stay as
    community.py reported them."""
    if cmc_handle and not (community or {}).get("handle"):
        return {"handle": cmc_handle,
                "followers": (community or {}).get("followers"),
                "activity": (community or {}).get("activity") or "UNKNOWN",
                "posts": (community or {}).get("posts"),
                "kols": (community or {}).get("kols", 0),
                "circulating_supply": (community or {}).get("circulating_supply"),
                "total_supply": (community or {}).get("total_supply")}
    return community


# ---------------------------------------------------------------- score

def _band(total):
    for floor, name in BANDS:
        if total >= floor:
            return name
    return "LEMAH"


def score_signal(f):
    """Facts -> {total, band, parts{...}}. Missing data scores the UNKNOWN
    value of its component and the card flags it."""
    parts = {}
    chg = f.get("change_24h")
    price_pts = 0
    if chg is not None:
        if chg >= 200:
            price_pts = 15
        elif chg >= 100:
            price_pts = 12
        elif chg >= 50:
            price_pts = 9
        elif chg >= 20:
            price_pts = 6
        elif chg >= 0:
            price_pts = 3
    vol = f.get("volume24h") or 0
    if vol >= 1_000_000:
        vol_pts = 15
    elif vol >= 500_000:
        vol_pts = 12
    elif vol >= 250_000:
        vol_pts = 10
    elif vol >= 100_000:
        vol_pts = 8
    elif vol >= 25_000:
        vol_pts = 5
    elif vol >= 5_000:
        vol_pts = 3
    else:
        vol_pts = 0
    parts["momentum"] = price_pts + vol_pts

    liq = f.get("liquidity") or 0
    if liq >= 500_000:
        liq_pts = 10
    elif liq >= 250_000:
        liq_pts = 8
    elif liq >= 100_000:
        liq_pts = 6
    elif liq >= 50_000:
        liq_pts = 4
    elif liq >= 10_000:
        liq_pts = 2
    else:
        liq_pts = 0
    risk = (f.get("risk") or {}).get("verdict")
    risk_pts = {"pass": 15, "unknown": 7, "fail": 0}.get(risk, 7)
    parts["safety"] = liq_pts + risk_pts

    clone = f.get("clone") or {}
    if clone.get("error"):
        clone_pts = 10
    elif clone.get("queried_rank") == 1:
        # The queried token IS the rank-1 candidate: the verdict is about it.
        if clone.get("verdict") == "REAL":
            clone_pts = 20
        elif clone.get("verdict") == "BELUM JELAS":
            # A CoinMarketCap listing resolves the uncertainty upward: the
            # token is a real listed project, not just an unjudged clone.
            clone_pts = 15 if clone.get("cmc_listed") else 12
        else:
            clone_pts = 10
    elif clone.get("queried_rank") == 2:
        # A clear original exists and it is NOT the queried token: the
        # queried pool is a clone. It must not inherit the REAL verdict's
        # points (a robinhood Anthropic scored 20/20 here before the fix).
        clone_pts = 5
    elif clone.get("queried_rank") is not None:
        clone_pts = 0
    else:
        clone_pts = 10
    parts["authenticity"] = clone_pts

    entry = f.get("entry") or {}
    if entry.get("error"):
        entry_pts = 0
    elif entry.get("status") == "triggered":
        entry_pts = 15
    elif entry.get("status") == "waiting":
        vs = entry.get("vs_level_pct")
        if vs is None:
            entry_pts = 2
        elif vs >= -5:
            entry_pts = 12
        elif vs >= -10:
            entry_pts = 9
        elif vs >= -20:
            entry_pts = 5
        else:
            entry_pts = 2
    else:
        entry_pts = 0
    parts["entry"] = entry_pts

    tm = f.get("tm") or {}
    if tm.get("error"):
        tm_pts = 5
    else:
        p = tm.get("p_target")
        if p is None:
            tm_pts = 5
        elif p >= 0.80:
            tm_pts = 10
        elif p >= 0.65:
            tm_pts = 7
        elif p >= 0.50:
            tm_pts = 5
        elif p >= 0.35:
            tm_pts = 3
        else:
            tm_pts = 1
    parts["tm"] = tm_pts

    # Creator rug history: a modifier, not a component. A creator who
    # rugged before costs 15; a creator with clean prior tokens earns 5;
    # a first launch or an unknowable history is neutral (0). The total
    # is clamped to 0-100 so the modifier can never break the band.
    parts["creator"] = (f.get("creator") or {}).get("modifier", 0) or 0
    total = max(0, min(100, sum(parts.values())))
    return {"total": total, "band": _band(total), "parts": parts}


# ---------------------------------------------------------------- fixed card

def render_signal(f):
    """The ONE fixed card: 12 lines, same labels and order, always. A value
    that is missing prints UNKNOWN; a line is never dropped or moved.
    Lines 1-10 are the original format; line 11 (Bonding) was added
    7 Nov 2026 at the user's request, line 12 (Kreator) the same day."""
    score = f.get("score") or score_signal(f)
    parts = score["parts"]
    clone = f.get("clone") or {}
    if clone.get("error"):
        clone_line = "UNKNOWN - clone check gagal"
    else:
        top = "%s, %s" % (clone.get("top_chain") or "-",
                          clone.get("top_age") or "umur UNKNOWN")
        shared = "; proyek sama multichain" if clone.get("shared") else ""
        rank = clone.get("queried_rank")
        not_first = ("; pool ini peringkat %d" % rank) if rank and rank > 1 else ""
        cmc = "; terdaftar di CMC" if clone.get("cmc_listed") else ""
        clone_line = "%s - peringkat 1 (token asli, %s)%s%s%s" % (
            clone.get("verdict") or "UNKNOWN", top, shared, not_first, cmc)
    entry = f.get("entry") or {}
    if entry.get("error"):
        entry_line = "UNKNOWN - entry check gagal"
    elif entry.get("status") == "triggered":
        entry_line = "TRIGGERED - close di atas high 24j %s (%s)" % (
            _price(entry.get("level")), _pct(entry.get("vs_level_pct")))
    elif entry.get("status") == "waiting":
        entry_line = "WAITING - entry %s (high 24j), close %s di bawahnya" % (
            _price(entry.get("level")), _pct(entry.get("vs_level_pct")))
    else:
        entry_line = "UNKNOWN - %s" % (entry.get("reason") or "data tidak tersedia")
    stop = (f.get("exit") or {}).get("stop")
    target = (f.get("exit") or {}).get("target")
    if (f.get("exit") or {}).get("error"):
        stop_line = "UNKNOWN | Target: UNKNOWN (2R)"
    else:
        stop_line = "%s | Target: %s (2R)" % (_price(stop), _price(target))
    tm = f.get("tm") or {}
    if tm.get("error") or tm.get("p_target") is None:
        tm_line = "UNKNOWN - data tidak tersedia"
    else:
        tm_line = "target duluan %.1f%% | rata-rata %+.2fR" % (
            tm["p_target"] * 100, tm.get("avg_R") or 0)
    risk = f.get("risk") or {}
    if risk.get("error"):
        risk_line = "UNKNOWN - risk check gagal"
    else:
        reasons = "; ".join((risk.get("reasons") or [])[:2])
        if len(reasons) > 140:
            cut = reasons.rfind("; ", 0, 140)
            reasons = (reasons[:cut] if cut > 40 else reasons[:137]) + "..."
        risk_line = "%s%s" % ((risk.get("verdict") or "UNKNOWN").upper(),
                              (" - " + reasons) if reasons else "")
    comm = f.get("community") or {}
    if comm.get("error"):
        comm_line = "UNKNOWN | followers: UNKNOWN | aktivitas: UNKNOWN"
    else:
        handle = ("@" + comm["handle"]) if comm.get("handle") else "-"
        comm_line = "%s | followers: %s | aktivitas: %s" % (
            handle, _fmt_int(comm.get("followers")),
            comm.get("activity") or "UNKNOWN")
    bonding_line = (f.get("bonding") or {}).get("line") or \
        "Bonding: UNKNOWN - tidak ada data bonding"
    creator_line = creator.creator_line(f.get("creator") or {})
    creator_mod = (f.get("creator") or {}).get("modifier", 0) or 0
    creator_mod_txt = ("+%d" % creator_mod) if creator_mod > 0 else str(creator_mod)
    lines = [
        "*SINYAL: %s (%s) - Skor %d/100 %s*" % (
            f.get("symbol") or "UNKNOWN", f.get("chain") or "-",
            score["total"], score["band"]),
        "Harga: %s | 24j %s | Vol %s | Lik %s | FDV %s | MCap %s | Umur pool %s" % (
            _price(f.get("price_usd")), _pct(f.get("change_24h")),
            _money(f.get("volume24h")), _money(f.get("liquidity")),
            _money(f.get("fdv")),
            _money(f.get("market_cap")) if f.get("market_cap") else "UNKNOWN",
            _age(f.get("age_hours"))),
        "Keaslian: %s" % clone_line,
        "Entry: %s" % entry_line,
        "Stop: %s" % stop_line,
        "Time Machine: %s" % tm_line,
        "Risk: %s" % risk_line,
        "Komunitas: %s" % comm_line,
        "Link: https://dexscreener.com/%s/%s" % (f.get("chain") or "-",
                                               f.get("pair") or "-"),
        "Skor: Momentum %d/30 | Likuiditas-Keamanan %d/25 | Keaslian %d/20 | "
        "Entry %d/15 | Time Machine %d/10 | Kreator %s" % (
            parts["momentum"], parts["safety"], parts["authenticity"],
            parts["entry"], parts["tm"], creator_mod_txt),
        bonding_line,
        creator_line,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- build

def build_signal(token, symbol=None, chain=None, pair=None, mentions=None,
                 offline=None, replays=10000):
    """Run every leg for one token -> the facts dict (with score)."""
    pool, pairs_doc, err = fetch_pool(token, chain=chain, pair=pair, offline=offline)
    if err is not None:
        return {"error": err}
    now = crypto.now_utc()
    age = crypto.pool_age_hours(pool.get("pair_created_at"), now)
    f = {"symbol": (pool.get("symbol") or symbol or "UNKNOWN"),
         "name": pool.get("name") or "",
         "chain": pool.get("chain") or "-",
         "pair": pool.get("pair_address") or "-",
         "token": pool.get("token_address") or token,
         "dex": pool.get("dex") or "",
         "market_cap": pool.get("market_cap_usd"),
         "price_usd": pool.get("price_usd"),
         "change_24h": pool.get("price_change_24h_pct"),
         "volume24h": pool.get("volume24h_usd"),
         "liquidity": pool.get("liquidity_usd"),
         "fdv": pool.get("fdv_usd"),
         "age_hours": age}
    f["clone"] = run_clone(f["symbol"], f["token"])
    f["cmc"] = run_cmc(f["symbol"], f["token"], f["chain"], offline=offline)
    if not f["clone"].get("error"):
        f["clone"]["cmc_listed"] = f["cmc"].get("listed")
    # A CoinMarketCap quote fills a price DexScreener never reported; it
    # never overrides a DexScreener price.
    if not f.get("price_usd") and (f["cmc"].get("price_usd") is not None):
        f["price_usd"] = f["cmc"]["price_usd"]
    tmpdir = tempfile.mkdtemp(prefix="signal-")
    f["entry"] = run_entry(f["chain"], f["pair"], tmpdir)
    entry_px = None
    if f["entry"].get("status") == "triggered":
        entry_px = f["entry"].get("price")
    elif f["entry"].get("level") is not None:
        entry_px = f["entry"].get("level")
    f["exit"] = run_exit(f["chain"], f["pair"], entry_px, tmpdir)
    f["tm"] = run_tm(f["chain"], f["pair"], tmpdir, replays=replays)
    f["risk"] = run_risk(f["chain"], f["pair"], f["token"], offline=offline)
    f["community"] = run_community(pairs_doc, f["chain"], f["token"],
                                   mentions=mentions, offline=offline)
    # A CoinMarketCap Twitter handle fills the Community row only when
    # DexScreener and CoinGecko have none.
    f["community"] = apply_cmc_handle(f["community"], (f["cmc"] or {}).get("twitter_handle"))
    f["holders"] = holder_rows(f)
    f["bonding"] = run_bonding(f, offline=offline)
    f["creator"] = run_creator(f["chain"], f["token"], offline=offline)
    f["score"] = score_signal(f)
    return f


def record_waiting(f, path, now=None):
    """A pool whose entry is WAITING -> waiting.json, so the watch can
    re-alert once the price breaks above the entry level. A pool already
    marked triggered is never resurrected, and a pool waiting at the same
    level keeps its original alerted_at. Returns True when the file changed."""
    entry = f.get("entry") or {}
    if entry.get("status") != "waiting" or entry.get("level") is None:
        return False
    pair = f.get("pair") or "-"
    try:
        level = float(entry["level"])
    except (TypeError, ValueError):
        return False
    if not level > 0:
        return False
    now = int(now if now is not None else time.time())
    try:
        with open(path, encoding="utf-8") as fh:
            waiting = json.load(fh)
        waiting = waiting if isinstance(waiting, dict) else {}
    except Exception:
        waiting = {}
    old = waiting.get(pair) or {}
    if old.get("triggered"):
        return False
    if old.get("entry_level") == level and old.get("alerted_at"):
        return False
    waiting[pair] = {"symbol": f.get("symbol"), "chain": f.get("chain"),
                     "pair": pair, "token": f.get("token"),
                     "entry_level": level, "alerted_at": now, "triggered": False}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(waiting, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sinyal trade meme: kartu tetap + skor 0-100.")
    ap.add_argument("--token", help="contract address token (CA)")
    ap.add_argument("--symbol", help="simbol token (default: dari pool)")
    ap.add_argument("--chain", help="kunci ke satu chain")
    ap.add_argument("--pair", help="kunci ke satu pool (tanpa --token, alamat token dicari dari pool ini)")
    ap.add_argument("--mentions", help="file mentions JSON untuk komunitas")
    ap.add_argument("--offline", metavar="DIR", help="folder fixture, bukan network")
    ap.add_argument("--replays", type=int, default=10000)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record-waiting", metavar="PATH",
                    help="catat pool yang entry-nya WAITING ke waiting.json, supaya watch bisa re-alert saat harga menembus level entry")
    a = ap.parse_args(argv)
    if a.replays <= 0:
        print("replays harus >= 1", file=sys.stderr)
        return 1
    if not a.token and not a.pair:
        print("butuh --token (CA) atau --pair", file=sys.stderr)
        return 1
    token = a.token
    if token is None:
        try:
            token = resolve_token(a.chain, a.pair)
        except ValueError as e:
            print("sinyal gagal: %s" % e, file=sys.stderr)
            return 1
    f = build_signal(token, symbol=a.symbol, chain=a.chain, pair=a.pair,
                     mentions=a.mentions, offline=a.offline, replays=a.replays)
    if f.get("error"):
        print("sinyal gagal: %s" % f["error"], file=sys.stderr)
        return 1
    if a.record_waiting:
        record_waiting(f, a.record_waiting)
    if a.json:
        print(json.dumps(f, indent=2))
    else:
        print(render_signal(f))
        print()
        print(render_holders(f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
