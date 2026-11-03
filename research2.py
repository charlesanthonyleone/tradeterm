"""Wide strategy search on ~2.5y public candles: walk-forward on a dev period, one untouched final holdout.
PAPER research only. Long-only spot, one $20 account. Usage: python3 research2.py"""
import json, os, math, time, statistics as st, datetime as dt
from fetch_long import klines, funding, COINS
ROOT = os.path.dirname(os.path.abspath(__file__))
FEES = {"kraken_t1": {"maker": 0.40, "taker": 0.80}, "cb_us": {"maker": 0.50, "taker": 0.90}}
SLIP = 0.05      # % adverse per side (spread/slippage) on top of the fee
HOLDOUT_DAYS = 180; TRAIN_DAYS = 180; TEST_DAYS = 60; START = 20.0
KRAKEN_MIN_USD = {"BTC": 4, "ETH": 5, "SOL": 5, "XRP": 5, "DOGE": 5, "ADA": 5, "AVAX": 5, "LINK": 5}  # ~ordermin*price, approx 2026-11

def ema(x, n):
    k = 2 / (n + 1); o = [x[0]]
    for v in x[1:]: o.append(o[-1] + k * (v - o[-1]))
    return o
def sma(x, n):
    o = []; s = 0
    for i, v in enumerate(x):
        s += v - (x[i - n] if i >= n else 0); o.append(s / n if i >= n - 1 else float("nan"))
    return o
def atr(cs, n=14):
    tr = [cs[0][2] - cs[0][3]] + [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4])) for p, c in zip(cs, cs[1:])]
    return ema(tr, n)

# ---------- data aligned on a common time grid ----------
def load(iv):
    d = {c: klines(c, iv) for c in COINS}
    ts = sorted(set.intersection(*[set(x[0] for x in v) for v in d.values()]))
    return ts, {c: [r for r in v if r[0] in set(ts)] for c, v in d.items()}
