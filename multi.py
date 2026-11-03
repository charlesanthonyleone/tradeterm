"""Multi-account PAPER engine: backtest, walk-forward, live. No real money, public data only."""
import json, os, time, math, sqlite3, datetime as dt, traceback, subprocess, sys, itertools, copy
import tt
from tt import ema, rsi, atr, ticker, min_size, S
ROOT = tt.ROOT; CFG = tt.CFG; RISK = CFG["risk"]
A = json.load(open(os.path.join(ROOT, "accounts.json")))
PAIRS = CFG["pairs"]; SLIP = CFG["slippage_pct"] / 100; START = CFG["starting_balance_usd"]
BT_SPREAD = 0.02  # % assumed spread in backtest (live median measured ~0.00-0.015% on these pairs)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True); os.makedirs(os.path.join(ROOT, "db"), exist_ok=True)

def fees(acct): return A["fee_profiles"][acct["fees"]]
def rt_cost(acct, spread=BT_SPREAD):
    f = fees(acct); e = f[acct["entry"] + "_pct"]; x = f[acct["exit"] + "_pct"]
    return e + x + spread + (SLIP * 100 if acct["entry"] == "taker" else 0) + (SLIP * 100 if acct["exit"] == "taker" else 0)

# ---------- history with disk cache ----------
def history(pair, gran, days):
    fn = os.path.join(ROOT, "data", f"{pair}_{gran}_{days}.json")
    if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 6 * 3600: return [tuple(x) for x in json.load(open(fn))]
    end = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0); t1 = end; cs = []
    while t1 > end - dt.timedelta(days=days):
        t0 = max(t1 - dt.timedelta(seconds=gran * 299), end - dt.timedelta(days=days))
        cs += tt.candles(pair, gran, t0, t1); t1 = t0; time.sleep(0.34)
    cs = sorted({c[0]: c for c in cs}.values()); json.dump(cs, open(fn, "w")); return cs

def indicators(cs, acct, p):
    c = [x[4] for x in cs]; ind = {"rsi": rsi(c, 14), "atr": atr(cs, 14)}
    if acct["strategy"] == "ema_cross": ind["f"] = ema(c, p["fast"]); ind["s"] = ema(c, p["slow"])
    else:
        n = p["sma"]; sm = []; acc = 0
        for i, x in enumerate(c):
            acc += x - (c[i - n] if i >= n else 0); sm.append(acc / min(i + 1, n))
        ind["sma"] = sm
    return ind

def entry_signal(acct, p, cs, ind, i, spread):
    """Rule evaluation on CLOSED bar i. Returns (go, reason, target_or_None)."""
    cost = rt_cost(acct, spread); c = cs[i][4]
    if i < 60: return False, "warmup", None
    if acct["strategy"] == "ema_cross":
        f, s, r = ind["f"], ind["s"], ind["rsi"][i]
        em = ind["atr"][i] * math.sqrt(p["atr_bars"]) / c * 100
        txt = f"EMA{p['fast']}/{p['slow']} {f[i]:.5g}/{s[i]:.5g} RSI {r:.1f} expMove {em:.2f}% cost {cost:.2f}%"
        if not (f[i-1] <= s[i-1] and f[i] > s[i]): return False, "no cross; " + txt, None
        if not (p["rsi_min"] <= r <= p["rsi_max"]): return False, "cross, RSI out of band; " + txt, None
        if em < cost * p["cost_mult"]: return False, f"cross, expMove < {p['cost_mult']}x cost; " + txt, None
        if p["tp"] <= cost: return False, "TP <= round-trip cost; " + txt, None
        return True, "BUY bull cross + RSI band + expMove>cost; " + txt, c * (1 + p["tp"] / 100)
    r = ind["rsi"][i]; m = ind["sma"][i]; gap = (m - c) / c * 100
    txt = f"RSI {r:.1f} close {c:.5g} SMA{p['sma']} {m:.5g} gap {gap:.2f}% cost {cost:.2f}%"
    if r >= p["rsi_buy"]: return False, "RSI not oversold; " + txt, None
    if gap < cost * p["min_gap_mult"]: return False, "oversold but gap to mean < cost; " + txt, None
    return True, f"BUY RSI<{p['rsi_buy']} and gap to mean > cost; " + txt, m

