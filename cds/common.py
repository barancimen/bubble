"""Shared CDS helpers for pd.py and pd_history.py: load a ticker's trades and price CDS legs.
Trades are read and priced by cds_signal.py, the same code the dashboard uses."""
import csv, math
from datetime import date
from pathlib import Path

import cds_signal
from cds_signal import R, RATE

HERE = Path(__file__).resolve().parent
# Years-left bands; each up-front's direction is settled within its band, since contracts of
# very different lengths can sit on opposite sides of the coupon.
BANDS = [(0, 2), (2, 4), (4, 6), (6, 30)]


def load_trades(ticker, bands=BANDS):
    """Priced trades from <ticker>/trades.csv as dicts (see cds_signal.resolve): trade, file_date,
    maturity, T, coupon, upfront (signed, clean), hazard, anchored."""
    rows = list(csv.DictReader(open(HERE / ticker.lower() / 'trades.csv')))
    start = min(date.fromisoformat(r['file_date']) for r in rows)  # the pulled window
    return [t for lo, hi in bands for t in cds_signal.resolve(rows, start, lo, hi)]


def upfront(surv, T, coupon):
    """Protection leg minus running-coupon premium leg, monthly grid, for survival function surv."""
    n = max(1, int(T * 12)); dt = T / n; prot = prem = 0
    for i in range(1, n + 1):
        t = i * dt; df = math.exp(-RATE * t); s0, s1 = surv(t - dt), surv(t)
        prot += (1 - R) * df * (s0 - s1); prem += coupon * dt * df * (s0 + s1) / 2
    return prot - prem


def solve(f, target, lo=0.0, hi=2.0):
    """Hazard h in [lo, hi] with f(h) == target, f increasing."""
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < target else (lo, mid)
    return mid
