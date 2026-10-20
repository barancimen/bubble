#!/usr/bin/env python3
"""Check the dashboard's CDS estimates against published levels.

Usage:
    python3 cds_check.py ORCL 2026-10-15            # estimate from trades in the 4 days to that date
    python3 cds_check.py NVDA 2026-07-29 7          # ...or the N days to that date
    python3 cds_check.py --tweet 2038023871690100765  # date an X post from its status ID

Uses the same method as the dashboard (cds_signal.py) and reads only the cached
DTCC files in dtcc_cache/; it downloads nothing. Published levels are usually
5-year spreads in bp, so compare the spread it prints. Any level in
published_levels.csv within a day of the date is printed alongside.
"""
import math
import statistics as st
import sys
from datetime import date, datetime, timedelta, timezone

import cds_signal as c


def spread_bp(pd5):
    """The 5-year par spread (bp) that implies a 5-year default probability pd5."""
    return c.par_spread(-math.log(1 - pd5) / c.HORIZON) * 1e4


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--tweet":
        ms = (int(args[1]) >> 22) + 1288834974657  # X/Twitter snowflake IDs encode the post time
        print(f"{datetime.fromtimestamp(ms / 1000, timezone.utc):%Y-%m-%d %H:%M} UTC")
        return
    if len(args) not in (2, 3):
        sys.exit(__doc__)
    ticker, end = args[0].upper(), date.fromisoformat(args[1])
    days = int(args[2]) if len(args) == 3 else 4
    today = date.today()
    pts = c.trade_pds(c.scan(today)[ticker], today - timedelta(days=c.WINDOW_DAYS))
    window = [x for x in pts if end - timedelta(days=days - 1) <= x[0] <= end]
    if not window:
        print(f"{ticker}: no usable trades in the {days} days to {end}; widen the window")
        return
    settled = [p for _, p, ok, _ in window if ok]
    open_ = [(p, alt) for _, p, ok, alt in window if not ok]
    head = f"{ticker} {end - timedelta(days=days - 1)}..{end}: "
    if settled:
        med = st.median(settled)
        print(head + f"5y default probability {med * 100:.1f}% (~{spread_bp(med):.0f}bp), "
              f"range {min(settled) * 100:.1f}-{max(settled) * 100:.1f}%, n={len(settled)}")
    if open_:
        hi, lo = st.median(p for p, _ in open_), st.median(a for _, a in open_)
        print(head + f"{len(open_)} trade(s) where the data don't say who paid the up-front: "
              f"~{spread_bp(lo):.0f}bp if the sellers paid, ~{spread_bp(hi):.0f}bp if the buyers did")
    for r in c.published_levels().get(ticker, []):
        if abs((r["date"] - end).days) <= 1:
            print(f"   published {r['date']}: {r['bp']:,.1f}bp ({r['source']}; {r['role']})")


if __name__ == "__main__":
    main()
