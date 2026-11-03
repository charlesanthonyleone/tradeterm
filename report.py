import json, os, time, multi
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
s = multi.snapshot(); R = os.path.join(multi.ROOT, "reports")
plt.figure(figsize=(10, 3.5))
for a in s["accounts"]:
    if a["curve"]: plt.plot([x[0] for x in a["curve"]], [x[1] for x in a["curve"]], label=a["id"])
plt.axhline(20, color="gray", ls="--"); plt.legend(fontsize=6); plt.title("tradeterm PAPER equity (all accounts)"); plt.xticks([]); plt.tight_layout(); plt.savefig(f"{R}/equity.png", dpi=90)
rows = "".join(f"<tr><td>{k}</td><td>{v['round_trip_cost_pct']}%</td><td>{v['full_fixed_params']['trades']}</td><td>{v['full_fixed_params']['gross']}</td><td>{v['full_fixed_params']['fees']}</td><td>{v['full_fixed_params']['net']}</td><td>{v['walk_forward_oos']['trades']}</td><td>{v['walk_forward_oos']['gross']}</td><td>{v['walk_forward_oos']['net']}</td></tr>" for k, v in (s["research"] or {}).get("accounts", {}).items())
live = "".join(f"<tr><td>{a['id']}</td><td>{a['equity']:.4f}</td><td>{a['stats']['closed']}</td><td>{a['stats']['fees']:.4f}</td><td>{a['stats']['gross']:.4f}</td><td>{a['stats']['net']:.4f}</td></tr>" for a in s["accounts"])
open(f"{R}/report.html", "w").write(f"<h1>tradeterm PAPER report {time.ctime()}</h1><img src='equity.png'><h2>Live</h2><table border=1><tr><th>acct</th><th>equity</th><th>closed</th><th>fees</th><th>gross</th><th>net</th></tr>{live}</table><h2>Backtest 45d / walk-forward OOS</h2><table border=1><tr><th>acct</th><th>RT cost</th><th>trades</th><th>gross</th><th>fees</th><th>net</th><th>OOS trades</th><th>OOS gross</th><th>OOS net</th></tr>{rows}</table>")
print("wrote", R)
