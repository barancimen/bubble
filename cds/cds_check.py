#!/usr/bin/env python3
"""Check the dashboard's CDS estimates against published levels.

Usage:
    python3 cds_check.py ORCL 2026-10-15            # estimate from trades in the 4 days to that date
    python3 cds_check.py NVDA 2026-07-29 7          # ...or the N days to that date
    python3 cds_check.py --tweet 2038023871690100765  # date an X post from its status ID

Uses the same method as the dashboard (cds_signal.py) and reads only the cached
DTCC files in dtcc_cache/; it downloads nothing. Published levels are usually
5-year spreads in bp, so compare the spread it prints.
"""
import math
import statistics as st
import sys
from datetime import date, datetime, timedelta, timezone

import cds_signal as c


def spread_bp(pd5):
    """Flat-hazard approximation: spread = hazard x (1 - recovery)."""
    return -math.log(1 - pd5) / c.HORIZON * (1 - c.R) * 1e4


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
    window = [(d, p, a) for d, p, a in pts if end - timedelta(days=days - 1) <= d <= end]
    if not window:
        print(f"{ticker}: no usable trades in the {days} days to {end}; widen the window")
        return
    v = [p for _, p, _ in window]
    med = st.median(v)
    assumed = sum(not a for _, _, a in window)
    print(f"{ticker} {end - timedelta(days=days - 1)}..{end}: 5y default probability {med * 100:.1f}% "
          f"(~{spread_bp(med):.0f}bp), range {min(v) * 100:.1f}-{max(v) * 100:.1f}%, n={len(v)}, "
          f"{assumed} with payment direction assumed")


if __name__ == "__main__":
    main()
