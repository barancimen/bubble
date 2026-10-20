#!/usr/bin/env python3
"""Weekly (or monthly, for long spans) 5-year default probability implied by a ticker's CDS trades, as an ASCII chart with 90% error bars."""
import math, random, statistics as st, sys
from datetime import timedelta
from common import load_trades

# Usage: python3 pd_history.py CRWV [--zero]   (--zero starts the vertical axis at 0%)
ticker = sys.argv[1].upper()
zero = '--zero' in sys.argv
# Near-5y contracts only (4-6 years left), so the curve's slope doesn't bias the estimate.
pts = [(t['trade'], 1 - math.exp(-5 * t['hazard']), t['anchored']) for t in load_trades(ticker, [(4, 6)])]

# Weekly buckets, or monthly once the data spans more than ~6 months (keeps the chart terminal-width).
monthly = (max(p[0] for p in pts) - min(p[0] for p in pts)).days > 182
period = 'month' if monthly else 'week'
start = min(p[0] for p in pts)
start -= timedelta(days=start.weekday())
weeks, anchored = {}, {}
for d, p, a in pts:
    key = d.replace(day=1) if monthly else start + timedelta(weeks=(d - start).days // 7)
    weeks.setdefault(key, []).append(p)
    anchored.setdefault(key, []).append(a)

random.seed(0)
series = []
for wk in sorted(weeks):
    v = weeks[wk]
    boots = sorted(st.median(random.choices(v, k=len(v))) for _ in range(2000))
    assumed = sum(anchored[wk]) < len(v) / 2  # most trades' up-front direction not settled
    series.append((wk, st.median(v), boots[100], boots[1899], len(v), assumed))

# Each row is a round step (0.5%, 1%, 2% ...) so labels land on exact values; at most ~25 rows.
lo_p, hi_p = 0 if zero else min(x[2] for x in series), max(x[3] for x in series)
step = next(d for d in (0.005, 0.01, 0.02, 0.025, 0.05) if (hi_p - lo_p) / d <= 24)
# Labels every 1/2/4/5/10% (a multiple of the row step), with both ends of the axis on a label.
lab = next(L for L in (0.01, 0.02, 0.04, 0.05, 0.1) if round(L / step, 6) % 1 == 0
           and (math.ceil(hi_p / L) - math.floor(lo_p / L)) <= 8)
lo_y, hi_y = math.floor(lo_p / lab) * lab, math.ceil(hi_p / lab) * lab
while (hi_y - lo_y) / step > 30:  # extending to label multiples can add rows; coarsen the step if so
    step *= 2
H, W = round((hi_y - lo_y) / step) + 1, 4
label_every = round(lab / step)
row = lambda p: round((hi_y - p) / step)

# One column per period, including empty ones, so the time axis is evenly spaced.
def periods(a, b):
    while a <= b:
        yield a
        a = (a.replace(day=28) + timedelta(days=4)).replace(day=1) if monthly else a + timedelta(weeks=1)
by_key = {x[0]: x for x in series}
cols = list(periods(series[0][0], series[-1][0]))
grid = [[' '] * (len(cols) * W) for _ in range(H)]
for i, key in enumerate(cols):
    if key not in by_key:
        continue
    _, med, lo, hi, _, _ = by_key[key]
    x = i * W + 2
    for y in range(row(hi), row(lo) + 1):
        grid[y][x] = '|'
    grid[row(hi)][x] = grid[row(lo)][x] = '-'
    grid[row(med)][x] = 'o'

print(f'{ticker}: market-implied P(default within 5y), {period}ly median, 90% bootstrap CI\n')
for y in range(H):
    p = hi_y - y * (hi_y - lo_y) / (H - 1)
    label = f'{p*100:5.4g}% '.rjust(7) if (H - 1 - y) % label_every == 0 else '       '
    print(label + '┤' + ''.join(grid[y]))
print('       └' + '─' * (len(cols) * W))
print('        ' + ''.join(wk.strftime('%b%y' if monthly else '%m%d').ljust(W) if i % 2 == 0 else ' ' * W for i, wk in enumerate(cols)))
print(f'\n{period} of'.ljust(13) + 'median  90% CI          trades')
for wk, med, lo, hi, n, assumed in series:
    print(f'{wk:%Y-%m}     ' if monthly else f'{wk}  ', end='')
    print(f'{med*100:5.1f}%  {lo*100:5.1f}–{hi*100:5.1f}%   {n:3d}' + ('  *' if assumed else ''))
if any(x[5] for x in series):
    print("\n* for most trades the data don't settle who paid the up-front; the buyer-paid reading is shown")