def daily_funding(rows):
    by = {}
    for t, r in rows: by.setdefault(t // 86400 * 86400, []).append(r)
    return {k: sum(v) for k, v in by.items()}  # daily summed hourly funding
FUND = {}
def init():
    global T1, D1, T4, D4
    T1, D1 = load("1d"); T4, D4 = load("4h")
    for c in ("BTC", "ETH"): FUND[c] = daily_funding(funding(c))

# ---------- signal families: each returns target weights per bar {coin: w} decided on bar close ----------
def w_trend(ts, D, coin, f, s):
    c = [x[4] for x in D[coin]]; ef, es = ema(c, f), ema(c, s)
    return [{coin: 1.0} if i > s and ef[i] > es[i] and c[i] > es[i] else {} for i in range(len(ts))]
def w_breakout(ts, D, coin, n):
    cs = D[coin]; c = [x[4] for x in cs]; a = atr(cs); am = sma([a[i] / c[i] for i in range(len(c))], 100); out = []; on = False
    for i in range(len(c)):
        if i < max(n, 100): out.append({}); continue
        hi = max(x[2] for x in cs[i - n:i]); lo = min(x[3] for x in cs[i - n // 2:i])
        if not on and c[i] > hi and a[i] / c[i] < 1.5 * am[i]: on = True
        elif on and c[i] < lo: on = False
        out.append({coin: 1.0} if on else {})
    return out
def w_regime(ts, D, coin, n):
    b = [x[4] for x in D["BTC"]]; m = sma(b, n)
    return [{coin: 1.0} if i >= n and b[i] > m[i] else {} for i in range(len(ts))]
def w_rotation(ts, D, lb, k):
    b = [x[4] for x in D["BTC"]]; m = sma(b, 100); out = []; cur = {}
    for i in range(len(ts)):
        if i >= max(lb, 100) and dt.datetime.fromtimestamp(ts[i], dt.timezone.utc).weekday() == 6:  # decide Sunday close
            if b[i] > m[i]:
                mom = sorted(((D[c][i][4] / D[c][i - lb][4] - 1, c) for c in COINS), reverse=True)
                cur = {c: 1.0 / k for r, c in mom[:k] if r > 0}
            else: cur = {}
        elif i >= 100 and b[i] < m[i]: cur = {}   # regime break exits mid-week
        out.append(dict(cur))
    return out
def w_funding(ts, D, coin, q, fund=None):
    fund = fund or FUND[coin]
    c = [x[4] for x in D[coin]]; m = sma(c, 50); fr = [fund.get(t, float("nan")) for t in ts]; out = []
    for i in range(len(ts)):
        w7 = [x for x in fr[max(0, i - 6):i + 1] if x == x]; w90 = [sum(fr[j - 6:j + 1]) / 7 for j in range(max(7, i - 89), i + 1)]
        w90 = sorted(x for x in w90 if x == x)
        ok = i >= 100 and len(w7) >= 5 and w90 and c[i] > m[i] and sum(w7) / len(w7) <= w90[int(q * (len(w90) - 1))]
        out.append({coin: 1.0} if ok else {})
    return out

def sim_weights(ts, D, W, fee, t0, t1):
    """Rebalance to W[i] at close of bar i (maker limit at close, cost=fee+slip per side). Returns per-bar equity + trades."""
    cost = (FEES[fee]["maker"] + SLIP) / 100; eq = START; hold = {}; curve = []; trades = []
    for i in range(1, len(ts)):
        if ts[i] < t0 or ts[i] >= t1: continue
        # mark-to-market from close i-1 to close i
        val = {c: v * D[c][i][4] / D[c][i - 1][4] for c, v in hold.items()}; eq = eq - sum(hold.values()) + sum(val.values()); hold = val
        tgt = W[i]
        if set(tgt) != set(hold) or any(abs(hold.get(c, 0) / eq - w) > 0.34 for c, w in tgt.items()):
            new = {c: w * eq for c, w in tgt.items()}
            for c in set(hold) | set(new):
                d = abs(new.get(c, 0) - hold.get(c, 0))
                if d > 1e-9: eq -= d * cost; trades.append((ts[i], c, d))
            hold = {c: v * (1 - 0) for c, v in new.items()}
            s = sum(hold.values()); hold = {c: v / s * min(s, eq) for c, v in hold.items()} if s else {}
        curve.append((ts[i], eq))
    return curve, trades

def sim_grid(ts, D, coin, step, fee, t0, t1):
    """One-lot range grid on 4h: active only when the 50-bar SMA is flat; buy limit step% under SMA20, sell step% above fill; stop 3 steps down."""
    cs = D[coin]; c = [x[4] for x in cs]; m20 = sma(c, 20); m50 = sma(c, 50); f = FEES[fee]
    eq = START; pos = None; curve = []; trades = []; s = step / 100
    for i in range(70, len(ts)):
        if ts[i] < t0 or ts[i] >= t1: continue
        o, h, l, cl = cs[i][1:5]
        flat = abs(m50[i - 1] / m50[i - 21] - 1) < 0.04
        if pos is None and flat:
            L = m20[i - 1] * (1 - s)
            if l < L:
                q = eq * (1 - (f["maker"]) / 100) / L; pos = (L, q); trades.append((ts[i], coin, eq))
        elif pos:
            L, q = pos
            if l <= L * (1 - 3 * s): px = min(o, L * (1 - 3 * s)) * (1 - SLIP / 100); eq = q * px * (1 - f["taker"] / 100); pos = None; trades.append((ts[i], coin, eq))
            elif h > L * (1 + s): eq = q * L * (1 + s) * (1 - f["maker"] / 100); pos = None; trades.append((ts[i], coin, eq))
        curve.append((ts[i], eq if pos is None else pos[1] * cl))
    return curve, trades

FAM = {
 "trend_ema_1d": [dict(coin=c, f=f, s=s) for c in ("BTC", "ETH") for f, s in ((20, 50), (50, 200))],
 "trend_ema_4h": [dict(coin=c, f=f, s=s) for c in ("BTC", "ETH") for f, s in ((50, 200), (100, 400))],
 "breakout_volfilter_1d": [dict(coin=c, n=n) for c in ("BTC", "ETH", "SOL") for n in (20, 55)],
 "regime_btc_sma_1d": [dict(coin=c, n=n) for c in ("BTC", "ETH") for n in (50, 100, 200)],
 "rs_rotation_8coins_1d": [dict(lb=lb, k=k) for lb in (30, 60, 90) for k in (1, 2)],
 "funding_filter_1d": [dict(coin=c, q=q) for c in ("BTC", "ETH") for q in (0.5, 0.8)],
 "range_grid_4h": [dict(coin=c, step=s) for c in ("BTC", "ETH") for s in (2.0, 3.0)],
}
N_CONFIGS = sum(len(v) for v in FAM.values())
_wcache = {}
def run(fam, p, fee, t0, t1):
    if fam == "range_grid_4h": return sim_grid(T4, D4, p["coin"], p["step"], fee, t0, t1)
    ts, D = (T4, D4) if fam.endswith("4h") else (T1, D1)
    key = (fam, json.dumps(p, sort_keys=True))
    if key not in _wcache:
        fn = {"trend_ema_1d": w_trend, "trend_ema_4h": w_trend, "breakout_volfilter_1d": w_breakout, "regime_btc_sma_1d": w_regime,
              "rs_rotation_8coins_1d": w_rotation, "funding_filter_1d": w_funding}[fam]
        _wcache[key] = fn(ts, D, **p)
    return sim_weights(ts, D, _wcache[key], fee, t0, t1)

def daily(curve, start_eq=START):
    by = {}
    for t, e in curve: by[t // 86400] = e
    ks = sorted(by); out = []; prev = start_eq
    for k in ks: out.append((k, by[k] / prev - 1)); prev = by[k]
    return out
def metrics(dr, trades, days_total):
    if not dr: return None
    r = [x for _, x in dr]; eq = START; peak = START; mdd = 0
    for x in r: eq *= 1 + x; peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
    wk = {}
    for k, x in dr: wk.setdefault((k + 3) // 7, []).append(x)
    wr = [math.prod(1 + y for y in v) - 1 for v in wk.values()]
    act = [x for x in r if abs(x) > 1e-12]
    sd = st.pstdev(r) if len(r) > 1 else 0; mu = st.mean(r)
    return {"days": len(r), "net_usd": round(eq - START, 2), "net_pct": round((eq / START - 1) * 100, 1),
            "profitable_days_pct": round(100 * sum(x > 0 for x in r) / len(r), 1),
            "win_rate_active_days_pct": round(100 * sum(x > 0 for x in act) / len(act), 1) if act else None,
            "days_in_market_pct": round(100 * len(act) / len(r), 1),
            "profitable_weeks_pct": round(100 * sum(x > 0 for x in wr) / len(wr), 1), "weeks": len(wr),
            "max_dd_pct": round(mdd * 100, 1), "trades": len(trades),
            "min_trade_usd": round(min(t[2] for t in trades), 2) if trades else None,
            "sharpe_ann": round(mu / sd * math.sqrt(365), 2) if sd else 0, "t_stat": round(mu / sd * math.sqrt(len(r)), 2) if sd else 0}

def main():
    init()
    now = T1[-1] + 86400; hold0 = now - HOLDOUT_DAYS * 86400; dev0 = T1[0] + 200 * 86400  # 200d indicator warmup
    folds = []; t = dev0
    while t + (TRAIN_DAYS + TEST_DAYS) * 86400 <= hold0: folds.append((t, t + TRAIN_DAYS * 86400, t + (TRAIN_DAYS + TEST_DAYS) * 86400)); t += TEST_DAYS * 86400
    # Bonferroni-style hurdle for the dev OOS t-stat across all configs tried (one-sided 5%)
    from statistics import NormalDist
    hurdle = round(NormalDist().inv_cdf(1 - 0.05 / N_CONFIGS), 2)
    res = {"generated": time.time(), "paper_only": True, "data": "Binance public spot candles (data-api.binance.vision) 2024-05-01..now; Hyperliquid public funding (BTC, ETH)",
           "coins": COINS, "fees_pct": FEES, "slip_pct_per_side": SLIP, "n_configs_tested": N_CONFIGS, "t_stat_hurdle_bonferroni": hurdle,
           "dev_from": dev0, "holdout_from": hold0, "holdout_to": now, "folds": len(folds), "train_days": TRAIN_DAYS, "test_days": TEST_DAYS,
           "assumptions": "Entries/exits at the bar close as post-only limit orders (maker fee + 0.05% slip); grid stops pay taker. Long-only spot, no leverage, one $20 account, full position (or 1/k for rotation).",
           "families": {}}
    for fam, grid in FAM.items():
        oos_curve = []; eq = START; chosen = []; ntr = []
        for a, b, c in folds:
            best = max(grid, key=lambda p: (run(fam, p, "kraken_t1", a, b)[0] or [(0, START)])[-1][1])
            cv, tr = run(fam, best, "kraken_t1", b, c); chosen.append(best); ntr += tr
            oos_curve += [(t, eq * e / START) for t, e in cv]; eq = oos_curve[-1][1] if oos_curve else eq
        dev_m = metrics(daily(oos_curve), ntr, None)
        # freeze params on the last TRAIN window before the holdout, then run the untouched holdout once
        frozen = max(grid, key=lambda p: (run(fam, p, "kraken_t1", hold0 - TRAIN_DAYS * 86400, hold0)[0] or [(0, START)])[-1][1])
        ho = {}
        for fee in FEES:
            cv, tr = run(fam, frozen, fee, hold0, now); ho[fee] = metrics(daily(cv), tr, None)
        cv, tr = run(fam, frozen, "kraken_t1", hold0 - 1, now)
        bh = metrics(daily([(T1[i], START * D1["BTC"][i][4] / D1["BTC"][[j for j in range(len(T1)) if T1[j] >= hold0][0] - 1][4]) for i in range(len(T1)) if T1[i] >= hold0]), [], None)
        res["families"][fam] = {"configs": len(grid), "walk_forward_dev_oos": dev_m, "chosen_per_fold": chosen, "frozen_params": frozen,
                                "holdout": ho, "holdout_equity": [[t, round(e, 3)] for t, e in cv][::6]}
        print(fam, "devOOS", dev_m and {k: dev_m[k] for k in ("net_pct", "profitable_weeks_pct", "max_dd_pct", "t_stat")}, "HO", {k: ho["kraken_t1"][k] for k in ("net_pct", "profitable_days_pct", "profitable_weeks_pct", "max_dd_pct", "trades")} if ho["kraken_t1"] else None, frozen)
    res["btc_buy_hold_holdout"] = bh
    # lock rule (decided before looking at holdout numbers)
    for fam, v in res["families"].items():
        h = v["holdout"]["kraken_t1"]; hc = v["holdout"]["cb_us"]; d = v["walk_forward_dev_oos"]
        why = []
        if not (h and h["net_usd"] > 0): why.append("holdout net <= 0 (Kraken T1)")
        if not (hc and hc["net_usd"] > 0): why.append("holdout net <= 0 (Coinbase US)")
        if not (h and h["profitable_weeks_pct"] > 50): why.append("profitable weeks <= 50% on holdout")
        if not (h and h["max_dd_pct"] <= 20): why.append("holdout max DD > 20%")
        if not (d and d["t_stat"] >= hurdle): why.append(f"dev OOS t-stat {d and d['t_stat']} < multiple-testing hurdle {hurdle}")
        if not (h and h["trades"] and h["min_trade_usd"] >= 5): why.append("min trade < $5 (below Kraken minimums at $20)")
        v["lock"] = "locked" if not why else "not locked"; v["lock_reasons"] = why
    rank = sorted(res["families"], key=lambda k: (res["families"][k]["walk_forward_dev_oos"] or {}).get("t_stat", -9), reverse=True)
    res["ranking_by_dev_oos_t"] = rank
    res["verdict"] = ("LOCKED: " + ", ".join(k for k in rank if res["families"][k]["lock"] == "locked")) if any(v["lock"] == "locked" for v in res["families"].values()) else "Nothing qualifies for lock."
    json.dump(res, open(os.path.join(ROOT, "reports/research2.json"), "w"), indent=1, default=str); print(res["verdict"], "hurdle", hurdle, "N", N_CONFIGS)
if __name__ == "__main__": main()
