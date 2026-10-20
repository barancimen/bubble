"""Market-implied 5-year default probabilities from single-name CDS trades.

Trade prints come from DTCC's free public security-based swap dissemination
(SEC Regulation SBSR). Daily files are cached in dtcc_cache/ next to this file (shared with
cds.py), so each run downloads only the days it hasn't seen.
Standard library only.

Method: for each fresh US-dollar standard-coupon trade on the company's own credit with
4.5-5.5 years left (legs of credit-index trades are dropped), solve for the constant
default rate (hazard) that prices it, assuming 40% recovery and a flat 3.75% risk-free
rate; 5-year default probability is then 1 - exp(-5 * hazard). A trade prices from its
quoted spread, or from its up-front payment: DTCC reports that as net cash (the clean
up-front minus the accrued coupon the seller hands back) without saying who paid it,
which is worked out from quotes and from the way prices behave across coupon dates
(see _sides). Trades are pooled by month: median, with a 90% interval from resampling
whole trading days. The latest month is also priced at 30% and 50% recovery, since the
result is sensitive to that assumption. Used by the AI bubble dashboard
(../bubble_watch.py); cds/CDS_VALIDATION.md checks it against published levels.
"""
import bisect
import csv
import io
import json
import math
import os
import random
import re
import statistics as st
import urllib.error
import urllib.request
import zipfile
from collections import Counter, namedtuple
from datetime import date, timedelta
from html import escape as esc

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "dtcc_cache")  # shared with cds.py
MISSING = os.path.join(HERE, "dtcc_missing.json")  # weekdays with no file (holidays)
PUBLISHED = os.path.join(HERE, "published_levels.csv")  # levels reported in the press
URL = "https://pddata.dtcc.com/ppd/api/report/cumulative/sec/SEC_CUMULATIVE_CREDITS_{d}.zip"

# How DTCC identifies each reference entity: its RED code (first six characters of a RED pair code),
# the ISIN of its standard reference bond, and a name to fall back on. Another entity's RED code
# (e.g. ORACLE SYSTEMS CORP, 6EC42I) is never matched, whatever its name says.
ENTITIES = {
    "CRWV": dict(name="COREWEAVE", red="239FAV", isins={"US21873SAC26"}),
    "ORCL": dict(name="ORACLE", red="6EC42L", isins={"US68389XBN49"}),
    "NVDA": dict(name="NVIDIA", red="6D76CM", isins={"US67066GAN43"}),
}
NAMES = {t: e["name"] for t, e in ENTITIES.items()}  # ticker -> reference entity
WINDOW_DAYS = 730          # charts span the last 24 months
R, RATE = 0.4, 0.0375      # assumed recovery rate, flat risk-free rate
HORIZON = 5                # years
TENOR = (4.5, 5.5)         # years left on the contracts used: the 5-year point, not the 4-year one
RECOVERY_RANGE = (0.3, 0.5)  # shown alongside R so the recovery assumption is visible
NAME_COLUMNS = ("Underlying Asset Name", "UPI Underlier Name")
ID_COLUMN, ID_SOURCE_COLUMN = "Underlier ID-Leg 1", "Underlier ID source-Leg 1"
CURRENCY_COLUMN = "Notional currency-Leg 1"
RESCALE_DAYS, RESCALE_SLACK = 10, 1.25  # see _rescaled_spread
INDEX_LEG_COPIES = 20      # a payment repeated on this many lines of one file is an index trade
IMM_PRE, IMM_POST = 5, 4   # days before/after a coupon date compared by _imm_tests
EVIDENCE_GAP = 30          # days within which direction evidence counts as close
MIN_DAYS = 3               # months with fewer trading days are shown as thin
OUTLIER_RATIO, OUTLIER_DAYS = 3, 5  # see _drop_outliers


# ---------- data ----------

def _path(day):
    return os.path.join(CACHE, f"SEC_CUMULATIVE_CREDITS_{day:%Y_%m_%d}.zip")