# ---------- event-driven backtest on one $20 account trading all pairs ----------
def backtest(acct, p, data, t_from, t_to):
    f = fees(acct); half = BT_SPREAD / 200; cash = START; pos = {}; trades = []; cool = 0
    ind = {pr: indicators(cs, acct, p) for pr, cs in data.items()}
    idx = {pr: {c[0]: k for k, c in enumerate(cs)} for pr, cs in data.items()}
    times = sorted({c[0] for cs in data.values() for c in cs if t_from <= c[0] < t_to})
    day = None; day_eq = START; nday = 0; halted = False; pend = {}; curve = []
    for t in times:
        d = t // 86400
        marks = {}
        for pr in data:
            k = idx[pr].get(t)
            if k is not None: marks[pr] = data[pr][k][4]
        eq = cash + sum(q["qty"] * marks.get(pr, q["px"]) for pr, q in pos.items())
        if d != day: day, day_eq, nday, halted = d, eq, 0, False
        for pr, cs in data.items():
            k = idx[pr].get(t)
            if k is None: continue
            o, h, l, c = cs[k][1], cs[k][2], cs[k][3], cs[k][4]
            if pr in pend:  # maker limit buy from previous bar
                L, usd, tgt, why = pend.pop(pr)
                if l < L and cash >= usd:
                    fee = usd * f["maker_pct"] / 100; qty = (usd - fee) / L; cash -= usd
                    pos[pr] = {"px": L, "qty": qty, "usd": usd, "fee": fee, "k": k, "tgt": tgt * L / data[pr][k-1][4] if acct["strategy"] == "ema_cross" else tgt, "sl": L * (1 - p["sl"] / 100)}
                    nday += 1
            if pr in pos:
                q = pos[pr]; ex = None
                if l <= q["sl"]: ex = (min(q["sl"], o) * (1 - half - SLIP), "taker", "SL")
                elif acct["strategy"] == "mean_rev" and k > q["k"]:
                    tgt = ind[pr]["sma"][k-1]
                    if h > tgt: ex = (tgt, acct["exit"], "MEAN")
                elif h > q["tgt"] and k > q["k"] - 1: ex = (q["tgt"], acct["exit"], "TP")
                if not ex and k - q["k"] >= p["max_hold"]: ex = (c * (1 - half - SLIP), "taker", "TIME")
                if ex:
                    px, kind, why = ex
                    if kind == "taker" and why in ("TP", "MEAN"): px = px * (1 - half - SLIP)
                    gross_out = q["qty"] * px; fee = gross_out * f[kind + "_pct"] / 100; cash += gross_out - fee
                    net = gross_out - fee - q["usd"]; trades.append({"t": t, "pair": pr, "why": why, "net": net, "fees": fee + q["fee"], "gross": net + fee + q["fee"]})
                    del pos[pr]; cool = t + 60 * (RISK["cooldown_min_after_loss"] if net < 0 else RISK["cooldown_min_after_exit"])
                    continue
            if pr in pos or pr in pend or t < cool or halted: continue
            if (day_eq - eq) / day_eq * 100 >= RISK["daily_loss_limit_pct"]: halted = True; continue
            if nday >= RISK["max_trades_per_day"] or len(pos) + len(pend) >= RISK["max_open_positions"]: continue
            go, why, tgt = entry_signal(acct, p, cs, ind[pr], k, BT_SPREAD)
            if not go: continue
            usd = min(cash, eq * RISK["max_position_pct"] / 100)
            if usd < CFG["min_order_usd"]: continue
            if acct["entry"] == "maker": pend[pr] = (c * (1 - half), usd, tgt, why)
            else:
                px = c * (1 + half + SLIP); fee = usd * f["taker_pct"] / 100; cash -= usd; nday += 1
                pos[pr] = {"px": px, "qty": (usd - fee) / px, "usd": usd, "fee": fee, "k": k + 1, "tgt": tgt * px / c if acct["strategy"] == "ema_cross" else tgt, "sl": px * (1 - p["sl"] / 100)}
        curve.append((t, eq))
    for pr, q in pos.items():  # mark-to-market close at end (taker)
        px = data[pr][max(i for tt_, i in idx[pr].items() if tt_ < t_to)][4] * (1 - half - SLIP)
        g = q["qty"] * px; fee = g * f["taker_pct"] / 100; net = g - fee - q["usd"]; cash += g - fee
        trades.append({"t": t_to, "pair": pr, "why": "END", "net": net, "fees": fee + q["fee"], "gross": net + fee + q["fee"]})
    n = len(trades); w = sum(x["net"] > 0 for x in trades)
    return {"trades": n, "wins": w, "win_rate": round(w / n * 100, 1) if n else None,
            "gross": round(sum(x["gross"] for x in trades), 4), "fees": round(sum(x["fees"] for x in trades), 4),
            "net": round(cash - START, 4), "end_balance": round(cash, 4),
            "max_dd_pct": round(maxdd(curve), 2)}

