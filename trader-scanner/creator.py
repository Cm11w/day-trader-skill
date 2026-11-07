#!/usr/bin/env python3
"""Creator check for a meme token: who deployed it, did they rug before?

    python3 creator.py --chain solana --token MINT
    python3 creator.py --chain robinhood --token 0x... --json

Free sources only, no keys:
  Solana  RugCheck full report -> creator + creatorTokens (other tokens
          by the same creator). Each prior token is checked for a rug:
          RugCheck `rugged`, else DexScreener dead (no pairs, or every
          pair under $100 liquidity).
  EVM     GoPlus token_security -> creator_address, creator_percent and
          honeypot_with_same_creator (1 = this creator already made a
          honeypot). GoPlus does not list the creator's other tokens, so
          the EVM verdict rests on that flag, not on a token count.

Verdict + score modifier (added to the signal total, clamped 0-100):
  RUG     creator rugged before, or GoPlus honeypot_with_same_creator=1  -> -15
  CLEAN   prior tokens checked, none rugged                              -> +5
  FIRST   no prior tokens (first launch)                                 -> 0
  UNKNOWN creator or history could not be fetched                       -> 0

Sybil limit: a rugger who launches from a FRESH address each time looks
like FIRST. That is not detectable from the creator's own history alone;
RugCheck's insider-network count (graphInsidersDetected) is reported as
context, never scored.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto  # noqa: E402  (free fetchers: RugCheck, GoPlus, DexScreener)

MAX_PRIOR_CHECKED = 5   # prior tokens actually checked for a rug
DEAD_LIQ_USD = 100.0    # a prior token whose best pair is under this is dead

VERDICTS = ("RUG", "CLEAN", "FIRST", "UNKNOWN")
MODIFIERS = {"RUG": -15, "CLEAN": 5, "FIRST": 0, "UNKNOWN": 0}


def _short(addr):
    if not addr:
        return None
    addr = str(addr)
    return addr if len(addr) <= 12 else "%s...%s" % (addr[:4], addr[-4:])


def _prior_mints(creator_tokens, self_token):
    """creatorTokens (RugCheck) -> mint addresses of the creator's OTHER
    tokens, self excluded. The entries are dicts (mint/address) or plain
    mint strings; anything unparseable is skipped, never fatal."""
    out = []
    self_lower = (self_token or "").lower()
    for item in creator_tokens or []:
        mint = None
        if isinstance(item, str):
            mint = item
        elif isinstance(item, dict):
            for key in ("mint", "address", "token", "tokenAddress", "mintAddress"):
                if item.get(key):
                    mint = item[key]
                    break
        if mint and str(mint).lower() != self_lower and str(mint) not in out:
            out.append(str(mint))
    return out


def _prior_rugged_solana(mint):
    """Is one prior Solana token rugged? RugCheck `rugged` first; when the
    report cannot be fetched, DexScreener dead (no pairs, or every pair
    under $100 liquidity). Returns True/False, never raises."""
    try:
        doc = crypto.fetch_rugcheck(mint, pause=0)
        if isinstance(doc, dict) and doc.get("mint"):
            return bool(doc.get("rugged"))
    except crypto.CryptoError:
        pass
    except Exception:
        pass
    try:
        doc = crypto.fetch_dexscreener_token(mint, pause=0)
        pairs = (doc or {}).get("pairs") or []
        if not pairs:
            return True
        best = 0.0
        for p in pairs:
            liq = ((p or {}).get("liquidity") or {}).get("usd")
            try:
                best = max(best, float(liq) if liq is not None else 0.0)
            except (TypeError, ValueError):
                pass
        return best < DEAD_LIQ_USD
    except Exception:
        return False


def _check_solana(token):
    """RugCheck creator + creatorTokens -> verdict. Raises CryptoError
    only when the main report itself cannot be fetched."""
    doc = crypto.fetch_rugcheck(token, pause=0)
    if not isinstance(doc, dict) or not doc.get("mint"):
        raise crypto.CryptoError("RugCheck has no report for %s" % token)
    creator = doc.get("creator") or None
    insiders = doc.get("graphInsidersDetected")
    if not creator:
        return {"creator": None, "prior": 0, "rugs": 0, "checked": 0,
                "verdict": "UNKNOWN", "modifier": 0, "source": "rugcheck",
                "insider_networks": insiders if isinstance(insiders, int) else None,
                "error": "RugCheck report has no creator"}
    prior = _prior_mints(doc.get("creatorTokens"), token)
    rugs = 0
    checked = 0
    for mint in prior[:MAX_PRIOR_CHECKED]:
        checked += 1
        if _prior_rugged_solana(mint):
            rugs += 1
    if not prior:
        verdict = "FIRST"
    elif rugs:
        verdict = "RUG"
    else:
        verdict = "CLEAN"
    return {"creator": creator, "prior": len(prior), "rugs": rugs,
            "checked": checked, "verdict": verdict,
            "modifier": MODIFIERS[verdict], "source": "rugcheck",
            "insider_networks": insiders if isinstance(insiders, int) else None,
            "error": None}


def _check_evm(chain, token):
    """GoPlus creator_address + honeypot_with_same_creator -> verdict.
    GoPlus does not list the creator's other tokens, so `prior` stays
    None and the verdict rests on the honeypot flag."""
    if chain not in crypto.GOPLUS_CHAINS:
        return {"creator": None, "prior": None, "rugs": None, "checked": 0,
                "verdict": "UNKNOWN", "modifier": 0, "source": "goplus",
                "insider_networks": None,
                "error": "GoPlus has no data for chain %s" % chain}
    doc = crypto.fetch_goplus_security(chain, [token], pause=0)
    rec = ((doc or {}).get("result") or {}).get((token or "").lower())
    if not isinstance(rec, dict):
        return {"creator": None, "prior": None, "rugs": None, "checked": 0,
                "verdict": "UNKNOWN", "modifier": 0, "source": "goplus",
                "insider_networks": None,
                "error": "GoPlus has no record for this token"}
    creator = rec.get("creator_address") or None
    same = rec.get("honeypot_with_same_creator")
    pct = rec.get("creator_percent")
    try:
        pct = float(pct) if pct not in (None, "") else None
    except (TypeError, ValueError):
        pct = None
    if not creator:
        return {"creator": None, "prior": None, "rugs": None, "checked": 0,
                "verdict": "UNKNOWN", "modifier": 0, "source": "goplus",
                "insider_networks": None, "creator_percent": pct,
                "error": "GoPlus record has no creator"}
    if str(same) == "1":
        verdict = "RUG"
    else:
        # A 0 flag means "no honeypot from this creator seen". That rules
        # out one rug shape, not a rug history: the creator may have
        # pulled liquidity instead, or may be brand new. No bonus without
        # a verified track record, so the verdict stays UNKNOWN (neutral).
        verdict = "UNKNOWN"
    return {"creator": creator, "prior": None, "rugs": None, "checked": 0,
            "verdict": verdict, "modifier": MODIFIERS[verdict],
            "source": "goplus", "insider_networks": None,
            "creator_percent": pct,
            "honeypot_same_creator": str(same) == "1",
            "error": None}


def check_creator(chain, token):
    """One token -> creator facts. Never raises: every failure is an
    UNKNOWN verdict with the reason in `error`."""
    chain = (chain or "").lower()
    try:
        if chain == "solana":
            out = _check_solana(token)
        else:
            out = _check_evm(chain, token)
    except crypto.CryptoError as e:
        out = {"creator": None, "prior": None, "rugs": None, "checked": 0,
               "verdict": "UNKNOWN", "modifier": 0,
               "source": "rugcheck" if chain == "solana" else "goplus",
               "insider_networks": None, "error": str(e)}
    except Exception as e:
        out = {"creator": None, "prior": None, "rugs": None, "checked": 0,
               "verdict": "UNKNOWN", "modifier": 0,
               "source": "rugcheck" if chain == "solana" else "goplus",
               "insider_networks": None, "error": "%s: %s" % (type(e).__name__, e)}
    out["creator_short"] = _short(out.get("creator"))
    return out


def creator_line(c):
    """The card line for a creator check result. The creator address is
    shown whenever it is known, even for an UNKNOWN verdict: the verdict
    is about the track record, not about whether the deployer was found."""
    c = c or {}
    if not c.get("creator"):
        why = c.get("error") or "tidak ada data kreator"
        return "Kreator: UNKNOWN - %s (0)" % why
    short = c.get("creator_short") or _short(c.get("creator")) or "?"
    mod = c.get("modifier", 0)
    mod_txt = ("+%d" % mod) if mod > 0 else str(mod)
    if c.get("source") == "goplus":
        flag = "ya" if c.get("honeypot_same_creator") else "tidak"
        return "Kreator: %s | honeypot kreator sama: %s | %s (%s)" % (
            short, flag, c.get("verdict") or "UNKNOWN", mod_txt)
    return "Kreator: %s | token lain: %d | rug: %d | %s (%s)" % (
        short, c.get("prior", 0) or 0, c.get("rugs", 0) or 0,
        c.get("verdict") or "UNKNOWN", mod_txt)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chain", required=True, help="solana, bsc, base, robinhood, ...")
    ap.add_argument("--token", required=True, help="token mint / contract address")
    ap.add_argument("--json", action="store_true", help="print the facts dict")
    a = ap.parse_args(argv)
    out = check_creator(a.chain, a.token)
    if a.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(creator_line(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