def _save(day, blob):
    """Writes a downloaded day to the cache only if it is a readable zip, via a temp file so a
    crash can't leave a truncated file behind. Bad downloads are dropped and retried next run."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            if z.testzip() is not None:
                return
    except zipfile.BadZipFile:
        return
    tmp = _path(day) + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, _path(day))


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
            except urllib.error.HTTPError as e:
                # Only a 404 means "no file for this day"; recent days may just not be out yet.
                if e.code == 404 and day < today - timedelta(days=7):
                    missing.add(day.isoformat())
                blob = None  # anything else (5xx, throttling) is retried on the next run
            except OSError:
                blob = None  # timeouts and network errors: retry on the next run
            if blob is not None:
                _save(day, blob)
        day += timedelta(days=1)
    tmp = MISSING + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(d for d in missing if d >= start.isoformat()), f)
    os.replace(tmp, MISSING)


def _entity(row, idx):
    """The ticker a row's reference entity belongs to, or None."""
    uid = row[idx[ID_COLUMN]].strip().upper() if ID_COLUMN in idx and idx[ID_COLUMN] < len(row) else ""
    src = row[idx[ID_SOURCE_COLUMN]].strip().upper() if ID_SOURCE_COLUMN in idx and idx[ID_SOURCE_COLUMN] < len(row) else ""
    if uid and src == "REDID":
        return next((t for t, e in ENTITIES.items() if uid[:6] == e["red"]), None)
    if uid and src == "ISIN":
        hit = next((t for t, e in ENTITIES.items() if uid in e["isins"]), None)
        if hit:
            return hit
    elif uid and src:  # loan and other identifiers (e.g. CoreWeave's DDTL facilities) aren't CDS on the entity
        return None
    names = " | ".join(row[idx[c]] for c in NAME_COLUMNS if c in idx and idx[c] < len(row)).upper()
    return next((t for t, e in ENTITIES.items() if e["name"] in names), None)


def scan(today):
    """{ticker: [row dict]} for every cached day in the window, each row tagged with file_date and
    index_leg (True when the row is one company's leg of a credit-index trade)."""
    start = today - timedelta(days=WINDOW_DAYS)
    hits = {t: [] for t in ENTITIES}
    needles = [n for e in ENTITIES.values() for n in (e["name"], e["red"], *e["isins"])]
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
                text = z.read(member).decode("utf-8", "replace")
                lines = text.splitlines()
                header = next(csv.reader(lines[:1]))
                idx = {c: i for i, c in enumerate(header)}
                if any(line.count('"') % 2 for line in lines):  # a quoted field spans lines
                    candidates = csv.reader(lines[1:])
                else:  # parse only the lines that mention a name or an identifier (~1 in 100)
                    candidates = csv.reader(l for l in lines[1:] if any(n in l.upper() for n in needles))
                found = []
                for row in candidates:
                    ticker = _entity(row, idx)
                    if ticker:
                        found.append((ticker, dict(zip(header, row), file_date=day.isoformat())))
                # An index trade (e.g. CDX) is disseminated as one leg per company, each with the
                # same payment: a payment repeated across many lines is an index leg, not a price
                # for this company's own credit.
                pays = {r.get("Other payment amount") for _, r in found} - {"", None}
                copies = Counter(re.findall("|".join(re.escape(f'"{p}"') for p in pays), text)) if pays else {}
                for ticker, r in found:
                    r["index_leg"] = copies.get(f'"{r.get("Other payment amount")}"', 0) >= INDEX_LEG_COPIES
                    hits[ticker].append(r)
    return hits


def _imm(y, m):
    """Standard CDS coupon date: the 20th of Mar/Jun/Sep/Dec, moved to Monday if on a weekend."""
    d = date(y, m, 20)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _accrual_start(stepin):
    """The coupon date on or before the step-in date (trade date + 1), where accrual restarts."""
    for y in (stepin.year, stepin.year - 1):
        for m in (12, 9, 6, 3):
            if _imm(y, m) <= stepin:
                return _imm(y, m)


