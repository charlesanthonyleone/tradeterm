"""tradeterm: 100% automated PAPER trading. No real money, no keys."""
import json, sqlite3, time, math, sys, os, subprocess, datetime as dt, traceback
import requests
ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
DB = os.path.join(ROOT, "tradeterm.db")
S = requests.Session(); S.headers["User-Agent"] = "tradeterm-paper/1.0"
KR = {"BTC-USD": "XBTUSD", "ETH-USD": "ETHUSD", "SOL-USD": "SOLUSD", "DOGE-USD": "XDGUSD"}

# ---------- market data (Coinbase public, Kraken fallback) ----------
def candles(pair, gran, start=None, end=None):
    p = {"granularity": gran}
    if start: p.update(start=start.isoformat(), end=end.isoformat())
    try:
        r = S.get(f"https://api.exchange.coinbase.com/products/{pair}/candles", params=p, timeout=10); r.raise_for_status()
        return sorted([(int(c[0]), c[3], c[2], c[1], c[4]) for c in r.json()])  # t,o,h,l,c
    except Exception:
        r = S.get("https://api.kraken.com/0/public/OHLC", params={"pair": KR[pair], "interval": gran // 60}, timeout=10)
        res = r.json()["result"]; k = [x for x in res if x != "last"][0]
        return [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in res[k]]

def ticker(pair):
    try:
        j = S.get(f"https://api.exchange.coinbase.com/products/{pair}/ticker", timeout=10).json()
        return float(j["bid"]), float(j["ask"]), "coinbase"
    except Exception:
        j = S.get("https://api.kraken.com/0/public/Ticker", params={"pair": KR[pair]}, timeout=10).json()["result"]
        v = list(j.values())[0]; return float(v["b"][0]), float(v["a"][0]), "kraken"

_mins = {}
def min_size(pair):
    if pair not in _mins:
        try: j = S.get(f"https://api.exchange.coinbase.com/products/{pair}", timeout=10).json(); _mins[pair] = (float(j.get("base_increment", 1e-8)), float(j.get("min_market_funds", CFG["min_order_usd"])))
        except Exception: _mins[pair] = (1e-8, CFG["min_order_usd"])
    return _mins[pair]

# ---------- indicators ----------
def ema(xs, n):
    k = 2 / (n + 1); out = []; e = xs[0]
    for x in xs: e = x * k + e * (1 - k); out.append(e)
    return out
def rsi(xs, n):
    out = [50.0] * len(xs); g = l = 0.0
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]; up, dn = max(d, 0), max(-d, 0)
        if i <= n: g += up / n; l += dn / n
        else: g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n
        out[i] = 100 - 100 / (1 + g / l) if l > 0 else 100.0
    return out
def atr(cs, n):
    out = []; a = None
    for i, c in enumerate(cs):
        tr = c[2] - c[3] if i == 0 else max(c[2] - c[3], abs(c[2] - cs[i-1][4]), abs(c[3] - cs[i-1][4]))
        a = tr if a is None else (a * (n - 1) + tr) / n; out.append(a)
    return out

def fee_pct(kind): return CFG["fees"][kind + "_pct"] / 100
def round_trip_cost_pct(spread_pct):
    f = CFG["fees"]; return (f[f["entry_type"] + "_pct"] + f[f["exit_type"] + "_pct"]) + 2 * CFG["slippage_pct"] + spread_pct

def signal(cs, spread_pct):
    """Return (go:bool, reason:str) for the last closed candle."""
    st = CFG["strategy"]; c = [x[4] for x in cs]
    if len(c) < st["ema_slow"] + 5: return False, "not enough data"
    ef, es, r, a = ema(c, st["ema_fast"]), ema(c, st["ema_slow"]), rsi(c, st["rsi_period"]), atr(cs, st["atr_period"])
    cross = ef[-2] <= es[-2] and ef[-1] > es[-1]
    exp_move = a[-1] * math.sqrt(st["expected_move_atr_bars"]) / c[-1] * 100
    cost = round_trip_cost_pct(spread_pct)
    txt = f"EMA{st['ema_fast']}={ef[-1]:.4g} EMA{st['ema_slow']}={es[-1]:.4g} RSI={r[-1]:.1f} expMove={exp_move:.2f}% cost={cost:.2f}%"
    if not cross: return False, "no cross; " + txt
    if not (st["rsi_min"] <= r[-1] <= st["rsi_max"]): return False, "cross but RSI filter fail; " + txt
    if exp_move < cost * st["min_expected_move_multiple"]: return False, "cross but expected move < cost x mult; " + txt
    if st["take_profit_pct"] <= cost: return False, "TP below round-trip cost; " + txt
    return True, "BUY: EMA bull cross + RSI ok + expMove>cost; " + txt

def fill(side, bid, ask, usd=None, qty=None, kind="taker"):
    slip = CFG["slippage_pct"] / 100
    px = ask * (1 + slip) if side == "buy" else bid * (1 - slip)
    if side == "buy":
        fee = usd * fee_pct(kind); qty = (usd - fee) / px; return px, qty, fee
    gross = qty * px; fee = gross * fee_pct(kind); return px, gross - fee, fee

# ---------- backtest ----------
def backtest(days=7, spread_pct=0.02):
    st, rk = CFG["strategy"], CFG["risk"]; g = CFG["granularity_sec"]; res = {}
    tot = {"trades": 0, "wins": 0, "gross": 0.0, "fees": 0.0, "net": 0.0}
    end = dt.datetime.now(dt.timezone.utc); allcs = {}
    for pair in CFG["pairs"]:
        cs = []; t1 = end
        while t1 > end - dt.timedelta(days=days):
            t0 = max(t1 - dt.timedelta(seconds=g * 300), end - dt.timedelta(days=days))
            cs += candles(pair, g, t0, t1); t1 = t0; time.sleep(0.35)
        allcs[pair] = sorted(set(cs))
    for pair, cs in allcs.items():
        bal = CFG["starting_balance_usd"]; pos = None; trades = []; cool = 0
        for i in range(60, len(cs)):
            c = cs[i]; half = c[4] * spread_pct / 200
            if pos:
                tp, sl = pos["px"] * (1 + st["take_profit_pct"]/100), pos["px"] * (1 - st["stop_loss_pct"]/100)
                ex = sl if c[3] <= sl else tp if c[2] >= tp else (c[4] if i - pos["i"] >= st["max_hold_bars"] else None)
                if ex:
                    px, proceeds, fee = fill("sell", ex - half, ex + half, qty=pos["qty"], kind=CFG["fees"]["exit_type"])
                    gross = pos["qty"] * px - pos["qty"] * pos["px"]  # price P&L before fees
                    net = proceeds - pos["usd"]; bal += proceeds
                    trades.append({"gross": gross, "net": net, "fees": fee + pos["fee"]}); pos = None
                    cool = i + (rk["cooldown_min_after_loss"] if net < 0 else rk["cooldown_min_after_exit"]) * 60 // g
                continue
            if i < cool: continue
            ok, _ = signal(cs[i-59:i+1], spread_pct)
            if ok:
                usd = bal * rk["max_position_pct"] / 100
                if usd < CFG["min_order_usd"]: continue
                px, qty, fee = fill("buy", c[4] - half, c[4] + half, usd=usd, kind=CFG["fees"]["entry_type"])
                bal -= usd; pos = {"px": px, "qty": qty, "usd": usd, "fee": fee, "i": i}
        n = len(trades); w = sum(t["net"] > 0 for t in trades)
        r = {"trades": n, "wins": w, "gross": sum(t["gross"] for t in trades) + sum(t["fees"] for t in trades) * 0,
             "fees": sum(t["fees"] for t in trades), "net": sum(t["net"] for t in trades), "bars": len(cs)}
        r["gross"] = r["net"] + r["fees"]  # gross = P&L if fees were zero (spread/slippage still applied)
        res[pair] = r
        for k in tot: tot[k] += r[k]
    res["TOTAL"] = tot; return res

# ---------- live paper daemon ----------
def db():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    con.executescript("""CREATE TABLE IF NOT EXISTS decisions(ts REAL, pair TEXT, action TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY, ts REAL, pair TEXT, side TEXT, px REAL, qty REAL, usd REAL, fee REAL, reason TEXT, pos_id INTEGER, gross REAL, net REAL);
    CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, pair TEXT, opened REAL, px REAL, qty REAL, usd REAL, fee REAL, tp REAL, sl REAL, closed REAL);
    CREATE TABLE IF NOT EXISTS equity(ts REAL, cash REAL, equity REAL);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);""")
    if not con.execute("select 1 from kv where k='cash'").fetchone():
        con.execute("insert into kv values('cash',?)", (str(CFG["starting_balance_usd"]),)); con.commit()
    return con
def kv(con, k, v=None):
    if v is None: r = con.execute("select v from kv where k=?", (k,)).fetchone(); return r[0] if r else None
    con.execute("insert or replace into kv values(?,?)", (k, str(v)))

def step(con):
    st, rk = CFG["strategy"], CFG["risk"]; now = time.time(); cash = float(kv(con, "cash"))
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    day_start = kv(con, "day_" + day)
    prices = {}
    for pair in CFG["pairs"]:
        try: prices[pair] = ticker(pair)
        except Exception as e: con.execute("insert into decisions values(?,?,?,?)", (now, pair, "error", f"ticker fail {e}"))
    opens = con.execute("select * from positions where closed is null").fetchall()
    def equity(): return cash + sum(p["qty"] * prices[p["pair"]][0] for p in con.execute("select * from positions where closed is null") if p["pair"] in prices)
    if day_start is None: kv(con, "day_" + day, equity()); day_start = equity()
    day_start = float(day_start)
    for p in opens:  # exits
        if p["pair"] not in prices: continue
        bid, ask, _ = prices[p["pair"]]; why = None
        if bid >= p["tp"]: why = f"TAKE-PROFIT bid {bid:.6g} >= {p['tp']:.6g}"
        elif bid <= p["sl"]: why = f"STOP-LOSS bid {bid:.6g} <= {p['sl']:.6g}"
        elif now - p["opened"] > st["max_hold_bars"] * CFG["granularity_sec"]: why = "TIME-EXIT max hold reached"
        if why:
            px, proceeds, fee = fill("sell", bid, ask, qty=p["qty"], kind=CFG["fees"]["exit_type"])
            net = proceeds - p["usd"]; gross = net + fee + p["fee"]; cash += proceeds
            con.execute("update positions set closed=? where id=?", (now, p["id"]))
            con.execute("insert into fills(ts,pair,side,px,qty,usd,fee,reason,pos_id,gross,net) values(?,?,?,?,?,?,?,?,?,?,?)", (now, p["pair"], "sell", px, p["qty"], proceeds, fee, why, p["id"], gross, net))
            kv(con, "cool_until", now + 60 * (rk["cooldown_min_after_loss"] if net < 0 else rk["cooldown_min_after_exit"]))
    kv(con, "cash", cash)
    eq = equity(); halted = (day_start - eq) / day_start * 100 >= rk["daily_loss_limit_pct"]
    ntoday = con.execute("select count(*) from fills where side='buy' and ts>=?", (dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp(),)).fetchone()[0]
    for pair in CFG["pairs"]:
        if pair not in prices: continue
        bid, ask, src = prices[pair]; spread = (ask - bid) / ((ask + bid) / 2) * 100
        nopen = con.execute("select count(*) from positions where closed is null").fetchone()[0]
        if con.execute("select 1 from positions where closed is null and pair=?", (pair,)).fetchone(): continue
        if halted: why = "HALT daily loss limit hit"; ok = False
        elif ntoday >= rk["max_trades_per_day"]: ok, why = False, "max trades/day reached"
        elif nopen >= rk["max_open_positions"]: ok, why = False, "max open positions"
        elif now < float(kv(con, "cool_until") or 0): ok, why = False, "cooldown"
        else:
            try: cs = candles(pair, CFG["granularity_sec"])[-100:-1]  # closed candles only
            except Exception as e: con.execute("insert into decisions values(?,?,?,?)", (now, pair, "error", str(e))); continue
            ok, why = signal(cs, spread)
        usd = min(cash, eq * rk["max_position_pct"] / 100)
        if ok and usd < max(CFG["min_order_usd"], min_size(pair)[1]): ok, why = False, "below min order size; " + why
        con.execute("insert into decisions values(?,?,?,?)", (now, pair, "buy" if ok else "hold", f"[{src} spread {spread:.3f}%] " + why))
        if ok:
            px, qty, fee = fill("buy", bid, ask, usd=usd, kind=CFG["fees"]["entry_type"])
            inc = min_size(pair)[0]; qty = math.floor(qty / inc) * inc
            cash -= usd; ntoday += 1
            cur = con.execute("insert into positions(pair,opened,px,qty,usd,fee,tp,sl) values(?,?,?,?,?,?,?,?)", (pair, now, px, qty, usd, fee, px * (1 + st["take_profit_pct"]/100), px * (1 - st["stop_loss_pct"]/100)))
            con.execute("insert into fills(ts,pair,side,px,qty,usd,fee,reason,pos_id) values(?,?,?,?,?,?,?,?,?)", (now, pair, "buy", px, qty, usd, fee, why, cur.lastrowid))
    kv(con, "cash", cash); kv(con, "prices", json.dumps({k: v[:2] for k, v in prices.items()}))
    con.execute("insert into equity values(?,?,?)", (now, cash, equity())); con.commit()

def snapshot(con=None):
    con = con or db(); prices = json.loads(kv(con, "prices") or "{}")
    sells = con.execute("select * from fills where side='sell'").fetchall()
    eqrows = con.execute("select ts,equity from equity order by ts").fetchall()
    eqrows = eqrows[:: max(1, len(eqrows) // 1500)] + eqrows[-1:]
    snap = {"generated": time.time(), "paper_only": True, "starting_balance": CFG["starting_balance_usd"],
        "cash": float(kv(con, "cash")), "equity": eqrows[-1][1] if eqrows else CFG["starting_balance_usd"],
        "equity_curve": [[r[0], round(r[1], 4)] for r in eqrows],
        "positions": [dict(p) | {"mark": prices.get(p["pair"], [None])[0]} for p in con.execute("select * from positions where closed is null")],
        "trades": [dict(f) for f in con.execute("select * from fills order by ts desc limit 200")],
        "decisions": [dict(d) for d in con.execute("select * from decisions order by ts desc limit 40")],
        "stats": {"closed_trades": len(sells), "wins": sum(s["net"] > 0 for s in sells),
            "win_rate": (sum(s["net"] > 0 for s in sells) / len(sells) * 100) if sells else None,
            "fees_paid": con.execute("select coalesce(sum(fee),0) from fills").fetchone()[0],
            "gross_pnl": sum(s["gross"] for s in sells), "net_pnl": sum(s["net"] for s in sells)},
        "rules": CFG, "backtest": json.load(open(os.path.join(ROOT, "reports/backtest.json"))) if os.path.exists(os.path.join(ROOT, "reports/backtest.json")) else None}
    return snap

def publish():
    """Write web/data.json and force-push a single squashed commit to gh-pages (no commit spam)."""
    json.dump(snapshot(), open(os.path.join(ROOT, "web/data.json"), "w"), default=str)
    d = os.path.join(ROOT, ".pages"); os.makedirs(d, exist_ok=True)
    subprocess.run(f"cp {ROOT}/web/* {d}/ && cd {d} && rm -rf .git && git init -q -b gh-pages && git add -A && git -c user.name=tradeterm-bot -c user.email=bot@localhost commit -qm 'snapshot' && git push -qf https://github.com/charlesanthonyleone/tradeterm.git gh-pages", shell=True, timeout=120, capture_output=True)

def run():
    con = db(); last_pub = 0
    while True:
        try: step(con)
        except Exception: con.execute("insert into decisions values(?,?,?,?)", (time.time(), "-", "error", traceback.format_exc()[-500:])); con.commit()
        if time.time() - last_pub > CFG["publish_every_sec"]:
            try: publish()
            except Exception: pass
            last_pub = time.time()
        time.sleep(CFG["loop_sec"])

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "backtest":
        r = backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 7); json.dump(r, open(os.path.join(ROOT, "reports/backtest.json"), "w"), indent=1); print(json.dumps(r, indent=1))
    elif cmd == "run": run()
    elif cmd == "publish": publish()
    elif cmd == "snapshot": json.dump(snapshot(), open(os.path.join(ROOT, "web/data.json"), "w"), default=str)
