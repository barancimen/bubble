"""Market-implied 5-year default probabilities from single-name CDS trades.

Trade prints come from DTCC's free public security-based swap dissemination
(SEC Regulation SBSR). Daily files are cached in dtcc_cache/ next to this file (shared with
cds.py), so each run downloads only the days it hasn't seen.
Standard library only.

Method: for each fresh standard-coupon trade with 4-6 years left, solve for the
constant default rate (hazard) that prices its up-front payment, assuming 40%
recovery and a flat 3.75% risk-free rate; 5-year default probability is then
1 - exp(-5 * hazard). Trades are pooled by month: median, with a 90% bootstrap
interval. Used by the AI bubble dashboard (../bubble_watch.py).
"""
import csv
import io
import json
import math
import os
import random
import statistics as st
import urllib.request
import zipfile
from datetime import date, timedelta
from html import escape as esc

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "dtcc_cache")  # shared with cds.py
MISSING = os.path.join(HERE, "dtcc_missing.json")  # weekdays with no file (holidays)
URL = "https://pddata.dtcc.com/ppd/api/report/cumulative/sec/SEC_CUMULATIVE_CREDITS_{d}.zip"

NAMES = {"CRWV": "COREWEAVE", "ORCL": "ORACLE", "NVDA": "NVIDIA"}  # ticker -> reference entity
WINDOW_DAYS = 730          # charts span the last 24 months
R, RATE = 0.4, 0.0375      # assumed recovery rate, flat risk-free rate
HORIZON = 5                # years


# ---------- data ----------

def _path(day):
    return os.path.join(CACHE, f"SEC_CUMULATIVE_CREDITS_{day:%Y_%m_%d}.zip")