def _accrued(trade, coupon):
    """Accrued coupon at the trade, as a fraction of notional (actual/360 to the step-in date)."""
    stepin = trade + timedelta(days=1)
    return coupon * (stepin - _accrual_start(stepin)).days / 360


def _legs(h, T, rec=R):
    """(protection leg, risky annuity) at constant hazard h, monthly grid."""
    n = max(1, int(T * 12)); dt = T / n; prot = ann = 0
    for i in range(1, n + 1):
        t = i * dt; df = math.exp(-RATE * t)
        s0, s1 = math.exp(-h * (t - dt)), math.exp(-h * t)
        prot += (1 - rec) * df * (s0 - s1); ann += dt * df * (s0 + s1) / 2
    return prot, ann


def _flat_upfront(h, T, coupon, rec=R):
    """Clean up-front (fraction of notional) of a contract paying coupon, at constant hazard h."""
    prot, ann = _legs(h, T, rec)
    return prot - coupon * ann


def _implied_hazard(u, T, coupon, rec=R):
    lo, hi = 0.0, 2.0
    for _ in range(32):  # ~5e-10 hazard precision, far below anything that shows
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _flat_upfront(mid, T, coupon, rec) < u else (lo, mid)
    return mid


def par_spread(h, T=HORIZON, rec=R):
    """The running spread that prices a contract at zero up-front at hazard h (for 5 years by
    default): what press reports call a company's CDS spread."""
    prot, ann = _legs(h, T, rec)
    return prot / ann


def _rescaled_spread(s, day, valid):
    """Some prints carry the spread 100x too small (0.00045 for 450bp). Returns s * 100 when that
    lands inside the band of properly reported quotes within RESCALE_DAYS of the trade (widened
    by RESCALE_SLACK), else None: with no nearby quote to confirm it, the print is not used."""
    if not 0 < s < 0.001:
        return None
    near = [q for d, q in valid if abs((d - day).days) <= RESCALE_DAYS]
    if near and min(near) / RESCALE_SLACK <= s * 100 <= max(near) * RESCALE_SLACK:
        return s * 100
    return None


def _spread_bp(side, u, rec):
    """Par spread (bp) of an up-front trade u = (base, |net cash|, accrued) if side paid the net cash."""
    base, paid, acc = u
    return par_spread(_implied_hazard(side * paid + acc, base["T"], base["coupon"], rec), base["T"], rec) * 1e4