def maxdd(curve):
    pk = 0; dd = 0
    for _, e in curve: pk = max(pk, e); dd = max(dd, (pk - e) / pk * 100 if pk else 0)
    return dd

def apply_grid(acct, p, g):
    q = dict(p)
    if acct["strategy"] == "ema_cross": q["tp"] = p["tp"] * g["tp_mult"]; q["sl"] = p["sl"] * g["sl_mult"]
    else: q["sl"] = p["sl"] * g["sl_mult"]; q["rsi_buy"] = g["rsi_buy"]
    return q

def research():
    wf = A["walk_forward"]; days = wf["days"]; out = {"generated": time.time(), "days": days, "spread_assumed_pct": BT_SPREAD, "accounts": {}}
    cache = {}
    for acct in A["accounts"]:
        g = acct["gran"]
        if g not in cache: cache[g] = {pr: history(pr, g, days) for pr in PAIRS}
        data = cache[g]; t0 = min(cs[0][0] for cs in data.values()); t1 = max(cs[-1][0] for cs in data.values()) + 1
        full = backtest(acct, acct["p"], data, t0 + 3 * 86400, t1)  # first 3d = indicator warmup
        grid = wf["grid"][acct["strategy"]]; combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
        folds = []; ts = t0 + wf["train_days"] * 86400
        while ts + 86400 < t1:
            te = min(ts + wf["test_days"] * 86400, t1)
            best = max(combos, key=lambda c: backtest(acct, apply_grid(acct, acct["p"], c), data, ts - wf["train_days"] * 86400, ts)["net"])
            r = backtest(acct, apply_grid(acct, acct["p"], best), data, ts, te)
            folds.append({"test_from": ts, "test_to": te, "chosen_on_train": best, **r}); ts = te
        oos = {k: round(sum(f[k] for f in folds), 4) for k in ("trades", "wins", "gross", "fees", "net")}
        out["accounts"][acct["id"]] = {"full_fixed_params": full, "walk_forward_oos": oos, "folds": folds,
            "round_trip_cost_pct": round(rt_cost(acct), 3), "fees": acct["fees"], "fee_profile": fees(acct)}
        print(acct["id"], "full", full, "| WF-OOS", oos, flush=True)
    json.dump(out, open(os.path.join(ROOT, "reports/research.json"), "w"), indent=1); return out