def sync(today):
    """Downloads any missing daily files in the window. Older files are kept: the cache is shared."""
    os.makedirs(CACHE, exist_ok=True)
    try:
        with open(MISSING) as f:
            missing = set(json.load(f))
    except (OSError, ValueError):
        missing = set()
    start = today - timedelta(days=WINDOW_DAYS)
    day = start
    while day < today:
        if day.weekday() < 5 and not os.path.exists(_path(day)) and day.isoformat() not in missing:
            req = urllib.request.Request(URL.format(d=f"{day:%Y_%m_%d}"),
                                         headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    blob = r.read()
            except OSError:
                blob = b""
            if blob.startswith(b"PK"):
                with open(_path(day), "wb") as f:
                    f.write(blob)
            elif day < today - timedelta(days=7):  # recent days may just not be published yet
                missing.add(day.isoformat())
        day += timedelta(days=1)
    with open(MISSING, "w") as f:
        json.dump(sorted(d for d in missing if d >= start.isoformat()), f)


def scan(today):
    """{ticker: [row dict]} for every cached day in the window, each row tagged with file_date."""
    start = today - timedelta(days=WINDOW_DAYS)
    hits = {t: [] for t in NAMES}
    for name in sorted(os.listdir(CACHE)):
        if not (name.startswith("SEC_CUMULATIVE_CREDITS_") and name.endswith(".zip")):
            continue
        y, m, d = name[len("SEC_CUMULATIVE_CREDITS_"):-len(".zip")].split("_")
        day = date(int(y), int(m), int(d))
        if day < start:
            continue
        with zipfile.ZipFile(os.path.join(CACHE, name)) as z:
            for member in z.namelist():
                if not member.lower().endswith(".csv"):
                    continue
                reader = csv.reader(io.StringIO(z.read(member).decode("utf-8", "replace")))
                header = next(reader)
                for row in reader:
                    # Name columns have shifted across DTCC schema versions, so search the whole row.
                    text = ",".join(row).upper()
                    for ticker, needle in NAMES.items():
                        if needle in text:
                            hits[ticker].append(dict(zip(header, row), file_date=day.isoformat()))
    return hits


def _last_coupon_date(d):
    for y in (d.year, d.year - 1):
        for m in (12, 9, 6, 3):
            if date(y, m, 20) < d:
                return date(y, m, 20)


def _flat_upfront(h, T, coupon):
    """Protection leg minus running-coupon premium leg at constant hazard h, monthly grid."""
    n = max(1, int(T * 12)); dt = T / n; prot = prem = 0
    for i in range(1, n + 1):
        t = i * dt; df = math.exp(-RATE * t)
        s0, s1 = math.exp(-h * (t - dt)), math.exp(-h * t)
        prot += (1 - R) * df * (s0 - s1); prem += coupon * dt * df * (s0 + s1) / 2
    return prot - prem


def _implied_hazard(u, T, coupon):
    lo, hi = 0.0, 2.0
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _flat_upfront(mid, T, coupon) < u else (lo, mid)
    return mid


def resolve(rows, start, lo=4, hi=6):
    """One ticker's raw rows -> priced trades with lo <= years left <= hi, as dicts: trade and
    file_date (dates), maturity, T (years left), coupon, upfront (signed clean up-front, fraction
    of notional), hazard (flat default rate pricing the trade), anchored (direction from a quote)."""
    errored = {r.get("Original Dissemination Identifier") for r in rows if r.get("Action type") == "EROR"}
    latest = {}
    for r in sorted(rows, key=lambda r: r["file_date"]):
        if r.get("Action type") not in ("NEWT", "CORR", "MODI") or r.get("Fixed rate-Leg 1") not in ("0.01", "0.05"):
            continue
        key = r.get("Original Dissemination Identifier") or r.get("Dissemination Identifier")
        if key in errored or r.get("Dissemination Identifier") in errored:
            continue
        latest[key] = r  # corrections/modifications overwrite originals
    trades = []
    for r in latest.values():
        try:
            trade = date.fromisoformat(r["Execution Timestamp"][:10])
            eff = date.fromisoformat(r["Effective Date"])
            mat = date.fromisoformat(r["Expiration Date"])
            coupon = float(r["Fixed rate-Leg 1"])
        except (KeyError, ValueError):
            continue
        # Files also carry lifecycle events on years-old trades, priced back then.
        if trade < start - timedelta(days=7):
            continue
        T = (mat - trade).days / 365.25
        if not lo <= T <= hi:
            continue
        base = dict(trade=trade, file_date=date.fromisoformat(r["file_date"]), maturity=mat, T=T, coupon=coupon)
        trades.append((r, eff, base))

    # Some trades report a quoted spread instead of an up-front payment. They price directly
    # (up-front of zero at a coupon equal to the spread), whatever the trade size.
    quotes = []  # (base, spread, hazard)
    upfronts = []  # (base, |reported up-front|, accrued), as fractions of notional
    for r, eff, base in trades:
        try:
            s = float(r.get("Spread-Leg 1") or "nan")
        except ValueError:
            s = math.nan
        if 0.001 <= s <= 0.5:  # a few prints carry values in other units; skip those
            quotes.append((base, s, _implied_hazard(0, base["T"], s)))
            continue
        notional, upfront = r.get("Notional amount-Leg 1", ""), r.get("Other payment amount", "")
        if not notional or "+" in notional or not upfront:  # "5,000,000+" hides the real size
            continue
        try:
            n, paid = float(notional.replace(",", "")), abs(float(upfront))
        except ValueError:
            continue
        accrued = base["coupon"] * (eff - _last_coupon_date(eff)).days / 360  # reported upfront includes it
        upfronts.append((base, paid / n, accrued))

    # DTCC reports an up-front's size but not who paid it. When the spread is above the coupon
    # the buyer pays; below it (e.g. Nvidia at ~40-90bp against a 100bp coupon) the seller does.
    # The direction can only change when the spread crosses the coupon, which shows up as
    # up-fronts near zero. So a quoted spread fixes the direction for trades on either side of
    # it, up to the first day that looks like a crossing; past that, with no quote, assume the
    # buyer pays and say so. Keep lo..hi narrow: a 1-year and a 5-year contract can sit on
    # opposite sides of the coupon.
    by_day = {}
    for base, paid, accrued in upfronts:
        by_day.setdefault(base["trade"], []).append(abs(paid - accrued) / base["T"])
    crossings = sorted(d for d, v in by_day.items() if st.median(v) < 0.001)  # within 10bp/yr

    def quote_side(day, coupon):
        usable = [q for q in quotes
                  if not any(min(q[0]["trade"], day) < c < max(q[0]["trade"], day) for c in crossings)]
        if not usable:
            return None
        q = min(usable, key=lambda q: abs((q[0]["trade"] - day).days))
        return 1 if q[1] > coupon else -1

    out = [dict(base, upfront=_flat_upfront(h, base["T"], base["coupon"]), hazard=h, anchored=True)
           for base, _, h in quotes]
    for base, paid, accrued in upfronts:
        side = quote_side(base["trade"], base["coupon"])
        u = (side or 1) * paid - accrued
        out.append(dict(base, upfront=u, hazard=_implied_hazard(u, base["T"], base["coupon"]),
                        anchored=side is not None))
    return out


def trade_pds(rows, start):
    """[(trade date, 5y default probability, direction anchored by a quote)] from near-5y contracts
    only (4-6 years left), so the curve's slope doesn't bias the estimate."""
    return sorted((t["trade"], 1 - math.exp(-HORIZON * t["hazard"]), t["anchored"])
                  for t in resolve(rows, start, 4, 6))


def months(start, end):
    m = start.replace(day=1)
    while m <= end:
        yield m
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)


def monthly(pts, start, end):
    """[(month, median, ci_lo, ci_hi, n, assumed)] for every month in the window (median None when
    the month has no trades); assumed is True when most trades' up-front direction was assumed."""
    by_month, anchored = {}, {}
    for d, p, a in pts:
        by_month.setdefault(d.replace(day=1), []).append(p)
        anchored.setdefault(d.replace(day=1), []).append(a)
    rng = random.Random(0)
    out = []
    for m in months(start, end):
        v = by_month.get(m)
        if not v:
            out.append((m, None, None, None, 0, False))
            continue
        boots = sorted(st.median(rng.choices(v, k=len(v))) for _ in range(2000))
        assumed = sum(anchored[m]) < len(v) / 2
        out.append((m, st.median(v), boots[100], boots[1899], len(v), assumed))
    return out