def _imm_tests(ups, rec):
    """{coupon date: side} for one coupon's up-front trades, where the data decide it.
    DTCC reports the net cash (the clean up-front minus the accrued coupon the seller hands back)
    without saying who paid it. Accrual restarts on each coupon date: read the right way, prices
    run straight through it; read the wrong way they jump by about twice the accrued coupon. The
    last few days before and the first few after are compared, so that real market moves stay
    small, and a test decides only if one reading jumps at most 2/3 as much as the other."""
    out = {}
    if not ups:
        return out
    days = sorted(u[0]["trade"] for u in ups)
    y, m = days[0].year, days[0].month
    while (y, m) <= (days[-1].year, days[-1].month):
        if m % 3 == 0:
            d0 = _imm(y, m)
            stepin = lambda u: u[0]["trade"] + timedelta(days=1)
            pre = [u for u in ups if d0 - timedelta(days=IMM_PRE) <= stepin(u) < d0]
            post = [u for u in ups if d0 <= stepin(u) <= d0 + timedelta(days=IMM_POST)]
            if len(pre) >= 2 and len(post) >= 2:
                jump = {side: abs(st.median(_spread_bp(side, u, rec) for u in post)
                                  - st.median(_spread_bp(side, u, rec) for u in pre)) for side in (1, -1)}
                small, big = sorted(jump, key=jump.get)
                if jump[small] <= 2 / 3 * jump[big]:
                    out[d0] = small
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _sides(quotes, ups, coupon, rec):
    """[side] for one coupon's up-front trades: +1 if the buyer paid the net cash, -1 if the seller
    did, None if the data don't say. A trade where only one reading is possible is settled by
    that. Evidence for the others: quoted spreads (whoever pays the net cash at that spread) and
    coupon-date tests; a quote within EVIDENCE_GAP days overrules a test that disagrees with it. Evidence on both sides of a trade that agrees settles it; where it
    disagrees, the side flips on the day the net cash came closest to zero. A trade with evidence
    on one side only is settled if that evidence is within EVIDENCE_GAP days."""
    # A reading that needs a negative default rate (or more than certain default) is impossible,
    # which settles that trade. It isn't evidence for others: a garbled print looks the same.
    forced = {}
    for k, (base, paid, acc) in enumerate(ups):
        if acc - paid <= _flat_upfront(0.0, base["T"], coupon, rec):
            forced[k] = 1
        elif paid + acc >= _flat_upfront(2.0, base["T"], coupon, rec):
            forced[k] = -1
    evidence = []  # (date, side)
    for base, s in quotes:
        u_ref = _flat_upfront(_implied_hazard(0, HORIZON, s, rec), HORIZON, coupon, rec)
        evidence.append((base["trade"], 1 if u_ref - _accrued(base["trade"], coupon) > 0 else -1))
    quoted = list(evidence)
    for d0, side in _imm_tests(ups, rec).items():
        if not any(abs((d0 - d).days) <= EVIDENCE_GAP and qs != side for d, qs in quoted):
            evidence.append((d0, side))
    evidence.sort()
    if not evidence:
        return [forced.get(k) for k in range(len(ups))]
    ev_days = [d for d, _ in evidence]
    by_day = {}
    for base, paid, _ in ups:
        by_day.setdefault(base["trade"], []).append(paid / base["T"])
    net = {d: st.median(v) for d, v in by_day.items()}  # net cash per year of maturity
    out = []
    for k, (base, _, _) in enumerate(ups):
        if k in forced:
            out.append(forced[k])
            continue
        day = base["trade"]
        i = bisect.bisect_right(ev_days, day)  # evidence[i - 1] is on or before the trade
        before = evidence[i - 1] if i else None
        after = evidence[i] if i < len(evidence) else None
        if before and after and before[1] != after[1]:
            between = [d for d in net if before[0] < d < after[0]]
            flip = min(between, key=net.get) if between else after[0]
            out.append(before[1] if day < flip else after[1])
        elif before and after:
            out.append(before[1])
        else:
            ev = before or after
            out.append(ev[1] if abs((ev[0] - day).days) <= EVIDENCE_GAP else None)
    return out


