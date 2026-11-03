"""Live PAPER accounts for the top research2 candidates. Public data only, no keys, no real orders.
Each loop: rebuild signal from the latest CLOSED public bars, rebalance a virtual $20 at the live Coinbase bid/ask
with maker fee + 0.05% slip (same cost model as the backtest). State in db/cand_<id>.json (untracked)."""
import json, os, time, requests
import research2 as R
ROOT = R.ROOT; DB = os.path.join(ROOT, "db"); os.makedirs(DB, exist_ok=True)
RES = json.load(open(os.path.join(ROOT, "reports/research2.json")))
TOP = RES["ranking_by_dev_oos_t"][:3]
CANDS = [{"id": f"cand_{fam}", "family": fam, "params": RES["families"][fam]["frozen_params"], "fees": "kraken_t1",
          "lock": RES["families"][fam]["lock"]} for fam in TOP]
_cache = {}
def bars(coin, iv):
    k = (coin, iv)
    if k in _cache and time.time() - _cache[k][0] < 600: return _cache[k][1]
    r = requests.get("https://data-api.binance.vision/api/v3/klines", params={"symbol": coin + "USDT", "interval": iv, "limit": 1000}, timeout=15).json()
    rows = [[x[0] // 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r][:-1]  # closed bars only
    _cache[k] = (time.time(), rows); return rows
def fund(coin):
    k = ("f", coin)
    if k in _cache and time.time() - _cache[k][0] < 3600: return _cache[k][1]
    out = []; t = int((time.time() - 130 * 86400) * 1000)
    while True:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "fundingHistory", "coin": coin, "startTime": t}, timeout=15).json()
        out += [[x["time"] // 1000, float(x["fundingRate"])] for x in r]
        if len(r) < 500: break
        t = r[-1]["time"] + 1
    v = R.daily_funding(out); _cache[k] = (time.time(), v); return v
def target(c):
    fam, p = c["family"], c["params"]; coins = R.COINS if fam.startswith("rs_rotation") else sorted({p.get("coin", "BTC"), "BTC"})
    iv = "4h" if fam.endswith("4h") else "1d"; D = {x: bars(x, iv) for x in coins}
    n = min(len(v) for v in D.values()); D = {x: v[-n:] for x, v in D.items()}; ts = [r[0] for r in D["BTC"]]
    if fam == "funding_filter_1d": W = R.w_funding(ts, D, p["coin"], p["q"], fund=fund(p["coin"]))
    else: W = {"trend_ema_1d": R.w_trend, "trend_ema_4h": R.w_trend, "breakout_volfilter_1d": R.w_breakout, "regime_btc_sma_1d": R.w_regime,
               "rs_rotation_8coins_1d": R.w_rotation}[fam](ts, D, **p)
    return W[-1], ts[-1]
def load(c):
    fn = f"{DB}/{c['id']}.json"
    return json.load(open(fn)) if os.path.exists(fn) else {"cash": 20.0, "hold": {}, "trades": [], "curve": [], "last_bar": None, "last_reason": ""}
def step(c, px):
    s = load(c); cost = (R.FEES[c["fees"]]["maker"] + R.SLIP) / 100
    try:
        tgt, bar = target(c)
        mark = {x: px[x + "-USD"][0] for x in s["hold"] if x + "-USD" in px}
        eq = s["cash"] + sum(q * mark.get(x, 0) for x, q in s["hold"].items())
        if bar != s["last_bar"]:
            s["last_bar"] = bar; s["last_reason"] = f"bar {bar} target {tgt}"
            if set(tgt) != set(s["hold"]):
                for x, q in list(s["hold"].items()):  # sell
                    if x not in tgt and x + "-USD" in px:
                        g = q * px[x + "-USD"][0]; s["cash"] += g * (1 - cost); del s["hold"][x]
                        s["trades"].append({"ts": time.time(), "side": "sell", "coin": x, "usd": round(g, 4), "fee": round(g * cost, 4)})
                eq = s["cash"] + sum(q * px[x + "-USD"][0] for x, q in s["hold"].items())
                for x, w in tgt.items():  # buy
                    if x not in s["hold"] and x + "-USD" in px:
                        usd = min(s["cash"], w * eq); ask = px[x + "-USD"][1]
                        s["hold"][x] = usd * (1 - cost) / ask; s["cash"] -= usd
                        s["trades"].append({"ts": time.time(), "side": "buy", "coin": x, "usd": round(usd, 4), "fee": round(usd * cost, 4)})
        eq = s["cash"] + sum(q * px.get(x + "-USD", (0,))[0] for x, q in s["hold"].items())
        if not s["curve"] or time.time() - s["curve"][-1][0] > 600: s["curve"].append([time.time(), round(eq, 4)])
        s["equity"] = eq; s["error"] = None
    except Exception as e: s["error"] = repr(e)[:200]
    json.dump(s, open(f"{DB}/{c['id']}.json", "w"))
def ensure_prices(px):
    for x in {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"}:
        k = x + "-USD"
        if k not in px:
            try: j = requests.get(f"https://api.exchange.coinbase.com/products/{k}/ticker", timeout=10).json(); px[k] = (float(j["bid"]), float(j["ask"]))
            except Exception: pass
    return px
def step_all(px):
    px = ensure_prices({k: (v[0], v[1]) for k, v in px.items()})
    for c in CANDS: step(c, px)
def snapshot():
    out = []
    for c in CANDS:
        s = load(c); out.append(c | {"equity": s.get("equity", 20.0), "cash": s["cash"], "hold": s["hold"], "trades": s["trades"][-20:], "curve": s["curve"][-600:],
                                     "last_reason": s["last_reason"], "error": s.get("error")})
    return {"accounts": out, "verdict": RES["verdict"], "hurdle": RES["t_stat_hurdle_bonferroni"], "n_configs": RES["n_configs_tested"],
            "holdout_from": RES["holdout_from"], "families": {k: {kk: v[kk] for kk in ("configs", "walk_forward_dev_oos", "frozen_params", "holdout", "lock", "lock_reasons")} for k, v in RES["families"].items()},
            "btc_buy_hold_holdout": RES["btc_buy_hold_holdout"]}
if __name__ == "__main__":
    step_all({}); print(json.dumps(snapshot()["accounts"], default=str)[:1500])