# ---------- live paper ----------
def adb(aid):
    path = tt.DB if aid == "baseline" else os.path.join(ROOT, "db", aid + ".db")
    con = sqlite3.connect(path); con.row_factory = sqlite3.Row
    con.executescript("""CREATE TABLE IF NOT EXISTS decisions(ts REAL, pair TEXT, action TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY, ts REAL, pair TEXT, side TEXT, px REAL, qty REAL, usd REAL, fee REAL, reason TEXT, pos_id INTEGER, gross REAL, net REAL);
    CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, pair TEXT, opened REAL, px REAL, qty REAL, usd REAL, fee REAL, tp REAL, sl REAL, closed REAL);
    CREATE TABLE IF NOT EXISTS pending(pair TEXT PRIMARY KEY, ts REAL, limit_px REAL, usd REAL, tgt REAL, reason TEXT);
    CREATE TABLE IF NOT EXISTS equity(ts REAL, cash REAL, equity REAL);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);""")
    if not con.execute("select 1 from kv where k='cash'").fetchone(): con.execute("insert into kv values('cash',?)", (str(START),)); con.commit()
    return con
kv = tt.kv
_cc = {}
def live_candles(pair, gran):
    k = (pair, gran); now = time.time()
    if k not in _cc or now - _cc[k][0] > min(gran / 2, 300):
        _cc[k] = (now, tt.candles(pair, gran)[-300:])
    cs = _cc[k][1]; return [c for c in cs if c[0] + gran <= now]  # closed only

def full_ticker(pair):
    try:
        j = S.get(f"https://api.exchange.coinbase.com/products/{pair}/ticker", timeout=10).json()
        return float(j["bid"]), float(j["ask"]), float(j["price"]), "coinbase"
    except Exception:
        b, a, src = ticker(pair); return b, a, (b + a) / 2, src

