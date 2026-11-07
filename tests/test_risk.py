import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
crypto = load("trader-scanner", "crypto")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
TSLA_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
V1_ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reel.json")
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"
V1_TODAY = "2026-10-13"
PRICES = {"AAPL": 341.27, "AXP": 309.84, "GOOGL": 353.78, "KO": 90.11, "BAC": 53.96}
QUOTE_TIME = datetime.datetime(2026, 10, 19, 14, 52, 13)   # Yahoo's TSLA quote 366.475, 10:52:13 New York
# v1's rules with every day-trade check switched off: the long-term checks still hold
V1 = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_usd": 2000, "min_order_usd": 5,
      "max_orders_per_day": 10, "require_stop": False, "no_entries_before": None, "no_entries_after": None,
      "max_trades_per_day": 0, "max_daily_loss_usd": 0, "max_loss_per_trade_usd": 0, "min_reward_risk": 0}


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def v1(**over):
    c = dict(V1)
    c.update(over)
    return c


def tsla(**over):
    with open(TSLA_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def bracket(sym, qty, entry, stop, target):
    return {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "%.2f" % stop,
            "plan": {"entry": entry, "stop": stop, "target": target}}


def v1_orders():
    with open(V1_ORDERS_PATH) as fh:
        return json.load(fh)["orders"]


def buy(sym, usd):
    return {"symbol": sym, "side": "buy", "notional": str(usd), "type": "market", "time_in_force": "day"}


def placed(sym, side="buy", day=TODAY, max_loss=46.88, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T10:20:00-04:00"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheReel(unittest.TestCase):
    def test_the_tsla_bracket_passes_every_check(self):
        res = check.check([tsla()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 364.18, 358.32, 370.04))
        self.assertEqual((r["risk_per_share"], r["max_loss"], r["reward_risk"]), (5.86, 46.88, 1.0))
        self.assertEqual(r["value_usd"], 2913.44)
        self.assertEqual(res["cash_after"], 2086.56)
        self.assertEqual(res["date"], TODAY)
        self.assertIs(res["paper"], True)

    def test_nine_shares_is_blocked_by_the_max_loss_only(self):
        res = check.check([tsla(qty="9")], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 9 shares x 5.86 risk a share = $52.74 at the stop, over your $50 max loss per trade"])

    def test_fingerprint_is_shared_with_the_broker(self):
        o = tsla()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, qty="9")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, stop_loss_stop_price="358.00")), check.fingerprint(o))
        self.assertEqual(check.ALPACA_FIELDS, ticket.ALPACA_FIELDS)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", TSLA_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy TSLA 8 shares, entry", text)
            self.assertIn("entry 364.18 (the plan's entry), stop 358.32, target 370.04: risk 5.86 a share", text)
            self.assertIn("max loss $46.88 (limit $50), reward:risk 1.00 (min 1)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-19 10:15 New York", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(tsla()))


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_a_stop_is_required(self):
        o = tsla()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price", "plan"):
            o.pop(k)
        o["plan"] = {"entry": 364.18}
        res = self.one(o)
        self.assertIn("stop: no stop", reasons(res))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = tsla()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price"):
            o.pop(k)
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 46.88)

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("must be below the entry 364.18", reasons(self.one(tsla(stop_loss_stop_price="365.00"))))
        res = self.one(tsla(take_profit_limit_price="364.00"))
        self.assertIn("target: the target 364.00 must be above the entry 364.18", reasons(res))

    def test_reward_risk(self):
        res = self.one(tsla(take_profit_limit_price="368.00"))
        self.assertIn("reward:risk: 0.65", reasons(res))
        self.assertEqual(verdicts(self.one(tsla(take_profit_limit_price="368.00"), min_reward_risk=0.5)), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        later = at("2026-10-19 10:55")
        res = check.check([tsla()], cfg(), prices={"TSLA": 366.475}, now=later, price_times={"TSLA": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (366.48, "price", 8.16))
        self.assertIn("loss: 8 shares x 8.16 risk a share = $65.28", reasons(res))

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-10-19 11:30", "2026-10-19 10:15"):
            res = check.check([tsla()], cfg(), prices={"TSLA": 366.475}, now=at(now),
                              price_times={"TSLA": QUOTE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 364.18)
            self.assertIn("older than 15 minutes", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([tsla()], cfg(), prices={"TSLA": 360.00}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 364.18)

    def test_a_limit_order_uses_its_limit_price(self):
        o = tsla(type="limit", limit_price="364.00")
        res = check.check([o], cfg(), prices={"TSLA": 366.475}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["max_loss"], r["verdict"]), (364.0, 45.44, "PASS"))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("AAPL"), placed("AMD")]}
        res = check.check([tsla()], cfg(), state=two, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, $93.76 of $150 daily loss", res["results"][0]["info"][-1])
        three = {"orders": [placed("AAPL"), placed("AMD"), placed("NVDA")]}
        res = check.check([tsla()], cfg(), state=three, now=at(NOW))
        why = reasons(res)
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: $140.64 already at risk today plus $46.88 for this one = $187.52, over your $150",
                      why)
        yesterday = {"orders": [placed(s, day="2026-10-16") for s in ("AAPL", "AMD", "NVDA")]}
        self.assertEqual(verdicts(check.check([tsla()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("AAPL"), placed("AAPL", side="sell", kind="exit", max_loss=None),
                         placed("AMD", status="declined"), placed("NVDA", status="failed")]}
        res = check.check([tsla()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [bracket(s, 1, 50.0, 10.0, 90.0) for s in ("AAA", "BBB", "CCC", "DDD")]
        res = check.check(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)
        self.assertIn("daily loss: $120 already at risk today plus $40 for this one = $160", why)

    def test_entry_window_new_york_time(self):
        cases = [("2026-10-19 09:44", "no new entries before 09:45 New York (it is 09:44)"),
                 ("2026-10-19 15:31", "no new entries after 15:30 New York (it is 15:31)"),
                 ("2026-10-19 08:00", "the market is closed (Mon 2026-10-19 08:00"),
                 ("2026-10-19 16:05", "the market is closed"),
                 ("2026-10-17 10:15", "the market is closed today (weekend"),
                 ("2026-11-26 10:15", "the market is closed today (NYSE holiday: Thanksgiving Day"),
                 ("2026-11-27 12:31", "no new entries after 12:30 New York"),
                 ("2026-11-27 13:05", "the market is closed"),
                 ("2026-11-02T14:40:00Z", "no new entries before 09:45 New York (it is 09:40)")]
        for now, want in cases:
            res = self.one(tsla(), now=now)
            self.assertIn("window: " + want, reasons(res), now)
        for now in ("2026-10-19 09:45", "2026-10-19 15:30", "2026-11-27 12:30", "2026-11-02T15:15:00Z",
                    "2026-10-30T14:40:00Z", "2026-03-09T13:50:00Z"):
            self.assertEqual(verdicts(self.one(tsla(), now=now)), ["PASS"], now)
        self.assertIn("early close", " ".join(self.one(tsla(), now="2026-11-27 12:30")["warnings"]))
        res = self.one(tsla(), now="2026-10-17 10:15", no_entries_before=None, no_entries_after=None)
        self.assertEqual(verdicts(res), ["PASS"])
        with self.assertRaises(check.RiskError):
            self.one(tsla(), no_entries_before="9.45am")

    def test_the_window_uses_new_york_time_not_the_machine(self):
        self.assertEqual(check.to_eastern(at("2026-10-19 10:15")), datetime.datetime(2026, 10, 19, 10, 15))
        self.assertEqual(at("2026-10-19 10:15"), datetime.datetime(2026, 10, 19, 14, 15))
        self.assertEqual(at("2026-12-01 10:15"), datetime.datetime(2026, 12, 1, 15, 15))

    def test_pdt_guard(self):
        def trade(day):
            return [placed("X" + day[-2:], day=day), placed("X" + day[-2:], side="sell", day=day, kind="exit",
                                                            status="filled", max_loss=None)]
        st = {"orders": trade("2026-10-13") + trade("2026-10-14") + trade("2026-10-15")}
        self.assertEqual(verdicts(self.one(tsla())), ["PASS"])
        res = check.check([tsla()], cfg(pdt_guard=True), state=st, now=at(NOW))
        self.assertIn("pdt: 3 day trades in the last 5 business days", reasons(res))
        self.assertEqual(verdicts(check.check([tsla()], cfg(), state=st, now=at(NOW))), ["PASS"])
        old = {"orders": trade("2026-10-12") + trade("2026-10-14") + trade("2026-10-15")}
        self.assertEqual(verdicts(check.check([tsla()], cfg(pdt_guard=True), state=old, now=at(NOW))), ["PASS"])
        pending = {"orders": trade("2026-10-14") + trade("2026-10-15") + [placed("AMD")]}
        res = check.check([tsla()], cfg(pdt_guard=True), state=pending, now=at(NOW))
        self.assertIn("pdt: 3 day trades", reasons(res))
        buy_only = {"orders": [placed("AMD", day="2026-10-15"), placed("NVDA", day="2026-10-16")]}
        self.assertEqual(check.day_trades(buy_only, TODAY)[0], 0)

    def test_a_short_entry_when_allowed(self):
        short = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day",
                 "order_class": "bracket", "take_profit_limit_price": "358.32", "stop_loss_stop_price": "370.04",
                 "plan": {"entry": 364.18}}
        res = self.one(short)
        self.assertIn("short: selling more TSLA than you hold", reasons(res))
        res = self.one(short, allow_short=True)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]), ("PASS", "entry", 46.88, 1.0))
        res = self.one(dict(short, stop_loss_stop_price="360.00"), allow_short=True)
        self.assertIn("must be above the entry 364.18 for a sell", reasons(res))

    def test_bracket_format(self):
        self.assertIn("whole-share qty", reasons(self.one(dict(tsla(qty=None), notional="2900"))))
        self.assertIn("whole shares only", reasons(self.one(tsla(qty="8.5"))))
        self.assertIn("time_in_force day or gtc", reasons(self.one(tsla(time_in_force="ioc"))))
        self.assertEqual(verdicts(self.one(tsla(time_in_force="gtc"))), ["PASS"])
        o = tsla()
        o.pop("take_profit_limit_price")
        self.assertIn("needs both take_profit_limit_price and stop_loss_stop_price", reasons(self.one(o)))
        self.assertIn("needs a stop_loss_stop_price", reasons(self.one(dict(tsla(), stop_loss_stop_price=None,
                                                                            stop_loss_limit_price="357.00"))))
        self.assertIn("as a string", warnings(self.one(tsla(stop_loss_stop_price=358.32))))

    def test_extended_hours(self):
        lim = {"symbol": "KO", "side": "buy", "qty": "1", "type": "limit", "limit_price": "90",
               "time_in_force": "gtc", "extended_hours": True}
        res = check.check([lim], v1(), prices=PRICES, today=V1_TODAY)
        self.assertEqual(verdicts(res), ["PASS"])
        res = check.check([dict(lim, type="market", limit_price=None)], v1(), prices=PRICES, today=V1_TODAY)
        self.assertIn("extended hours needs a limit order", reasons(res))


class Exits(unittest.TestCase):
    PF = {"cash": 2086.56, "positions": [{"ticker": "TSLA", "shares": 8,
                                           "lots": [{"date": TODAY, "shares": 8, "cost": 2913.44}]}]}
    SELL = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("TSLA"), placed("AMD"), placed("NVDA")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("TSLA", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["TSLA"], max_order_usd=100, max_daily_loss_usd=50)
        res = check.check([self.SELL], c, portfolio=self.PF, state=full, now=at("2026-10-19 15:58"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        self.assertNotIn("price", why)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-10-17 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty="4")
        res = check.check([half, half], cfg(), portfolio=self.PF, prices={"TSLA": 366.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, prices={"TSLA": 366.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        res = check.check([dict(self.SELL, type="limit")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("needs a limit_price", reasons(res))
        res = check.check([dict(self.SELL, qty="9")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more TSLA than you hold (8 shares)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([tsla(), self.SELL], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class V1Checks(unittest.TestCase):
    """v1's long-term checks still hold (day-trade checks off)."""

    def one(self, order, **over):
        return check.check([order], v1(**over), prices=PRICES, today=V1_TODAY)["results"][0]

    def test_the_five_buys_pass_on_a_fresh_5000_paper_account(self):
        res = check.check(v1_orders(), v1(), today=V1_TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5)
        self.assertEqual(res["cash_after"], 500)
        self.assertEqual(res["results"][0]["value_usd"], 1250)
        self.assertEqual(res["results"][0]["amount"], "$1,250")

    def test_a_sixth_buy_is_blocked_three_ways(self):
        res = check.check(v1_orders() + [buy("AAPL", 100)], v1(), today=V1_TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5 + ["BLOCK"])
        why = reasons(res, 5)
        self.assertIn("cash:", why)
        self.assertIn("position:", why)
        self.assertIn("duplicate:", why)

    def test_the_v2_config_blocks_buys_without_a_stop(self):
        res = check.check(v1_orders(), cfg(), now=at(NOW))
        self.assertEqual(verdicts(res)[0], "BLOCK")
        self.assertIn("stop: no stop", reasons(res))

    def test_max_and_min_order(self):
        self.assertIn("over your $2,000 max order", " ".join(self.one(buy("KO", 2500), max_position_pct=100)["reasons"]))
        self.assertIn("under your $5 minimum", " ".join(self.one(buy("KO", 2))["reasons"]))

    def test_blocked_list(self):
        r = self.one(buy("KO", 100), blocked=["KO"])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked list", r["reasons"][0])

    def test_options_off_unless_allowed(self):
        opt = {"symbol": "BE270617C00100000", "side": "buy", "qty": "1", "type": "limit",
               "limit_price": "12.50", "time_in_force": "day"}
        r = self.one(opt)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("option", " ".join(r["reasons"]))
        r = self.one(opt, allow_options=True)
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(r["warnings"])

    def test_crypto_and_junk_symbols(self):
        crypto = {"symbol": "BTC/USD", "side": "buy", "notional": "50", "type": "market", "time_in_force": "day"}
        self.assertIn("stocks and ETFs only", " ".join(self.one(crypto)["reasons"]))

    def test_format_rules(self):
        both = dict(buy("KO", 100), qty="1")
        self.assertIn("exactly one of qty or notional", " ".join(self.one(both)["reasons"]))
        lim = dict(buy("KO", 100), type="limit", limit_price="90")
        self.assertIn("market order with time_in_force day", " ".join(self.one(lim)["reasons"]))
        nolimit = {"symbol": "KO", "side": "buy", "qty": "1", "type": "limit", "time_in_force": "day"}
        self.assertIn("needs a limit_price", " ".join(self.one(nolimit)["reasons"]))
        bad = {"symbol": "KO", "side": "hold", "notional": "100"}
        self.assertIn("side must be buy or sell", " ".join(self.one(bad)["reasons"]))

    def test_numbers_should_be_strings(self):
        r = self.one({"symbol": "KO", "side": "buy", "notional": 100, "type": "market", "time_in_force": "day"})
        self.assertEqual(r["verdict"], "PASS")
        self.assertIn("as a string", r["warnings"][0])

    def test_share_orders_need_a_price(self):
        q = {"symbol": "ZZZZ", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day"}
        self.assertIn("no price for ZZZZ", " ".join(self.one(q)["reasons"]))
        q["symbol"] = "KO"
        r = self.one(q)
        self.assertEqual(r["verdict"], "PASS")
        self.assertAlmostEqual(r["value_usd"], 180.22)

    def test_margin(self):
        r = check.check([buy("KO", 1500)], v1(cash_buffer_pct=0, max_position_pct=100),
                        portfolio={"cash": 1000, "positions": []}, today=V1_TODAY)["results"][0]
        self.assertIn("margin", " ".join(r["reasons"]))

    PF = {"cash": 500.0, "positions": [{"ticker": "KO", "shares": 7.8571,
                                         "lots": [{"date": "2026-10-13", "shares": 7.8571, "cost": 708.0}]}]}

    def test_short_sales_are_blocked(self):
        sell = {"symbol": "KO", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day"}
        r = check.check([sell], v1(), portfolio=self.PF, prices=PRICES, today=V1_TODAY)["results"][0]
        self.assertIn("short:", " ".join(r["reasons"]))
        sell["qty"] = "3"
        r = check.check([sell], v1(), portfolio=self.PF, prices=PRICES, today=V1_TODAY)["results"][0]
        self.assertEqual(r["verdict"], "PASS")

    def test_a_sell_frees_cash_for_a_later_buy(self):
        orders = [{"symbol": "KO", "side": "sell", "qty": "5", "type": "market", "time_in_force": "day"},
                  buy("BAC", 450)]
        res = check.check(orders, v1(max_position_pct=100), portfolio=self.PF, prices=PRICES, today=V1_TODAY)
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        res = check.check([buy("BAC", 450)], v1(max_position_pct=100), portfolio=self.PF, prices=PRICES,
                          today=V1_TODAY)
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_daily_limit_and_already_placed_today(self):
        st = {"orders": [{"symbol": "T%d" % i, "side": "buy", "status": "placed", "at": V1_TODAY + "T10:00:00"}
                         for i in range(10)]}
        res = check.check([buy("KO", 100)], v1(), state=st, today=V1_TODAY)
        self.assertIn("daily limit", reasons(res))
        res = check.check([buy("KO", 100)], v1(), state=st, today="2026-10-14")
        self.assertEqual(verdicts(res), ["PASS"])
        ko = {"orders": [{"symbol": "KO", "side": "buy", "status": "placed", "at": V1_TODAY + "T10:00:00"}]}
        res = check.check([buy("KO", 100)], v1(), state=ko, today=V1_TODAY)
        self.assertIn("already placed today", reasons(res))
        declined = {"orders": [{"symbol": "KO", "side": "buy", "status": "declined", "at": V1_TODAY + "T10:00:00"}]}
        self.assertEqual(verdicts(check.check([buy("KO", 100)], v1(), state=declined, today=V1_TODAY)), ["PASS"])

    def test_live_account_warns(self):
        res = check.check([buy("KO", 100)], v1(paper=False), today=V1_TODAY)
        self.assertIn("LIVE", res["warnings"][0])


class Cli(unittest.TestCase):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-19T14:55:00Z", "prices": {"TSLA": 366.475},
                           "detail": {"TSLA": {"time": "2026-10-19T14:52:13Z", "fetched": "2026-10-19T14:55:00Z"}}},
                          fh)
            none = os.path.join(d, "none")
            base = ["--orders", TSLA_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-10-19 10:55"]), 1)
            self.assertIn("8 shares x 8.16 risk a share = $65.28", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-10-19 11:30"]), 0)

    def test_v1_orders_with_a_v1_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(v1(), fh)
            none = os.path.join(d, "none")
            out = os.path.join(d, "state", "risk.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", V1_ORDERS_PATH, "--config", c, "--portfolio", none, "--prices", none,
                                   "--state", none, "--today", V1_TODAY, "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("5 passed, 0 blocked", buf.getvalue())
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], V1_TODAY)
            self.assertTrue(all(len(r["fingerprint"]) == 16 for r in rec["results"]))
            bad = os.path.join(d, "orders.json")
            with open(bad, "w") as fh:
                json.dump({"orders": v1_orders() + [buy("AAPL", 100)]}, fh)
            with redirect_stdout(io.StringIO()):
                code = check.main(["--orders", bad, "--config", c, "--portfolio", none, "--prices", none,
                                   "--state", none, "--today", V1_TODAY, "--no-write"])
            self.assertEqual(code, 1)

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", TSLA_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", TSLA_PATH, "--today", "monday", "--no-write"]), 1)
        self.assertIn("--today wants", err.getvalue())


RISK_FIX = os.path.join(FIX, "trader-risk")
SOL_DIR = os.path.join(RISK_FIX, "crypto-solana")
BASE_DIR = os.path.join(RISK_FIX, "crypto-base")
MOON_PAIR = "8GjMy6ipYsXfMWwRPu9C7ATssorjg6AL88d9xtTZsLqe"
USDC_ADDR = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_PAIR = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
EVM_REASON = "no GoPlus security record for this token"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
GP_DIR = os.path.join(FIX, "crypto", "goplus")


def solana_security():
    """The live BONK security record (GoPlus Solana + RugCheck + Jupiter),
    merged the way the risk check does it."""
    goplus = crypto.parse_goplus_solana(load_json(os.path.join(GP_DIR, "solana_bonk.json")),
                                        BONK_MINT)
    rugcheck = crypto.parse_rugcheck(load_json(os.path.join(GP_DIR, "rugcheck_bonk.json")))
    jupiter = crypto.parse_jupiter_quote(load_json(os.path.join(GP_DIR, "jupiter_bonk.json")))
    return crypto.merge_solana_security(goplus, rugcheck, jupiter)


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def moon_pool():
    doc = load_json(os.path.join(SOL_DIR, "dexscreener-pairs.json"))
    return crypto.parse_dexscreener_pairs(doc)[0]


def usdc_security():
    return crypto.parse_goplus_security(load_json(os.path.join(BASE_DIR, "goplus.json")), USDC_ADDR)


def check_of(res, name):
    for c in res["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError("no check named %s in %s" % (name, res["checks"]))


def run_risk(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = check.main(argv)
    return code, out.getvalue(), err.getvalue()


class CryptoMemeCheck(unittest.TestCase):
    def test_a_good_pool_with_clean_security_passes(self):
        res = check.check_crypto_meme(moon_pool(), usdc_security())
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["unknowns"], [])
        self.assertTrue(all(c["pass"] is True for c in res["checks"]), res["checks"])
        self.assertEqual([c["name"] for c in res["checks"]],
                         ["liquidity", "volume24h", "market_cap", "top10", "honeypot",
                          "buy_tax", "sell_tax", "cannot_buy", "is_proxy"])

    def test_low_liquidity_fails(self):
        pool = dict(moon_pool(), liquidity_usd=9000.0)
        res = check.check_crypto_meme(pool, usdc_security())
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "liquidity")["pass"], False)
        self.assertIn("10000", check_of(res, "liquidity")["detail"])

    def test_low_volume_and_zero_market_cap_fail(self):
        res = check.check_crypto_meme(dict(moon_pool(), volume24h_usd=100.0), usdc_security())
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "volume24h")["pass"], False)
        res = check.check_crypto_meme(dict(moon_pool(), market_cap_usd=0.0), usdc_security())
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "market_cap")["pass"], False)

    def test_top10_over_half_fails(self):
        sec = dict(usdc_security(), top10_percent=0.62)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "top10")["pass"], False)

    def test_missing_holder_data_is_unknown_not_a_fail(self):
        sec = dict(usdc_security(), top10_percent=None, top_holders=[])
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "unknown")
        self.assertIsNone(check_of(res, "top10")["pass"])
        self.assertTrue(any("top10" in u for u in res["unknowns"]))

    def test_a_honeypot_fails_and_high_tax_fails(self):
        sec = dict(usdc_security(), is_honeypot=True)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "honeypot")["pass"], False)
        sec = dict(usdc_security(), buy_tax=10.0)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertIs(check_of(res, "buy_tax")["pass"], False)
        sec = dict(usdc_security(), sell_tax=6.0)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertIs(check_of(res, "sell_tax")["pass"], False)

    def test_cannot_buy_fails(self):
        sec = dict(usdc_security(), cannot_buy=True)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "cannot_buy")["pass"], False)

    def test_a_proxy_is_a_passing_warning_not_a_failure(self):
        sec = dict(usdc_security(), is_proxy=True)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "pass")
        c = check_of(res, "is_proxy")
        self.assertIs(c["pass"], True)
        self.assertEqual(c["detail"], "proxy contract: upgradeable")

    def test_no_security_is_unknown_never_a_pass(self):
        res = check.check_crypto_meme(moon_pool(), None)
        self.assertEqual(res["verdict"], "unknown")
        self.assertIsNone(check_of(res, "honeypot")["pass"])
        self.assertIsNone(check_of(res, "buy_tax")["pass"])
        self.assertIsNone(check_of(res, "top10")["pass"])
        self.assertTrue(res["unknowns"])
        res = check.check_crypto_meme(moon_pool(), None,
                                      {"security_unavailable_reason": EVM_REASON})
        self.assertIn(EVM_REASON, " | ".join(res["unknowns"]))

    def test_a_missing_tax_is_unknown(self):
        sec = dict(usdc_security(), sell_tax=None)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "unknown")
        self.assertIsNone(check_of(res, "sell_tax")["pass"])

    def test_thresholds_come_from_the_config(self):
        res = check.check_crypto_meme(moon_pool(), usdc_security(),
                                      {"min_liquidity_usd": 60000.0})
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "liquidity")["pass"], False)

    def test_unknown_market_cap_is_unknown_never_a_pass_on_the_fdv(self):
        pool = dict(moon_pool(), market_cap_usd=0.0, market_cap_known=False, fdv_usd=750000.0)
        res = check.check_crypto_meme(pool, usdc_security())
        self.assertEqual(res["verdict"], "unknown")
        self.assertIsNone(check_of(res, "market_cap")["pass"])
        self.assertIn("unknown", check_of(res, "market_cap")["detail"])
        self.assertTrue(any("market_cap" in u for u in res["unknowns"]))

    def test_tiny_quantities_do_not_print_as_zero(self):
        self.assertEqual(check._crypto_qty(0.0003972), "0.0003972")
        self.assertEqual(check._crypto_qty(0.5), "0.50")
        self.assertEqual(check._crypto_qty(50), "50")
        self.assertEqual(check._crypto_qty(0.017902791), "0.02")

    def test_a_clean_solana_token_passes_with_its_own_checks(self):
        res = check.check_crypto_meme(moon_pool(), solana_security())
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["unknowns"], [])
        self.assertEqual([c["name"] for c in res["checks"]],
                         ["liquidity", "volume24h", "market_cap", "top10", "honeypot",
                          "buy_tax", "sell_tax", "freeze_authority", "mint_authority",
                          "rugcheck"])
        self.assertIs(check_of(res, "freeze_authority")["pass"], True)
        self.assertIs(check_of(res, "mint_authority")["pass"], True)
        self.assertIs(check_of(res, "rugcheck")["pass"], True)
        self.assertIn("7", check_of(res, "rugcheck")["detail"])

    def test_solana_skips_the_checks_that_have_no_solana_meaning(self):
        res = check.check_crypto_meme(moon_pool(), solana_security())
        names = [c["name"] for c in res["checks"]]
        self.assertNotIn("cannot_buy", names)
        self.assertNotIn("is_proxy", names)

    def test_a_set_freeze_authority_fails_on_solana(self):
        sec = dict(solana_security(), freezable=True)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "freeze_authority")["pass"], False)
        self.assertIn("freeze", check_of(res, "freeze_authority")["detail"])

    def test_a_set_mint_authority_fails_on_solana(self):
        sec = dict(solana_security(), mintable=True)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "mint_authority")["pass"], False)

    def test_a_rugcheck_danger_fails_on_solana(self):
        sec = dict(solana_security(), rugcheck_danger=["Freeze Authority enabled"])
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "rugcheck")["pass"], False)
        self.assertIn("Freeze Authority enabled", check_of(res, "rugcheck")["detail"])

    def test_an_unsellable_solana_token_is_a_honeypot(self):
        sec = dict(solana_security(), is_honeypot=True)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "honeypot")["pass"], False)

    def test_a_solana_transfer_fee_over_5_percent_fails(self):
        sec = dict(solana_security(), buy_tax=6.0, sell_tax=6.0)
        res = check.check_crypto_meme(moon_pool(), sec)
        self.assertEqual(res["verdict"], "fail")
        self.assertIs(check_of(res, "sell_tax")["pass"], False)


