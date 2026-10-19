#!/usr/bin/env python3
"""Bootstrap a hazard-rate curve from the latest two days of a ticker's CDS trades.

Usage: python3 pd.py CRWV
"""
import math, statistics as st, sys
from common import load_trades, upfront, solve

ticker = sys.argv[1].upper()
trades = load_trades(ticker)
days = sorted({t['file_date'] for t in trades})[-2:]
trades = [t for t in trades if t['file_date'] in days]
t0 = max(t['trade'] for t in trades)

pts = {}
for t in trades:
    pts.setdefault((t['maturity'], t['coupon']), []).append(t['upfront'])

curve = []  # (T_end, hazard)
def surv(t):
    s, prev = 0, 0
    for te, h in curve + [(1e9, curve[-1][1] if curve else 0)]:
        s += h * (min(t, te) - prev)
        if t <= te: break
        prev = te
    return math.exp(-s)

print(f'{ticker} CDS, trades from {days[0]} to {days[-1]}')
print('maturity   yrs  n  coupon  clean-upfront  hazard')
for (mat, coupon), v in sorted(pts.items()):
    u, Tm = st.median(v), (mat - t0).days / 365.25
    if Tm <= 0 or (curve and Tm <= curve[-1][0]): continue  # skip a second coupon at the same maturity
    curve.append((Tm, 0))
    def f(h): curve[-1] = (Tm, h); return upfront(surv, Tm, coupon)
    h = solve(f, u); curve[-1] = (Tm, h)
    print(mat, f'{Tm:4.2f} {len(v):2d}  {coupon*100:4.0f}%   {u*100:7.2f}%     {h*100:5.2f}%')
for y in (1, 2, 3, 4, 5):
    print(f'P(default within {y}y) = {(1-surv(y))*100:.1f}%')