def step_account(acct, con, px):
    p = acct["p"]; f = fees(acct); now = time.time(); cash = float(kv(con, "cash")); g = acct["gran"]
    log = lambda pr, a, r: con.execute("insert into decisions values(?,?,?,?)", (now, pr, a, r))
    def fillrow(pr, side, price, qty, usd, fee, why, pid, gross=None, net=None):
        con.execute("insert into fills(ts,pair,side,px,qty,usd,fee,reason,pos_id,gross,net) values(?,?,?,?,?,?,?,?,?,?,?)", (now, pr, side, price, qty, usd, fee, why, pid, gross, net))
    # pending maker buys
    for o in con.execute("select * from pending").fetchall():
        pr = o["pair"]
        if pr not in px: continue
        last = px[pr][2]
        if last < o["limit_px"] and cash >= o["usd"]:
            fee = o["usd"] * f["maker_pct"] / 100; qty = (o["usd"] - fee) / o["limit_px"]; cash -= o["usd"]
            tgt = o["tgt"]; sl = o["limit_px"] * (1 - p["sl"] / 100)
            cur = con.execute("insert into positions(pair,opened,px,qty,usd,fee,tp,sl) values(?,?,?,?,?,?,?,?)", (pr, now, o["limit_px"], qty, o["usd"], fee, tgt, sl))
            fillrow(pr, "buy", o["limit_px"], qty, o["usd"], fee, "MAKER limit filled (last trade < limit); " + o["reason"], cur.lastrowid)
            con.execute("delete from pending where pair=?", (pr,))
        elif now - o["ts"] > g: con.execute("delete from pending where pair=?", (pr,)); log(pr, "cancel", f"limit {o['limit_px']:.6g} not filled within 1 bar")
    # exits
    for q in con.execute("select * from positions where closed is null").fetchall():
        pr = q["pair"]
        if pr not in px: continue
        bid, ask, last, _ = px[pr]; ex = None
        tgt = q["tp"]
        if acct["strategy"] == "mean_rev":
            cs = live_candles(pr, g); ind = indicators(cs, acct, p); tgt = ind["sma"][-1]
        if bid <= q["sl"]: ex = (bid * (1 - SLIP), "taker", f"STOP-LOSS bid {bid:.6g} <= {q['sl']:.6g}")
        elif last > tgt: ex = ((tgt if acct["exit"] == "maker" else bid * (1 - SLIP)), acct["exit"], f"{'MEAN' if acct['strategy']=='mean_rev' else 'TAKE-PROFIT'} last {last:.6g} > target {tgt:.6g}")
        elif now - q["opened"] > p["max_hold"] * g: ex = (bid * (1 - SLIP), "taker", "TIME-EXIT max hold")
        if ex:
            price, kind, why = ex; gout = q["qty"] * price; fee = gout * f[kind + "_pct"] / 100; net = gout - fee - q["usd"]; cash += gout - fee
            con.execute("update positions set closed=? where id=?", (now, q["id"]))
            fillrow(pr, "sell", price, q["qty"], gout - fee, fee, why + f" [{kind}]", q["id"], net + fee + q["fee"], net)
            kv(con, "cool_until", now + 60 * (RISK["cooldown_min_after_loss"] if net < 0 else RISK["cooldown_min_after_exit"]))
    eq = cash + sum(r["qty"] * px.get(r["pair"], (r["px"],))[0] for r in con.execute("select * from positions where closed is null"))
    eq += sum(r["usd"] for r in con.execute("select usd from pending")) * 0
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if kv(con, "day_" + day) is None: kv(con, "day_" + day, eq)
    halted = (float(kv(con, "day_" + day)) - eq) / float(kv(con, "day_" + day)) * 100 >= RISK["daily_loss_limit_pct"]
    nday = con.execute("select count(*) from fills where side='buy' and ts>=?", (dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp(),)).fetchone()[0]
    for pr in PAIRS:
        if pr not in px: continue
        cs = live_candles(pr, g)
        if not cs or kv(con, "bar_" + pr) == str(cs[-1][0]): continue  # evaluate once per closed bar
        kv(con, "bar_" + pr, cs[-1][0])
        bid, ask, last, src = px[pr]; spread = (ask - bid) / ((ask + bid) / 2) * 100
        busy = con.execute("select (select count(*) from positions where closed is null)+(select count(*) from pending)").fetchone()[0]
        if con.execute("select 1 from positions where closed is null and pair=? union select 1 from pending where pair=?", (pr, pr)).fetchone(): continue
        if halted: go, why = False, "HALT daily loss limit"
        elif nday >= RISK["max_trades_per_day"]: go, why = False, "max trades/day"
        elif busy >= RISK["max_open_positions"]: go, why = False, "max open positions"
        elif now < float(kv(con, "cool_until") or 0): go, why = False, "cooldown"
        else:
            ind = indicators(cs, acct, p); go, why, tgt = entry_signal(acct, p, cs, ind, len(cs) - 1, spread)
        usd = min(cash, eq * RISK["max_position_pct"] / 100)
        if go and usd < max(CFG["min_order_usd"], min_size(pr)[1]): go, why = False, "below min order; " + why
        log(pr, "buy" if go else "hold", f"[{src} spread {spread:.3f}%] {why}")
        if go:
            if acct["entry"] == "maker":
                con.execute("insert or replace into pending values(?,?,?,?,?,?)", (pr, now, bid, usd, tgt * bid / cs[-1][4] if acct["strategy"] == "ema_cross" else tgt, why)); busy += 1
            else:
                price = ask * (1 + SLIP); fee = usd * f["taker_pct"] / 100; qty = (usd - fee) / price; cash -= usd; nday += 1
                cur = con.execute("insert into positions(pair,opened,px,qty,usd,fee,tp,sl) values(?,?,?,?,?,?,?,?)", (pr, now, price, qty, usd, fee, tgt * price / cs[-1][4], price * (1 - p["sl"] / 100)))
                fillrow(pr, "buy", price, qty, usd, fee, why + " [taker]", cur.lastrowid)
    kv(con, "cash", cash); kv(con, "prices", json.dumps({k: v[:2] for k, v in px.items()}))
    reserved = sum(r[0] for r in con.execute("select usd from pending"))
    con.execute("insert into equity values(?,?,?)", (now, cash, eq)); con.commit()