def series(today):
    """{ticker: monthly series} after syncing the cache."""
    start = today - timedelta(days=WINDOW_DAYS)
    sync(today)
    return {t: monthly(trade_pds(rows, start), start, today) for t, rows in scan(today).items()}


# ---------- chart ----------

W, H = 960, 240
ML, MR, MT, MB = 52, 64, 12, 28  # margins: left PD axis, right price axis, top, bottom month labels
PD_COLOR, PRICE_COLOR = "#2a78d6", "#8f8d87"


def _nice_pd_max(v):
    for step in (0.01, 0.02, 0.05, 0.1, 0.2):
        top = math.ceil(v / step) * step
        if top / step <= 6:
            return top, step
    return 1.0, 0.2


def _log_ticks(lo, hi):
    ticks = []
    for k in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1):
        for m in (1, 2, 5):
            v = m * 10 ** k
            if lo <= v <= hi:
                ticks.append(v)
    if len(ticks) < 3:  # narrow ranges get the in-between steps too
        ticks = sorted({m * 10 ** k for k in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1)
                        for m in (1, 1.5, 2, 3, 5, 7) if lo <= m * 10 ** k <= hi})
    return ticks


def _money(v):
    return f"${v:,.0f}" if v >= 10 else f"${v:g}"


def svg_chart(ticker, data, closes, start, end):
    """One ticker: monthly 5y default probability (left axis, from 0%) with the stock's daily
    close on a log scale (right axis). Every chart gets the same start/end, so they line up."""
    pw, ph = W - ML - MR, H - MT - MB
    span = (end - start).days
    x = lambda d: ML + (d - start).days / span * pw
    have = [s for s in data if s[1] is not None]
    top, step = _nice_pd_max(max([s[3] for s in have] + [0.01]))
    y = lambda p: MT + ph - p / top * ph

    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'aria-label="{esc(ticker)} 5-year default probability by month, with stock price" '
           'style="font:11px system-ui,sans-serif;display:block">']
    # PD grid and left labels
    for i in range(round(top / step) + 1):
        p = i * step; yy = y(p)
        out.append(f'<line x1="{ML}" x2="{W - MR}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#e6e5e1"/>')
        out.append(f'<text x="{ML - 6}" y="{yy + 4:.1f}" text-anchor="end" fill="#52514e">{p * 100:.0f}%</text>')
    # month labels, every third month
    for i, (m, *_rest) in enumerate(data):
        if i % 3 == 0:
            xx = x(m + timedelta(days=15))
            out.append(f'<text x="{xx:.1f}" y="{H - 8}" text-anchor="middle" fill="#52514e">{m:%b %y}</text>')

    # stock price, log scale, right axis
    closes = [(d, c) for d, c in closes if start <= d <= end and c > 0]
    if closes:
        lo, hi = min(c for _, c in closes), max(c for _, c in closes)
        llo, lhi = math.log(lo / 1.05), math.log(hi * 1.05)
        yp = lambda c: MT + ph - (math.log(c) - llo) / (lhi - llo) * ph
        pts = " ".join(f"{x(d):.1f},{yp(c):.1f}" for d, c in closes)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{PRICE_COLOR}" stroke-width="1.5"/>')
        for v in _log_ticks(lo / 1.05, hi * 1.05):
            out.append(f'<text x="{W - MR + 6}" y="{yp(v) + 4:.1f}" fill="#52514e">{_money(v)}</text>')

    # PD: median line (broken where a month has no trades), 90% interval, dots with tooltips
    run = []
    for m, med, lo, hi, n, _ in data + [(None, None, None, None, 0, False)]:
        if med is None:
            if len(run) > 1:
                out.append(f'<polyline points="{" ".join(run)}" fill="none" stroke="{PD_COLOR}" stroke-width="2"/>')
            run = []
            continue
        xx = x(m + timedelta(days=15))
        run.append(f"{xx:.1f},{y(med):.1f}")
    for m, med, lo, hi, n, assumed in have:
        xx = x(m + timedelta(days=15))
        out.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{y(hi):.1f}" y2="{y(lo):.1f}" '
                   f'stroke="{PD_COLOR}" stroke-width="2" stroke-linecap="round" opacity="0.45"/>')
        fill, ring = ("#fff", PD_COLOR) if assumed else (PD_COLOR, "#fff")
        note = "; payment direction assumed, no recent quote" if assumed else ""
        out.append(f'<circle cx="{xx:.1f}" cy="{y(med):.1f}" r="4" fill="{fill}" stroke="{ring}" stroke-width="2">'
                   f'<title>{m:%b %Y}: {med * 100:.1f}% (90% interval {lo * 100:.1f}–{hi * 100:.1f}%, '
                   f'{n} trade{"s" * (n != 1)}{note})</title></circle>')
    if not have:
        out.append(f'<text x="{ML + pw / 2}" y="{MT + ph / 2}" text-anchor="middle" fill="#52514e">'
                   'No usable CDS trades in this window</text>')
    out.append("</svg>")
    return "".join(out)