def resolve(rows, start, lo=TENOR[0], hi=TENOR[1], rec=R, since=None):
    """One ticker's raw rows -> priced trades with lo <= years left <= hi, as dicts: trade and
    file_date (dates), maturity, T (years left), coupon, upfront (signed clean up-front, fraction
    of notional), hazard (flat default rate pricing the trade), anchored (False when the data don't
    say who paid the up-front: then hazard assumes the buyer paid and hazard_alt the seller).
    rec is the assumed recovery rate. since (a date) limits the output, and the pricing work, to
    trades on or after it; direction evidence still comes from the whole window."""
    errored = {r.get("Original Dissemination Identifier") for r in rows if r.get("Action type") == "EROR"}
    # Only fresh trades are prices: new trades (event TRAD) and corrections of them. Novations,
    # modifications and terminations carry fees on existing trades, not today's price.
    not_fresh = {r.get("Dissemination Identifier") for r in rows
                 if r.get("Action type") == "NEWT" and r.get("Event type", "TRAD") not in ("TRAD", "")}
    latest = {}
    for r in sorted(rows, key=lambda r: r["file_date"]):
        if r.get("Action type") not in ("NEWT", "CORR") or r.get("Fixed rate-Leg 1") not in ("0.01", "0.05"):
            continue
        if r.get("Dissemination Identifier") in not_fresh or r.get("Original Dissemination Identifier") in not_fresh:
            continue
        # EUR and blank-currency prints aren't comparable. Checked here rather than in scan, because
        # error cancellations (EROR) often have a blank currency and must still knock trades out.
        if r.get(CURRENCY_COLUMN, "USD") != "USD":
            continue
        key = r.get("Original Dissemination Identifier") or r.get("Dissemination Identifier")
        if key in errored or r.get("Dissemination Identifier") in errored:
            continue
        latest[key] = r  # corrections overwrite originals
    trades = []
    for r in latest.values():
        if r.get("index_leg") is True:  # one leg of an index trade: the index's price, not this name's
            continue
        try:
            trade = date.fromisoformat(r["Execution Timestamp"][:10])
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
        trades.append((r, base))

    # Some trades report a quoted spread instead of an up-front payment. They price directly
    # (up-front of zero at a coupon equal to the spread), whatever the trade size.
    def spread(r):
        try:
            return float(r.get("Spread-Leg 1") or "nan")
        except ValueError:
            return math.nan

    valid = [(base["trade"], spread(r)) for r, base in trades if 0.001 <= spread(r) <= 0.5]
    quotes = []  # (base, spread)
    upfronts = []  # (base, |reported net cash|, accrued), as fractions of notional
    for r, base in trades:
        s = spread(r)
        if not 0.001 <= s <= 0.5:  # a few prints carry values in other units
            s = _rescaled_spread(s, base["trade"], valid) or math.nan
        if 0.001 <= s <= 0.5:
            quotes.append((base, s))
            continue
        notional, upfront = r.get("Notional amount-Leg 1", ""), r.get("Other payment amount", "")
        if not notional or "+" in notional or not upfront:  # "5,000,000+" hides the real size
            continue
        if r.get("Other payment type", "UFRO") != "UFRO":  # unwind fees and mixed payments aren't prices
            continue
        try:
            n, paid = float(notional.replace(",", "")), abs(float(upfront))
        except ValueError:
            continue
        if paid == 0 or n == 0:  # a zero payment is a missing value, not a price
            continue
        upfronts.append((base, paid / n, _accrued(base["trade"], base["coupon"])))

    # Who paid the up-front isn't reported: the clean up-front is paid + accrued if the buyer paid
    # the net cash, accrued - paid if the seller did. _sides works it out per coupon where it can.
    side_of = {}
    for coupon in {base["coupon"] for base, *_ in upfronts}:
        group = [u for u in upfronts if u[0]["coupon"] == coupon]
        # Who paid is a fact about the trade, not about the recovery assumption: decide it at R.
        for u, sa in zip(group, _sides(quotes, group, coupon, R)):
            side_of[id(u)] = sa

    wanted = lambda base: since is None or base["trade"] >= since
    out = []
    for base, s in quotes:
        if wanted(base):
            h = _implied_hazard(0, base["T"], s, rec)
            out.append(dict(base, upfront=_flat_upfront(h, base["T"], base["coupon"], rec), hazard=h,
                            hazard_alt=h, anchored=True))
    for item in upfronts:
        base, paid, acc = item
        if not wanted(base):
            continue
        side = side_of[id(item)]
        u = (side or 1) * paid + acc
        h = _implied_hazard(u, base["T"], base["coupon"], rec)
        # Unresolved: keep the buyer-paid reading in upfront/hazard, and the seller-paid one too.
        alt = h if side else _implied_hazard(acc - paid, base["T"], base["coupon"], rec)
        out.append(dict(base, upfront=u, hazard=h, hazard_alt=alt, anchored=side is not None))
    return _drop_outliers(out)


def _drop_outliers(trades):
    """Drops a settled trade priced more than OUTLIER_RATIO times above or below the median of at
    least three other settled trades within OUTLIER_DAYS days: a garbled record, not a price."""
    settled = sorted((t["trade"], t["hazard"], i) for i, t in enumerate(trades) if t["anchored"])
    days = [d for d, _, _ in settled]
    bad = set()
    for d, h, i in settled:
        lo = bisect.bisect_left(days, d - timedelta(days=OUTLIER_DAYS))
        hi = bisect.bisect_right(days, d + timedelta(days=OUTLIER_DAYS))
        others = [x for _, x, j in settled[lo:hi] if j != i]
        if len(others) >= 3:
            m = st.median(others)
            if h > OUTLIER_RATIO * m or h * OUTLIER_RATIO < m:
                bad.add(i)
    return [t for i, t in enumerate(trades) if i not in bad]


