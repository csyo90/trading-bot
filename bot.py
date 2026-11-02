#!/usr/bin/env python3
"""Trading bot — decision engine (Linux port of run-bot.ps1). Stdlib only.

Rules (idempotent, safe to run repeatedly):
  1. PFE position exists:
     - price at/above target (+8%) -> sell now (cancel protective order first if held).
     - otherwise ensure ONE bracket order exists: GTC stop-sell (-5%) + take_profit_price (+8%).
       (Alpaca holds a share against a second sell order, so stop+target must be one order.)
  2. PFE entry order open, no position, price > limit*1.02 -> cancel stale entry.
  3. No KO position, no KO order, cash >= $15 -> DAY limit buy 0.15 KO at price*1.005.

A failure inside any single rule is logged and does NOT fail the run (exit 1 is
reserved for fatal errors like bad credentials or unreachable API).

Run:  python3 bot.py            (real orders)
      python3 bot.py --dry-run  (log intended actions, place nothing)
Env:  ALPACA_BASE, ALPACA_API_KEY, ALPACA_SECRET_KEY
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("ALPACA_BASE", "https://paper-api.alpaca.markets/v2")
DATA = os.environ.get("ALPACA_DATA", "https://data.alpaca.markets/v2")
KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
DRY = "--dry-run" in sys.argv
REPO = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(REPO, "bot.log")
CSV = os.path.join(REPO, "trades.csv")


def log(msg):
    line = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line)


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SECRET)
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {e.read().decode()}")


def get_price(sym):
    url = f"{DATA}/stocks/trades/latest?symbols={sym}"
    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", KEY)
    req.add_header("APCA-API-SECRET-KEY", SECRET)
    try:
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read().decode())
        return float(d["trades"][sym]["p"])
    except (urllib.error.URLError, KeyError, IndexError):
        url = f"{DATA}/stocks/quotes/latest?symbols={sym}"
        req = urllib.request.Request(url)
        req.add_header("APCA-API-KEY-ID", KEY)
        req.add_header("APCA-API-SECRET-KEY", SECRET)
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read().decode())
        return float(d["quotes"][sym]["c"])


def submit(sym, side, qty, otype, tif, limit=None, stop=None, take_profit=None):
    body = {"symbol": sym, "qty": str(qty), "side": side, "type": otype, "time_in_force": tif}
    if limit is not None:
        body["limit_price"] = str(limit)
    if stop is not None:
        body["stop_price"] = str(stop)
    if take_profit is not None:
        body["take_profit_price"] = str(take_profit)
    if DRY:
        log(f"DRY-RUN would PLACE {side} {qty} {sym} {otype} limit={limit} stop={stop} take_profit={take_profit}")
        return {"id": "dry-run"}
    return api("/orders", "POST", body)


def cancel(oid):
    if DRY:
        log(f"DRY-RUN would CANCEL order {oid}")
        return
    api(f"/orders/{oid}", "DELETE")


def trade_csv(sym, side, qty, price, oid, stop, target, note):
    with open(CSV, "a") as f:
        f.write(f"{datetime.date.today().isoformat()},{sym},{side},{qty},{price},"
                f"{oid},{stop or ''},{target or ''},bot-placed,{note}\n")


def main():
    if not KEY or not SECRET:
        raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    log("=== Bot run start ===" + (" (DRY-RUN)" if DRY else ""))
    acct = api("/account") or {}
    pos = api("/positions") or []
    open_orders = api("/orders?status=open") or []
    cash = float(acct.get("cash") or 0)
    log(f"cash={cash} equity={acct.get('equity')} positions={len(pos)} open_orders={len(open_orders)}")
    for p in pos:
        log(f"POSITION: {p['symbol']} qty={p['qty']} entry={p['avg_entry_price']} "
            f"now={p.get('current_price')} upnl={p.get('unrealized_pl')}")
    for o in open_orders:
        log(f"OPEN_ORDER: {o['side']} {o['symbol']} qty={o['qty']} type={o['type']} "
            f"limit={o.get('limit_price')} stop={o.get('stop_price')} id={o['id']}")

    # --- Rule 1: PFE filled -> sell at target, else ensure protective bracket exists
    pfe = next((p for p in pos if p["symbol"] == "PFE"), None)
    if pfe:
        try:
            qty = float(pfe["qty"])
            entry = float(pfe["avg_entry_price"])
            stop = round(entry * 0.95, 2)
            target = round(entry * 1.08, 2)
            pfe_sells = [o for o in open_orders if o["symbol"] == "PFE" and o["side"] == "sell"]
            px = get_price("PFE")
            if px >= target:
                # Target hit: sell now. A protective order holds the share, so
                # cancel it first, then market-sell (DAY TIF for safety).
                for o in pfe_sells:
                    cancel(o["id"])
                    log(f"CANCELED PFE protective order {o['id']} before target sell")
                o = submit("PFE", "sell", qty, "market", "day")
                log(f"PFE at {px} >= target {target} -> SELLING qty={qty} order={o['id']}")
                trade_csv("PFE", "sell", qty, px, o["id"], stop, target, "take profit hit (bot)")
            else:
                if pfe_sells:
                    log(f"PFE at {px} < target {target}; already protected (stop={stop})")
                else:
                    # ONE bracket order: Alpaca rejects a second sell order on the
                    # same held share (40310000), so stop + take_profit go together.
                    o = submit("PFE", "sell", qty, "stop", "gtc", stop=stop, take_profit=target)
                    log(f"PLACED PFE bracket-sell qty={qty} stop={stop} target={target} order={o['id']}")
                    trade_csv("PFE", "sell", qty, stop, o["id"], stop, target, "protective stop+target (bot)")
        except Exception as e:  # noqa: BLE001
            log(f"RULE-1 ERROR (continuing): {e}")

    # --- Rule 2: stale PFE entry (price ran away) -> cancel
    entry = next((o for o in open_orders
                  if o["symbol"] == "PFE" and o["side"] == "buy" and o["type"] == "limit"), None)
    if entry and not pfe:
        try:
            px = get_price("PFE")
            lim = float(entry["limit_price"])
            if px > lim * 1.02:
                cancel(entry["id"])
                log(f"PFE at {px} > limit+2% ({lim}) -> CANCELED stale entry {entry['id']}")
            else:
                log(f"PFE at {px}, entry order still valid (limit {lim})")
        except Exception as e:  # noqa: BLE001
            log(f"RULE-2 ERROR (continuing): {e}")

    # --- Rule 3: KO momentum add (fractional, DAY order, bounded)
    ko_pos = next((p for p in pos if p["symbol"] == "KO"), None)
    ko_ord = next((o for o in open_orders if o["symbol"] == "KO"), None)
    if not ko_pos and not ko_ord and cash >= 15:
        try:
            px = get_price("KO")
            qty = 0.15
            limit = round(px * 1.005, 2)
            if cash >= limit * qty:
                o = submit("KO", "buy", qty, "limit", "day", limit=limit)
                log(f"PLACED KO day limit buy qty={qty} @ {limit} order={o['id']}")
                trade_csv("KO", "buy", qty, limit, o["id"], "", "", "momentum add (bot, DAY order)")
            else:
                log("KO skipped: insufficient cash after sizing")
        except Exception as e:  # noqa: BLE001
            log(f"RULE-3 ERROR (continuing): {e}")

    log("=== Bot run complete ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
        sys.exit(1)
