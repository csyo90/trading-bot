#!/usr/bin/env python3
"""Trading bot — decision engine (Linux port of run-bot.ps1). Stdlib only.

Rules (idempotent, safe to run repeatedly):
  1. PFE position exists and protective orders missing -> GTC stop-sell (-5%) + limit-sell (+8%).
  2. PFE entry order open, no position, price > limit*1.02 -> cancel stale entry.
  3. No KO position, no KO order, cash >= $15 -> DAY limit buy 0.15 KO at price*1.005.

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


def submit(sym, side, qty, otype, tif, limit=None, stop=None):
    body = {"symbol": sym, "qty": str(qty), "side": side, "type": otype, "time_in_force": tif}
    if limit is not None:
        body["limit_price"] = str(limit)
    if stop is not None:
        body["stop_price"] = str(stop)
    if DRY:
        log(f"DRY-RUN would PLACE {side} {qty} {sym} {otype} limit={limit} stop={stop}")
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

    # --- Rule 1: PFE filled -> ensure protective stop + take-profit exist
    pfe = next((p for p in pos if p["symbol"] == "PFE"), None)
    if pfe:
        qty = float(pfe["qty"])
        entry = float(pfe["avg_entry_price"])
        stop = round(entry * 0.95, 2)
        target = round(entry * 1.08, 2)
        has_stop = any(o["symbol"] == "PFE" and o["side"] == "sell" and o["type"] == "stop" for o in open_orders)
        has_target = any(o["symbol"] == "PFE" and o["side"] == "sell" and o["type"] == "limit" for o in open_orders)
        if not has_stop:
            o = submit("PFE", "sell", qty, "stop", "gtc", stop=stop)
            log(f"PLACED PFE stop-sell qty={qty} stop={stop} order={o['id']}")
            trade_csv("PFE", "sell", qty, stop, o["id"], stop, target, "protective stop (bot)")
        if not has_target:
            o = submit("PFE", "sell", qty, "limit", "gtc", limit=target)
            log(f"PLACED PFE limit-sell qty={qty} target={target} order={o['id']}")
            trade_csv("PFE", "sell", qty, target, o["id"], stop, target, "take profit (bot)")
        if has_stop and has_target:
            log(f"PFE already protected: stop={stop} target={target}")

    # --- Rule 2: stale PFE entry (price ran away) -> cancel
    entry = next((o for o in open_orders
                  if o["symbol"] == "PFE" and o["side"] == "buy" and o["type"] == "limit"), None)
    if entry and not pfe:
        px = get_price("PFE")
        lim = float(entry["limit_price"])
        if px > lim * 1.02:
            cancel(entry["id"])
            log(f"PFE at {px} > limit+2% ({lim}) -> CANCELED stale entry {entry['id']}")
        else:
            log(f"PFE at {px}, entry order still valid (limit {lim})")

    # --- Rule 3: KO momentum add (fractional, DAY order, bounded)
    ko_pos = next((p for p in pos if p["symbol"] == "KO"), None)
    ko_ord = next((o for o in open_orders if o["symbol"] == "KO"), None)
    if not ko_pos and not ko_ord and cash >= 15:
        px = get_price("KO")
        qty = 0.15
        limit = round(px * 1.005, 2)
        if cash >= limit * qty:
            o = submit("KO", "buy", qty, "limit", "day", limit=limit)
            log(f"PLACED KO day limit buy qty={qty} @ {limit} order={o['id']}")
            trade_csv("KO", "buy", qty, limit, o["id"], "", "", "momentum add (bot, DAY order)")
        else:
            log("KO skipped: insufficient cash after sizing")

    log("=== Bot run complete ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
        sys.exit(1)