def snapshot():
    accts = []
    for acct in A["accounts"]:
        con = adb(acct["id"]); prices = json.loads(kv(con, "prices") or "{}")
        sells = con.execute("select * from fills where side='sell'").fetchall()
        er = con.execute("select ts,equity from equity order by ts").fetchall(); er = er[::max(1, len(er)//600)] + er[-1:]
        accts.append({"id": acct["id"], "strategy": acct["strategy"], "gran": acct["gran"], "fees": acct["fees"], "fee_profile": fees(acct),
            "entry": acct["entry"], "exit": acct["exit"], "params": acct["p"], "round_trip_cost_pct": round(rt_cost(acct), 3),
            "cash": float(kv(con, "cash")), "equity": er[-1][1] if er else START, "curve": [[r[0], round(r[1], 4)] for r in er],
            "positions": [dict(r) | {"mark": prices.get(r["pair"], [None])[0]} for r in con.execute("select * from positions where closed is null")],
            "pending": [dict(r) for r in con.execute("select * from pending")],
            "trades": [dict(r) for r in con.execute("select * from fills order by ts desc limit 60")],
            "decisions": [dict(r) for r in con.execute("select * from decisions order by ts desc limit 12")],
            "stats": {"closed": len(sells), "wins": sum(s["net"] > 0 for s in sells), "win_rate": round(sum(s["net"] > 0 for s in sells) / len(sells) * 100, 1) if sells else None,
                "fees": con.execute("select coalesce(sum(fee),0) from fills").fetchone()[0], "gross": sum(s["gross"] for s in sells), "net": sum(s["net"] for s in sells)}})
        con.close()
    rj = os.path.join(ROOT, "reports/research.json")
    return {"generated": time.time(), "paper_only": True, "starting_balance": START, "accounts": accts, "risk": RISK, "slippage_pct": CFG["slippage_pct"],
            "fee_sources": A["_fee_sources"], "research": json.load(open(rj)) if os.path.exists(rj) else None}

def write_snapshot(): json.dump(snapshot(), open(os.path.join(ROOT, "web/data.json"), "w"), default=str)
def publish():
    write_snapshot(); d = os.path.join(ROOT, ".pages"); os.makedirs(d, exist_ok=True)
    subprocess.run(f"cp {ROOT}/web/* {d}/ && cd {d} && rm -rf .git && git init -q -b gh-pages && git add -A && git -c user.name=tradeterm-bot -c user.email=bot@localhost commit -qm 'snapshot (squashed)' && git push -qf https://github.com/charlesanthonyleone/tradeterm.git gh-pages", shell=True, timeout=120, capture_output=True)

def run():
    cons = {a["id"]: adb(a["id"]) for a in A["accounts"]}; last_pub = 0; last_snap = 0
    while True:
        px = {}
        for pr in PAIRS:
            try: px[pr] = full_ticker(pr)
            except Exception as e: pass
        for a in A["accounts"]:
            try: step_account(a, cons[a["id"]], px)
            except Exception: c = cons[a["id"]]; c.execute("insert into decisions values(?,?,?,?)", (time.time(), "-", "error", traceback.format_exc()[-400:])); c.commit()
        if time.time() - last_snap > 60: write_snapshot(); last_snap = time.time()
        if time.time() - last_pub > CFG["publish_every_sec"]:
            try: publish()
            except Exception: pass
            last_pub = time.time()
        time.sleep(CFG["loop_sec"])

if __name__ == "__main__":
    {"research": research, "run": run, "publish": publish, "snapshot": write_snapshot}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
