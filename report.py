import json, os, time, tt
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
s = tt.snapshot(); R = os.path.join(tt.ROOT, "reports")
e = s["equity_curve"]; plt.figure(figsize=(9, 3)); plt.plot([time.strftime('%m-%d %H:%M', time.localtime(x[0])) for x in e], [x[1] for x in e]) if e else None
plt.axhline(s["starting_balance"], color="gray", ls="--"); plt.title("tradeterm PAPER equity"); plt.xticks([]); plt.tight_layout(); plt.savefig(f"{R}/equity.png", dpi=90)
st = s["stats"]; rows = "".join(f"<tr><td>{time.ctime(t['ts'])}</td><td>{t['pair']}</td><td>{t['side']}</td><td>{t['usd']:.4f}</td><td>{t['fee']:.4f}</td><td>{t['reason']}</td></tr>" for t in s["trades"])
open(f"{R}/report.html", "w").write(f"<h1>tradeterm PAPER report {time.ctime()}</h1><p>Equity ${s['equity']:.4f} (start ${s['starting_balance']}) | trades {st['closed_trades']} | fees {st['fees_paid']:.4f} | gross {st['gross_pnl']:.4f} | net {st['net_pnl']:.4f}</p><img src='equity.png'><pre>{json.dumps({k:v['TOTAL'] for k,v in (s['backtest'] or {}).items()},indent=1)}</pre><table border=1>{rows}</table>")
print("wrote", R)