def trade_pds(rows, start, rec=R, since=None):
    """[(trade date, 5y default probability, settled, alternative)] from 5-year contracts only (TENOR
    years left), so the curve's slope doesn't bias the estimate. When who paid the up-front isn't
    settled, the probability assumes the buyer paid and the alternative that the seller did;
    otherwise the two are equal."""
    pd = lambda h: 1 - math.exp(-HORIZON * h)
    return sorted((t["trade"], pd(t["hazard"]), t["anchored"], pd(t["hazard_alt"]))
                  for t in resolve(rows, start, *TENOR, rec, since))


def months(start, end):
    m = start.replace(day=1)
    while m <= end:
        yield m
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)


Month = namedtuple("Month", "month median lo hi n unresolved days mid last alt")


def monthly(pts, start, end):
    """[Month] for every month in the window (median None when the month has no trades).
    Settled months: median 5y default probability of the settled trades, with a 90% interval from
    resampling whole trading days (prints on one day are not independent), the number of distinct
    prints and of trading days, and the middle and last trade dates. A block trade split into
    identical pieces counts once. Unresolved months (most trades unsettled): median is the reading
    in which the buyers paid the up-fronts, alt the one in which the sellers did, lo/hi None."""
    by_month = {}
    for d, p, settled, alt in pts:
        by_month.setdefault(d.replace(day=1), set()).add((d, round(p, 6), settled, round(alt, 6)))
    rng = random.Random(0)
    out = []
    for m in months(start, end):
        trades = sorted(by_month.get(m, ()))
        if not trades:
            out.append(Month(m, None, None, None, 0, False, 0, None, None, None))
            continue
        dates = [d for d, *_ in trades]
        mid, last, ndays = dates[len(dates) // 2], dates[-1], len(set(dates))
        settled = [(d, p) for d, p, ok, _ in trades if ok]
        if len(settled) * 2 < len(trades):
            out.append(Month(m, st.median(p for _, p, _, _ in trades), None, None, len(trades), True,
                             ndays, mid, last, st.median(a for *_, a in trades)))
            continue
        per_day = {}
        for d, p in settled:
            per_day.setdefault(d, []).append(p)
        groups = list(per_day.values())
        boots = sorted(st.median([p for g in rng.choices(groups, k=len(groups)) for p in g]) for _ in range(2000))
        out.append(Month(m, st.median(p for _, p in settled), boots[100], boots[1899], len(settled), False,
                         len(per_day), mid, last, None))
    return out


def recovery_range(rows, start, month):
    """{recovery rate: median 5y default probability of the settled trades in month} for R and each
    RECOVERY_RANGE rate (None if none are settled)."""
    out = {}
    for rec in sorted({R, *RECOVERY_RANGE}):
        v = {(d, round(p, 6)) for d, p, ok, _ in trade_pds(rows, start, rec, since=month)
             if ok and d.replace(day=1) == month}
        out[rec] = st.median(p for _, p in v) if v else None
    return out


def series(today, download=True):
    """{ticker: ([Month], recovery_range for its latest month with trades, or {}, trade_pds)} after
    syncing the cache (download=False uses only the files already cached)."""
    start = today - timedelta(days=WINDOW_DAYS)
    if download:
        sync(today)
    out = {}
    for t, rows in scan(today).items():
        pts = trade_pds(rows, start)
        data = monthly(pts, start, today)
        have = [s.month for s in data if s.median is not None]
        out[t] = (data, recovery_range(rows, start, have[-1]) if have else {}, pts)
    return out


def estimate_near(pts, day, before=4, after=1):
    """Median 5-year spread (bp) of the settled trades from `before` days before day to `after`
    days after, and how many there were; (None, 0) if there are none."""
    v = {(d, p) for d, p, ok, _ in pts if ok and day - timedelta(days=before) <= d <= day + timedelta(days=after)}
    if not v:
        return None, 0
    return par_spread(-math.log(1 - st.median(p for _, p in v)) / HORIZON) * 1e4, len(v)


def published_levels():
    """{ticker: [dict(date, bp, source, url, note, role)]} from published_levels.csv: 5-year CDS
    levels reported in the press, with their sources. role is "design" for levels looked at while
    the method was built, "holdout" for ones found only after it was fixed."""
    out = {t: [] for t in ENTITIES}
    with open(PUBLISHED, newline="") as f:
        for r in csv.DictReader(f):
            if r["ticker"] in out:
                out[r["ticker"]].append(dict(date=date.fromisoformat(r["date"]), bp=float(r["spread_bp"]),
                                             source=r["source"], url=r["url"], note=r["note"], role=r["role"]))
    return out


# ---------- chart ----------

W, H = 960, 240
ML, MR, MT, MB = 64, 52, 12, 28  # margins: left price axis, right probability axis, top, bottom month labels
PD_COLOR, NEWS_COLOR, PRICE_COLOR, GRID, TEXT = "#2a78d6", "#eb6834", "#8f8d87", "#e6e5e1", "#52514e"


def nice_pd_max(v):
    """(top, step) of a probability axis reaching at least v, with at most ten steps."""
    for step in (0.01, 0.02, 0.05, 0.1, 0.2):
        top = math.ceil(max(v, 0.01) / step) * step
        if top / step <= 10:
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


def svg_chart(ticker, data, closes, start, end, top=None, reported=()):
    """One ticker: monthly 5y default probability from DTCC trades (right axis, from 0%, on the
    scale top = (max, step) shared by every company so they can be compared), levels reported in
    the news on the same axis (reported = [(date, probability, tooltip)]), and the stock's daily
    close on a log scale (left axis). Each month's dot sits at the middle of its trade dates;
    months with fewer than MIN_DAYS trading days are faded and not joined up; unresolved months
    show both readings as a band."""
    pw, ph = W - ML - MR, H - MT - MB
    span = (end - start).days
    x = lambda d: ML + (d - start).days / span * pw
    have = [s for s in data if s.median is not None]
    top, step = top or nice_pd_max(max([s.hi or s.median for s in have] + [p for _, p, _ in reported] + [0.01]))
    y = lambda p: MT + ph - min(p, top) / top * ph
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'aria-label="{esc(ticker)}: 5-year default probability by month from CDS trades and levels reported '
           f'in the news (right axis), with the stock price (left axis)" '
           'style="font:11px system-ui,sans-serif;display:block">']
    # default-probability grid and right labels
    for i in range(round(top / step) + 1):
        p = i * step; yy = y(p)
        out.append(f'<line x1="{ML}" x2="{W - MR}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{W - MR + 6}" y="{yy + 4:.1f}" fill="{TEXT}">{p * 100:.0f}%</text>')
    # a vertical line at each month start; month labels every second month
    for i, s in enumerate(data):
        m = s.month
        if m > start:
            out.append(f'<line x1="{x(m):.1f}" x2="{x(m):.1f}" y1="{MT}" y2="{MT + ph}" stroke="{GRID}"/>')
        mid = max(m, start) + (min(m + timedelta(days=31), end) - max(m, start)) / 2
        if i % 2 == 0 and x(mid) >= ML + 16 and x(mid) <= W - MR - 16:
            out.append(f'<text x="{x(mid):.1f}" y="{H - 8}" text-anchor="middle" fill="{TEXT}">{m:%b} ’{m:%y}</text>')

    # stock price, log scale, left axis
    closes = [(d, c) for d, c in closes if start <= d <= end and c > 0]
    if closes:
        lo, hi = min(c for _, c in closes), max(c for _, c in closes)
        llo, lhi = math.log(lo / 1.05), math.log(hi * 1.05)
        yp = lambda c: MT + ph - (math.log(c) - llo) / (lhi - llo) * ph
        pts = " ".join(f"{x(d):.1f},{yp(c):.1f}" for d, c in closes)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{PRICE_COLOR}" stroke-width="1.5"/>')
        for v in _log_ticks(lo / 1.05, hi * 1.05):
            out.append(f'<text x="{ML - 6}" y="{yp(v) + 4:.1f}" text-anchor="end" fill="{TEXT}">{_money(v)}</text>')

    def period(s):
        return (f"{s.month:%b} 1–{s.last.day}, {s.month:%Y}" if (s.last.year, s.last.month) == (end.year, end.month)
                else f"{s.month:%b %Y}")

    # DTCC estimate: a line through settled months with enough trading days, 90% interval, dots
    run = []
    for s in data + [Month(None, None, None, None, 0, False, 0, None, None, None)]:
        if s.median is None or s.unresolved or s.days < MIN_DAYS:
            if len(run) > 1:
                out.append(f'<polyline points="{" ".join(run)}" fill="none" stroke="{PD_COLOR}" stroke-width="2"/>')
            run = []
            continue
        run.append(f"{x(s.mid):.1f},{y(s.median):.1f}")
    for s in have:
        xx = x(s.mid)
        days = f"{s.n} trade{'s' * (s.n != 1)} on {s.days} day{'s' * (s.days != 1)}"
        thin = f"; only {s.days} trading day{'s' * (s.days != 1)}, treat with caution" if s.days < MIN_DAYS else ""
        if s.unresolved:
            out.append(f'<rect x="{xx - 5:.1f}" y="{y(s.median):.1f}" width="10" height="{max(y(s.alt) - y(s.median), 1):.1f}" '
                       f'rx="5" fill="{PD_COLOR}" opacity="0.18"><title>{period(s)}: who paid the up-fronts isn\'t '
                       f'settled by the data: {s.median * 100:.0f}% if the buyers paid them, {s.alt * 100:.0f}% if '
                       f'the sellers did ({days}{thin})</title></rect>')
            for v in (s.median, s.alt):
                out.append(f'<circle cx="{xx:.1f}" cy="{y(v):.1f}" r="3" fill="#fff" stroke="{PD_COLOR}" stroke-width="1.5"/>')
            continue
        opacity = ' opacity="0.45"' if s.days < MIN_DAYS else ""
        out.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{y(s.hi):.1f}" y2="{y(s.lo):.1f}" '
                   f'stroke="{PD_COLOR}" stroke-width="2" stroke-linecap="round" opacity="0.45"/>')
        out.append(f'<circle cx="{xx:.1f}" cy="{y(s.median):.1f}" r="4" fill="{PD_COLOR}" stroke="#fff" stroke-width="2"{opacity}>'
                   f'<title>{period(s)}: {s.median * 100:.1f}% (90% interval {s.lo * 100:.1f}–{s.hi * 100:.1f}%; '
                   f'{days}{thin})</title></circle>')
    # levels reported in the news: diamonds
    for d, p, tip in reported:
        if start <= d <= end:
            xx, yy = x(d), y(p)
            out.append(f'<path d="M{xx:.1f},{yy - 6:.1f}L{xx + 5:.1f},{yy:.1f}L{xx:.1f},{yy + 6:.1f}L{xx - 5:.1f},{yy:.1f}Z" '
                       f'fill="{NEWS_COLOR}" stroke="#fff" stroke-width="1.5"><title>{esc(tip)}</title></path>')
    if not have and not reported:
        out.append(f'<text x="{ML + pw / 2}" y="{MT + ph / 2}" text-anchor="middle" fill="{TEXT}">'
                   'No usable CDS trades in this window</text>')
    out.append("</svg>")
    return "".join(out)


def hazard_from_spread(s, T=HORIZON, rec=R):
    """Constant hazard whose par spread over T years is s (a fraction, e.g. 0.0104 for 104bp)."""
    return _implied_hazard(0, T, s, rec)


def pd_from_spread(s, T=HORIZON, rec=R):
    """5-year default probability implied by a 5-year CDS spread s, on this file's assumptions."""
    return 1 - math.exp(-HORIZON * hazard_from_spread(s, T, rec))
