"""Fetch ~2.5y of public candles (Binance public spot data mirror) + Hyperliquid funding. Cache in data/long (untracked)."""
import requests, json, os, time
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "long"); os.makedirs(D, exist_ok=True)
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
START = 1714521600000  # 2024-05-01 UTC
def klines(sym, iv):
    fn = f"{D}/{sym}_{iv}.json"
    if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 6 * 3600: return json.load(open(fn))
    out = []; t = START
    while True:
        r = requests.get("https://data-api.binance.vision/api/v3/klines", params={"symbol": sym + "USDT", "interval": iv, "startTime": t, "limit": 1000}, timeout=20).json()
        if not r: break
        out += [[x[0] // 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r]
        if len(r) < 1000: break
        t = r[-1][0] + 1; time.sleep(0.2)
    out = out[:-1]  # drop the still-open bar
    json.dump(out, open(fn, "w")); return out
def funding(coin):
    fn = f"{D}/{coin}_funding.json"
    if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 6 * 3600: return json.load(open(fn))
    out = []; t = START
    while True:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "fundingHistory", "coin": coin, "startTime": t}, timeout=20).json()
        if not r: break
        out += [[x["time"] // 1000, float(x["fundingRate"])] for x in r]
        if len(r) < 500: break
        t = r[-1]["time"] + 1; time.sleep(0.3)
    json.dump(out, open(fn, "w")); return out
if __name__ == "__main__":
    for c in COINS:
        for iv in ("4h", "1d"): print(c, iv, len(klines(c, iv)))
    for c in ("BTC", "ETH", "SOL"): print(c, "funding", len(funding(c)))