class CryptoCli(unittest.TestCase):
    def test_solana_offline_passes_with_solana_security(self):
        code, text, err = run_risk(["crypto", "--chain", "solana", "--pair", MOON_PAIR,
                                    "--offline", SOL_DIR, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["chain"], "solana")
        self.assertEqual(doc["pool"]["symbol"], "MOON")
        self.assertEqual(doc["verdict"], "pass")
        self.assertEqual(doc["security"]["chain"], "solana")
        self.assertIs(doc["security"]["is_honeypot"], False)
        self.assertIs(doc["security"]["freezable"], False)
        self.assertEqual(doc["security"]["rugcheck_score"], 7)
        self.assertEqual(check_of(doc, "freeze_authority")["pass"], True)
        self.assertEqual(check_of(doc, "rugcheck")["pass"], True)

    def test_solana_offline_without_fixtures_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            pairs = load_json(os.path.join(SOL_DIR, "dexscreener-pairs.json"))
            with open(os.path.join(d, "dexscreener-pairs.json"), "w") as fh:
                json.dump(pairs, fh)
            code, text, err = run_risk(["crypto", "--chain", "solana", "--pair", MOON_PAIR,
                                        "--offline", d, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["verdict"], "unknown")
        self.assertIsNone(doc["security"])
        # No fixture file at all: the reason names the missing files.
        self.assertIn("no Solana security fixture in", " | ".join(doc["unknowns"]))

    def test_base_offline_passes_with_goplus_data(self):
        code, text, err = run_risk(["crypto", "--chain", "base", "--pair", BASE_PAIR,
                                    "--token", USDC_ADDR, "--offline", BASE_DIR, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["verdict"], "pass")
        self.assertIs(doc["security"]["is_honeypot"], False)
        self.assertEqual(check_of(doc, "is_proxy")["detail"], "proxy contract: upgradeable")

    def test_text_mode_reports_the_verdict(self):
        code, text, err = run_risk(["crypto", "--chain", "solana", "--pair", MOON_PAIR,
                                    "--offline", SOL_DIR])
        self.assertEqual(code, 0, err)
        self.assertIn("PASS", text)
        self.assertIn("freeze_authority", text)
        self.assertIn("rugcheck", text)
        self.assertIn("Verdict: PASS", text)
        self.assertIn("not financial advice", text)

    def test_a_thin_pool_fails_offline(self):
        with tempfile.TemporaryDirectory() as d:
            thin = {"pairs": [{"baseToken": {"address": "THINTOKEN", "name": "Thin", "symbol": "THIN"},
                               "chainId": "solana", "dexId": "pumpswap", "pairAddress": "THINPAIR",
                               "priceUsd": "0.001", "marketCap": 1000, "liquidity": {"usd": 100.0},
                               "volume": {"h24": 50.0}}]}
            with open(os.path.join(d, "dexscreener-pairs.json"), "w") as fh:
                json.dump(thin, fh)
            code, text, err = run_risk(["crypto", "--chain", "solana", "--pair", "THINPAIR",
                                        "--offline", d, "--json"])
            self.assertEqual(code, 1, err)
            self.assertEqual(json.loads(text)["verdict"], "fail")

    def test_unknown_chain_and_unknown_pair_fail(self):
        code, _, err = run_risk(["crypto", "--chain", "nosuchchain", "--pair", MOON_PAIR,
                                 "--offline", SOL_DIR, "--json"])
        self.assertEqual(code, 1)
        self.assertIn("nosuchchain", err)
        code, _, err = run_risk(["crypto", "--chain", "solana", "--pair", "NOPAIR",
                                 "--offline", SOL_DIR, "--json"])
        self.assertEqual(code, 1)
        self.assertIn("NOPAIR", err)

    def test_unmatched_pair_falls_back_to_the_most_liquid_pool_for_the_token(self):
        moon_token = "8UQdy5sHMVWUWSHeA5B3ZuuAVNumjSLPXdf19MXcpump"
        code, text, err = run_risk(["crypto", "--chain", "solana", "--pair", "NOPAIR",
                                    "--token", moon_token, "--offline", SOL_DIR, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["pair"], MOON_PAIR)   # the 50000-liquidity pool, not OTHERPAIR
        self.assertEqual(doc["pool"]["symbol"], "MOON")
        self.assertIn("most liquid", doc["pool_note"])
        self.assertIn("NOPAIR", doc["pool_note"])

    def test_lowercase_evm_address_matches_the_same_pool(self):
        # Live: DexScreener returns a checksummed pair address while the
        # caller passes lowercase. That is the same pool, not a fallback.
        with tempfile.TemporaryDirectory() as d:
            doc = {"pairs": [{"baseToken": {"address": USDC_ADDR, "name": "USD Coin",
                                            "symbol": "USDC"},
                              "chainId": "base", "dexId": "uniswap-v3",
                              "pairAddress": "0x000000000000000000000000000000000000BEEF",
                              "priceUsd": "1.0", "marketCap": 1000000,
                              "liquidity": {"usd": 50000.0}, "volume": {"h24": 10000.0}}]}
            with open(os.path.join(d, "dexscreener-pairs.json"), "w") as fh:
                json.dump(doc, fh)
            shutil.copy(os.path.join(BASE_DIR, "goplus.json"),
                        os.path.join(d, "goplus.json"))
            code, text, err = run_risk(["crypto", "--chain", "base", "--pair",
                                        "0x000000000000000000000000000000000000beef",
                                        "--token", USDC_ADDR, "--offline", d, "--json"])
            self.assertEqual(code, 0, err)
            doc = json.loads(text)
            self.assertEqual(doc["pair"], "0x000000000000000000000000000000000000BEEF")
            self.assertIsNone(doc["pool_note"])  # same pool, no fallback note

    def test_goplus_failure_degrades_to_unknown_not_exit_1(self):
        real = crypto

        class Stub(object):
            DEX_TO_GT = real.DEX_TO_GT
            GOPLUS_CHAINS = real.GOPLUS_CHAINS
            DS_BASE = real.DS_BASE
            CryptoError = real.CryptoError
            fetch_json = staticmethod(real.fetch_json)
            parse_dexscreener_pairs = staticmethod(real.parse_dexscreener_pairs)
            parse_goplus_security = staticmethod(real.parse_goplus_security)

            def fetch_dexscreener_token(self, address, pause=0.3):
                return load_json(os.path.join(BASE_DIR, "dexscreener-pairs.json"))

            def fetch_goplus_security(self, chain, addresses, pause=0.3):
                raise real.CryptoError("GoPlus answered HTTP 429")

        orig = check._crypto_module
        check._crypto_module = lambda: Stub()
        try:
            code, text, err = run_risk(["crypto", "--chain", "base", "--pair", BASE_PAIR,
                                        "--token", USDC_ADDR, "--json"])
        finally:
            check._crypto_module = orig
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["verdict"], "unknown")
        self.assertIsNone(doc["security"])
        self.assertIn("security sources did not answer (GoPlus answered HTTP 429)",
                      " | ".join(doc["unknowns"]))


if __name__ == "__main__":
    unittest.main()
