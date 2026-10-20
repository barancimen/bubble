#!/usr/bin/env python3
"""Daily AI-bubble watch.

Fetches raw signals from free public sources and writes dashboard.html next
to this script. Each signal shows its own "start worrying" line; there is no
score. The signals follow Ares's Fall 2026 "In the Gaps" newsletter.

Run once a day, e.g. from cron at 15:00 UTC, after FRED has usually posted the day's spreads (the server's clock is UTC):
    0 15 * * * SEC_USER_AGENT="Your Name you@example.com" /usr/bin/python3 /root/bubble/bubble_watch.py

The SEC asks every client to identify itself with a name and contact email,
so set SEC_USER_AGENT. Headlines are classified by DeepSeek V4.1 Flash; the
API key is read from DEEPSEEK_API_KEY or .deepseek_api_key next to this
script. Standard library only; no installs needed.

    python3 bubble_watch.py --cached

rebuilds the page from the responses saved by the last full run, without any network or
DeepSeek calls: use it to check layout and wording changes in a few seconds.
"""
import csv
import functools
import hashlib
import html
import json
import math
import os
import re
import statistics as st
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cds"))  # CDS code and data live in ./cds
import cds_signal  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "dashboard.html")
PREVIEW = os.path.join(HERE, "dashboard-preview.html")  # what a --cached run writes
SEC_UA = os.environ.get("SEC_USER_AGENT", "bubble-watch admin@example.com")
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64)"

# Worry lines. Edit these to taste.
BBB_WIDENING_BP = 30      # BBB spread this far above its 1-year low
HY_WIDENING_BP = 100      # High-yield spread this far above its 1-year low
GPU_DROP_13W_PCT = 10     # GPU on-demand median falls this much over 13 weeks
GPU_START = date(2026, 2, 2)  # the price series became automated here; earlier weeks are hand-entered
GPU_AVG_WEEKS = 4         # changes compare averages over this many weeks
SEC_STALE_DAYS = 140      # a newest filed quarter older than this gets a warning

HYPERSCALERS = [  # ticker, SEC CIK, XBRL concept the company uses for capex
    ("MSFT", 789019, "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("GOOGL", 1652044, "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("AMZN", 1018724, "PaymentsToAcquireProductiveAssets"),
    ("META", 1326801, "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("ORCL", 1341439, "PaymentsToAcquirePropertyPlantAndEquipment"),
]
BACKLOG = [("ORCL", 1341439), ("MSFT", 789019), ("GOOGL", 1652044)]
NVDA_CIK = 1045810
COMMITMENTS = [  # ticker, CIK, concept, what the company's filings say the tagged figure is
    ("GOOGL", 1652044, "LongTermPurchaseCommitmentAmount", "Purchase commitments, mostly long-term supply agreements"),
    ("META", 1326801, "UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount",
     "Data-center and network leases signed but not yet started"),
    ("ORCL", 1341439, "UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount",
     "Purchase obligations for cloud components and power"),
    ("NVDA", 1045810, "UnrecordedUnconditionalPurchaseObligationBalanceSheetAmount",
     "Data-center leases signed but not yet started (tagged once a year)"),
]
WEAK_BORROWERS = ["CRWV", "IREN", "HUT", "CIFR", "WULF", "APLD", "ORCL"]
WEAK_BASKET_PCT, WEAK_EACH_PCT, QQQ_CALM_PCT = 20, 15, 5  # see block_weak_borrowers
CDS_NAMES = [("CRWV", "CoreWeave"), ("ORCL", "Oracle"), ("NVDA", "Nvidia")]
GPU_MODELS = [("nvidia-h100", "H100"), ("nvidia-h200", "H200"), ("nvidia-b200", "B200")]

# Headline classification. "deepseek-flash" is DeepSeek's unversioned name, so each run checks
# which model is behind it and pauses classification if it's no longer V4.1 Flash.
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_MODEL_NAME = "DeepSeek-V4.1-Flash"
DEEPSEEK_KEY_FILE = os.path.join(HERE, ".deepseek_api_key")
DEEPSEEK_TIMEOUT = 600
MAX_HEADLINES = 499       # across all feeds; Google News returns at most 100 per search
VISIBLE_HEADLINES = 6     # per feed; the rest fold away
HEADLINE_STORE = os.path.join(HERE, "data", "headlines.json")
STORE_DAYS = 90           # forget headlines not seen for this long


# ---------- fetching ----------
# Every full run saves each response in HTTP_CACHE; --cached replays them instead of fetching.

CACHED = "--cached" in sys.argv[1:]
HTTP_CACHE = os.path.join(HERE, "data", "http_cache")
FETCHED_AT = os.path.join(HTTP_CACHE, "fetched_at")


def _run_clock():
    """(today, now in UTC) for this run: the real clock, or in a --cached run the moment the saved
    responses were fetched, so that dated searches and date windows match the data."""
    if CACHED:
        try:
            with open(FETCHED_AT) as f:
                t = datetime.strptime(f.read().strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            return t.date(), t
        except (OSError, ValueError):
            pass
    now = datetime.now(timezone.utc)
    return now.date(), now


TODAY, NOW = _run_clock()
WARNINGS = []  # problems worth a reader's attention, shown at the top of the page and logged


def warn(text):
    WARNINGS.append(text)
    print(f"WARNING: {text}", file=sys.stderr)


def short_error(e):
    """An exception as one short line for the public page; the full text goes to the log."""
    text = re.sub(r"\b(sk|key|token|bearer)[-_ ][A-Za-z0-9_-]{8,}", "[redacted]", " ".join(str(e).split()), flags=re.I)
    return text[:160] + ("…" if len(text) > 160 else "")


def load_json_store(path):
    """A dict saved by an earlier run, or {} if there is none. A file that can't be read is set
    aside (renamed .corrupt-<time>) rather than silently replaced, and the run says so."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        raise ValueError("not a JSON object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if not CACHED:
            os.replace(path, f"{path}.corrupt-{NOW:%Y%m%d%H%M}")
        warn(f"{os.path.basename(path)} couldn't be read ({short_error(e)}); it was set aside and is being rebuilt")
        return {}


def save_json_store(path, data):
    """Writes a store atomically. A --cached run never writes: it only rebuilds the page."""
    if CACHED:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _cache_path(url):
    return os.path.join(HTTP_CACHE, hashlib.sha256(url.encode()).hexdigest())


def fetch(url, ua=WEB_UA, tries=3):
    if CACHED:
        try:
            with open(_cache_path(url), "rb") as f:
                return f.read()
        except OSError:
            raise RuntimeError(f"not saved by the last full run: {url}") from None
    req = urllib.request.Request(url, headers={"User-Agent": ua} if ua else {})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            break
        except OSError:  # covers URLError, HTTPError and timeouts
            if attempt == tries - 1:
                raise
            time.sleep(5)
    os.makedirs(HTTP_CACHE, exist_ok=True)
    tmp = _cache_path(url) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, _cache_path(url))
    return body


def fetch_json(url, ua=WEB_UA):
    return json.loads(fetch(url, ua))


def sec_concept(cik, concept):
    time.sleep(0.2)  # SEC allows at most 10 requests per second
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{concept}.json"
    return fetch_json(url, SEC_UA)["units"]["USD"]


def latest_filed(facts, key):
    best = {}
    for f in facts:
        k = key(f)
        if k not in best or f["filed"] > best[k]["filed"]:
            best[k] = f
    return best


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def consecutive(ends):
    """True when each quarter end follows the one before by one quarter (none missing)."""
    return all(80 <= days_between(a, b) <= 100 for a, b in zip(ends, ends[1:]))


def quarterly_flows(facts):
    """Quarterly values from flow facts. 10-Q cash-flow statements are
    year-to-date, so a quarter is the difference of two YTD figures."""
    facts = latest_filed([f for f in facts if "start" in f], lambda f: (f["start"], f["end"]))
    q = {e: f["val"] for (s, e), f in facts.items() if 80 <= days_between(s, e) <= 100}
    for (s, e), f in facts.items():
        if e in q:
            continue
        for (s2, e2), f2 in facts.items():
            if s2 == s and 80 <= days_between(e2, e) <= 100:
                q[e] = f["val"] - f2["val"]
                break
    return dict(sorted(q.items()))


def point_in_time(facts):
    facts = latest_filed(facts, lambda f: f["end"])
    return {e: f["val"] for e, f in sorted(facts.items())}


def year_earlier(series, end):
    target = date.fromisoformat(end) - timedelta(days=365)
    for e, v in series.items():
        if abs((date.fromisoformat(e) - target).days) <= 10:
            return v
    return None


def fred_series(series_id):
    # FRED stalls on browser-like user agents but answers Python's default one.
    text = fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", ua=None).decode()
    rows = []
    for r in csv.reader(text.splitlines()[1:]):
        if len(r) == 2 and r[1] not in ("", "."):
            rows.append((r[0], round(float(r[1]) * 100, 6)))  # percent -> basis points, without float dust
    return rows


def yahoo_closes(ticker, span="1y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={span}&interval=1d"
    r = fetch_json(url)["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    out = [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), c)
           for t, c in zip(r["timestamp"], closes) if c is not None]
    # While the market is open, Yahoo's last bar is today's running price, not a close.
    if out and out[-1][0] == TODAY.isoformat() and NOW.hour < 21:
        out = out[:-1]
    return out


def gpu_weekly(slug):
    """Weekly on-demand median $/GPU-hour (each provider counted once) as
    [(week start, price, providers)], oldest first, complete weeks only."""
    text = fetch(f"https://getdeploying.com/dataset/gpu-prices/{slug}.csv").decode()
    today = TODAY
    weeks = []
    for r in csv.DictReader(text.splitlines()):
        if r["billing_type"] == "ON_DEMAND" and r["provider_median_price"]:
            if date.fromisoformat(r["date"]) + timedelta(days=7) <= today:  # still filling in
                weeks.append((r["date"], float(r["provider_median_price"]),
                              int(r["provider_count"])))
    if not weeks:
        raise RuntimeError(f"no on-demand rows for {slug}")
    return sorted(weeks)


def news(query):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    root = ET.fromstring(fetch(url))
    items = []
    for i in root.findall("./channel/item"):
        try:
            when = parsedate_to_datetime(i.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        link = i.findtext("link") or ""
        if not link.startswith(("https://", "http://")):  # no javascript: or other schemes
            link = "#"
        items.append({"when": when, "title": i.findtext("title") or "", "link": link})
    return items


def deepseek(path, payload=None):
    if CACHED:
        raise RuntimeError("not called in a --cached run")
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        with open(DEEPSEEK_KEY_FILE) as f:
            key = f.read().strip()
    req = urllib.request.Request(
        "https://api.deepseek.com" + path,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=DEEPSEEK_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"DeepSeek API error {e.code}: {e.read()[:500].decode(errors='replace')}", file=sys.stderr)
        raise RuntimeError(f"DeepSeek API error {e.code} (details in the run log)") from None


# ---------- rendering helpers ----------

def esc(s):
    return html.escape(str(s))


def billions(v, red_if_negative=False):
    s = f"{v / 1e9:,.1f}"
    return f'<span class="bad">{s}</span>' if red_if_negative and v < 0 else s


def pct(v, red_if_negative=False):
    if v is None:
        return "n/a"
    s = f"{v:+.1f}%"
    return f'<span class="bad">{s}</span>' if red_if_negative and v < 0 else s


def line(rule, crossed, detail=""):
    """The worry line for one signal and whether it is crossed now
    (crossed=None means the reader has to check it by hand)."""
    if crossed is None:
        status = "Check by hand"
    else:
        status = '<span class="bad">CROSSED</span>' if crossed else "Not crossed"
    detail = f" {detail}" if detail else ""
    return (f'<p class="line"><b>Start worrying when:</b> {rule}<br>'
            f"<b>Now:</b> {status}.{detail}</p>")


def no_line(text):
    return f'<p class="line"><b>No fixed line.</b> {text}</p>'


def table(header, rows):
    head = "".join(f"<th>{h}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def source(text, url):
    return f'<p class="muted">Source: <a href="{esc(url)}">{esc(text)}</a></p>'


# ---------- charts ----------
# Inline SVG, no scripts (the page's security policy blocks them): hover text comes from <title>
# elements, and every value is also in the table next to the chart.

SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # blue, orange, aqua: validated as a set on white
INK, MUTED, GRID = "#111", "#52514e", "#e6e5e1"
CHART_FONT = "font:11px system-ui,sans-serif;display:block"


def legend(items):
    """HTML legend for two or more series: [(name, color)]."""
    return '<p class="legend">' + "".join(
        f'<span class="key" style="background:{c}"></span>{esc(n)}' for n, c in items) + "</p>"


def nice_ticks(lo, hi, n=4):
    """Round tick values spanning lo..hi (always including 0 when the range crosses it)."""
    span = (hi - lo) or abs(hi) or 1
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t, ticks = math.floor(lo / step) * step, []
    while t <= math.ceil(hi / step) * step + step / 1e6:
        ticks.append(round(t, 10))
        t += step
    return ticks


def bar_path(x, w, y0, y1, r=4):
    """A bar from baseline y0 to y1: 4px rounded at the data end, square at the baseline."""
    r = min(r, abs(y1 - y0), w / 2)
    if y1 <= y0:  # grows up
        return (f"M{x:.1f},{y0:.1f}V{y1 + r:.1f}Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f}"
                f"H{x + w - r:.1f}Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 + r:.1f}V{y0:.1f}Z")
    return (f"M{x:.1f},{y0:.1f}V{y1 - r:.1f}Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 - r:.1f}V{y0:.1f}Z")


def short_date(d):
    """Month and year for axis labels, e.g. "Jun ’26" (an apostrophe so it can't read as a day)."""
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%b} ’{d:%y}"


def column_chart(cats, series, fmt, domain=None, w=300, h=200, label_last=True, tip=None, aria=""):
    """Grouped columns per category on one shared axis, a vertical gridline between quarters and a
    date under each. series: [(name, color, [value per cat])]. domain=(lo, hi) shares a scale across
    small multiples. Only the last value of a single series is labeled; the rest are on hover."""
    vals = [v for _, _, vs in series for v in vs]
    lo, hi = domain or (min(vals), max(vals))
    lo, hi = min(0, lo), max(0, hi)  # bars grow from zero, so zero is always on the scale
    ticks = nice_ticks(lo, hi, n=8)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    ml, mr, mt, mb = 34, 6, 16, 32
    pw, ph = w - ml - mr, h - mt - mb
    y = lambda v: mt + (hi - v) / (hi - lo) * ph
    band = pw / len(cats)
    bw = min(24, band * 0.72 / len(series) - 2)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{esc(aria)}" style="{CHART_FONT}">']
    for t in ticks:
        out.append(f'<line x1="{ml}" x2="{w - mr}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{ml - 5}" y="{y(t) + 4:.1f}" text-anchor="end" fill="{MUTED}">{fmt(t, tick=float(t).is_integer())}</text>')
    for i in range(len(cats) + 1):  # quarter boundaries
        gx = ml + band * i
        out.append(f'<line x1="{gx:.1f}" x2="{gx:.1f}" y1="{mt}" y2="{mt + ph}" stroke="{GRID}"/>')
    out.append(f'<line x1="{ml}" x2="{w - mr}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="{MUTED}"/>')
    group = len(series) * bw + (len(series) - 1) * 2
    for i, cat in enumerate(cats):
        x0 = ml + band * i + (band - group) / 2
        d, cx = date.fromisoformat(cat), ml + band * (i + 0.5)  # month over year, so they fit
        out.append(f'<text x="{cx:.1f}" y="{h - 18}" text-anchor="middle" fill="{MUTED}">{d:%b}</text>'
                   f'<text x="{cx:.1f}" y="{h - 5}" text-anchor="middle" fill="{MUTED}">{d:%Y}</text>')
        for k, (name, color, vs) in enumerate(series):
            v = vs[i]
            x = x0 + k * (bw + 2)
            out.append(f'<path d="{bar_path(x, bw, y(0), y(v))}" fill="{color}"><title>{esc(name)}, quarter ending {esc(cat)}: {(tip or fmt)(v)}</title></path>')
            if label_last and i == len(cats) - 1 and len(series) == 1:
                ty = y(v) - 4 if v >= 0 else y(v) + 12
                out.append(f'<text x="{x + bw / 2:.1f}" y="{ty:.1f}" text-anchor="middle" fill="{INK}">{fmt(v)}</text>')
    out.append("</svg>")
    return "".join(out)


def line_chart(series, fmt, w=960, h=240, label_every=1, aria=""):
    """Weekly lines on one axis from zero, with a vertical gridline at each month (labeled every
    label_every months), an end dot and an end label per line.
    series: [(name, color, [(ISO date, value)])]. Hover any point for its value."""
    days = sorted({date.fromisoformat(d) for _, _, pts in series for d, _ in pts})
    hi = max(v for _, _, pts in series for _, v in pts)
    ticks = nice_ticks(0, hi, n=8)
    hi = ticks[-1]
    ml, mr, mt, mb = 40, 96, 10, 22
    pw, ph = w - ml - mr, h - mt - mb
    span = (days[-1] - days[0]).days or 1
    x = lambda d: ml + (d - days[0]).days / span * pw
    y = lambda v: mt + (hi - v) / hi * ph
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{esc(aria)}" style="{CHART_FONT}">']
    for t in ticks:
        out.append(f'<line x1="{ml}" x2="{w - mr}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{ml - 6}" y="{y(t) + 4:.1f}" text-anchor="end" fill="{MUTED}">{fmt(t, tick=float(t).is_integer())}</text>')
    m, k = (days[0].replace(day=28) + timedelta(days=4)).replace(day=1), 0  # first month start inside
    while m <= days[-1]:
        out.append(f'<line x1="{x(m):.1f}" x2="{x(m):.1f}" y1="{mt}" y2="{mt + ph}" stroke="{GRID}"/>')
        if k % label_every == 0:
            out.append(f'<text x="{x(m):.1f}" y="{h - 6}" text-anchor="middle" fill="{MUTED}">{esc(short_date(m))}</text>')
        m, k = (m.replace(day=28) + timedelta(days=4)).replace(day=1), k + 1
    for name, color, data in series:
        pts = " ".join(f"{x(date.fromisoformat(d)):.1f},{y(v):.1f}" for d, v in data)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round"/>')
    for name, color, data in series:  # end dot with a surface ring, label in text ink
        ex, ey = x(date.fromisoformat(data[-1][0])), y(data[-1][1])
        out.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{color}" stroke="#fff" stroke-width="2"/>')
        out.append(f'<text x="{ex + 9:.1f}" y="{ey + 4:.1f}" fill="{INK}">{esc(name)} {fmt(data[-1][1])}</text>')
    for name, color, data in series:  # generous invisible hover targets
        for d, v in data:
            out.append(f'<circle cx="{x(date.fromisoformat(d)):.1f}" cy="{y(v):.1f}" r="7" fill="transparent">'
                       f'<title>{esc(name)}, week of {esc(d)}: {fmt(v)}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def money_b(v, tick=False):
    """$B for charts; ticks drop the decimal."""
    if tick:
        return f"{v:,.0f}".replace("-", "−")
    return f"−{abs(v):,.1f}" if v < 0 else f"{v:,.1f}"


def money_tip(v):
    """A value in $B for a tooltip, e.g. "−$5.9B"."""
    return f"−${abs(v):,.1f}B" if v < 0 else f"${v:,.1f}B"


# ---------- signal blocks ----------

def sec_concept_or_empty(cik, concept):
    """Like sec_concept, but [] when the company doesn't report that concept at all."""
    try:
        return sec_concept(cik, concept)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise
    except RuntimeError:  # a --cached run that never fetched it
        return []


def stale(end, what, max_days=SEC_STALE_DAYS):
    """Warns (and returns a note) when the newest filed figure is older than max_days."""
    age = (TODAY - date.fromisoformat(end)).days
    if age > max_days:
        warn(f"{what}: the newest filed figures are for {end}, {age} days ago")
        return f" The newest figures are for {end}, which is unusually old: check for a late filing or a renamed concept."
    return ""


ORACLE_STORE = os.path.join(HERE, "data", "oracle_prepayments.json")
ORACLE_PREPAID = "IncreaseDecreaseInDeferredRevenuesFromCustomerPrepaymentsWithSignificantFinancingComponent"


def oracle_prepayments():
    """{quarter end: (customer prepayments with a significant financing component, first day they
    cover)} that Oracle counts in operating cash flow. The line is in Oracle's own XBRL extension,
    which the SEC's API leaves out, so it is read from each filing's XBRL instance once and
    remembered. Quarters before the line first appeared count as zero. Where the earlier quarters of
    a fiscal year weren't tagged, the remainder of the year-to-date figure is posted to its last
    quarter but covers from the first day after the latest earlier figure."""
    store = load_json_store(ORACLE_STORE)
    sub = fetch_json("https://data.sec.gov/submissions/CIK0001341439.json", SEC_UA)["filings"]["recent"]
    for i, form in enumerate(sub["form"]):
        acc, period = sub["accessionNumber"][i], sub["reportDate"][i]
        if form not in ("10-Q", "10-K") or period < "2025-05-01" or acc in store:
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/1341439/{acc.replace('-', '')}/"
        time.sleep(0.2)
        names = [x["name"] for x in fetch_json(base + "index.json", SEC_UA)["directory"]["item"]]
        inst = next((n for n in names if n.endswith("_htm.xml")), None)
        facts = []
        if inst:
            time.sleep(0.2)
            root = ET.fromstring(fetch(base + inst, SEC_UA))
            ctx = {}
            for c in root.iter():
                if c.tag.endswith("}context") and not any(x.tag.endswith("}segment") for x in c.iter()):
                    sd, ed = c.find(".//{*}startDate"), c.find(".//{*}endDate")
                    if sd is not None and ed is not None:
                        ctx[c.get("id")] = (sd.text, ed.text)
            facts = sorted({(*ctx[e.get("contextRef")], float(e.text)) for e in root.iter()
                            if e.tag.endswith("}" + ORACLE_PREPAID) and e.get("contextRef") in ctx and e.text})
        store[acc] = dict(period=period, form=form, facts=facts)
    save_json_store(ORACLE_STORE, store)
    ytd = {(s0, e): v for rec in store.values() for s0, e, v in rec["facts"]}
    out = {}
    for (s0, e), v in ytd.items():
        if days_between(s0, e) <= 100:
            out[e] = (v, s0)
            continue
        prev = max(((e2, v2) for (s2, e2), v2 in ytd.items() if s2 == s0 and e2 < e), default=None)
        if prev is None:  # no earlier figure this fiscal year: it covers the whole year so far
            out[e] = (v, s0)
        else:
            out[e] = (v - prev[1], (date.fromisoformat(prev[0]) + timedelta(days=1)).isoformat())
    return out


def block_hyperscalers():
    rows, panels, fcf_negative, fcf_unknown, capex_down, unknown, notes = [], [], [], [], [], [], ""
    for ticker, cik, capex_concept in HYPERSCALERS:
        ocf = quarterly_flows(sec_concept(cik, "NetCashProvidedByUsedInOperatingActivities"))
        capex = quarterly_flows(sec_concept(cik, capex_concept))
        lease = quarterly_flows(sec_concept_or_empty(cik, "FinanceLeasePrincipalPayments"))
        prepaid_spans = oracle_prepayments() if ticker == "ORCL" else {}
        prepaid = {e: v for e, (v, _) in prepaid_spans.items()}
        ends = [e for e in capex if e in ocf]
        fcf = {e: ocf[e] - capex[e] - lease.get(e, 0) - prepaid.get(e, 0) for e in ends}
        last = ends[-1]
        notes += stale(last, f"{ticker} capex and cash flow")
        ttm = None
        if len(ends) < 4 or not consecutive(ends[-4:]):
            notes += f" {ticker}'s last four filed quarters ({', '.join(ends[-4:])}) aren't consecutive, so its four-quarter free cash flow isn't shown."
        elif any(v and days_between(s, ends[-4]) > 100 for e, (v, s) in prepaid_spans.items() if e in ends[-4:]):
            notes += (f" {ticker}'s customer prepayments are tagged only for a period reaching back before "
                      "the last four quarters, so its four-quarter free cash flow isn't shown.")
        else:
            ttm = sum(fcf[e] for e in ends[-4:])
        untagged = [e for e in ends[-4:] if lease and e not in lease and e > min(lease)]
        if untagged:
            tagged = max(e for e in lease if e < untagged[0])
            notes += (f" {ticker} hasn't tagged finance-lease repayments for the quarters ending "
                      f"{' and '.join(untagged)}, so they count as zero here (${lease[tagged] / 1e9:,.2f}B in the "
                      f"quarter ending {tagged}).")
        yoys = []
        for e in ends[-2:]:
            prior = year_earlier(capex, e)
            yoys.append((capex[e] / prior - 1) * 100 if prior else None)
        if None in yoys:
            unknown.append(ticker)
        elif all(y < 0 for y in yoys):
            capex_down.append(ticker)
        if ttm is None:
            fcf_unknown.append(ticker)
        elif ttm < 0:
            fcf_negative.append(ticker)
        last6 = ends[-6:]
        panels.append((ticker, last6, [capex[e] / 1e9 for e in last6], [fcf[e] / 1e9 for e in last6]))
        if prepaid:
            notes += (f" For Oracle, customer prepayments with a significant financing component "
                      f"(${sum(prepaid.get(e, 0) for e in ends[-4:]) / 1e9:,.1f}B over the last four quarters) are "
                      "taken out of operating cash flow: Oracle counts them there, but they are customers "
                      "funding Oracle's build-out, not cash its business generated.")
            for e, (v, s) in prepaid_spans.items():
                if v and e in last6 and days_between(s, e) > 100:
                    notes += (f" Oracle tagged these prepayments for {s} to {e} only as a total "
                              f"(${v / 1e9:,.1f}B); the chart shows all of it in the quarter ending {e}.")
        rows.append([ticker, esc(last), billions(ttm, True) if ttm is not None else "n/a",
                     " / ".join(pct(y, True) for y in yoys)])
    lo = min(0, *(min(f) for *_, f in panels))
    hi = max(0, *(max(c) for _, _, c, _ in panels))
    out = legend([("Capex", SERIES[0]), ("Free cash flow", SERIES[1])])
    out += ('<p class="muted">Last 6 quarters by quarter-end month, $B, same scale for every '
            'company. Hover a bar for its exact value.</p><div class="multiples">')
    out += "".join(
        f"<div><h5>{t}</h5>" + column_chart(ends, [("Capex", SERIES[0], c), ("Free cash flow", SERIES[1], f)],
                                           money_b, (lo, hi), tip=money_tip,
                                           aria=f"{t} capex and free cash flow, last 6 quarters") + "</div>"
        for t, ends, c, f in panels) + "</div>"
    out += table(["Company", "Latest quarter end", "Free cash flow, last 4 quarters ($B)",
                  "Capex vs same quarter a year earlier (previous / latest quarter)"], rows)
    detail = f"Two quarters below a year earlier: {', '.join(capex_down)}." if capex_down else ""
    if unknown:
        detail += f" No year-earlier figure for {', '.join(unknown)}, so check by hand."
    out += line(
        "any company's quarterly capex is lower than in the same quarter a year earlier, two "
        "quarters in a row (one lumpy quarter isn't enough). Ares's trigger is a season of "
        "disappointing AI revenue that leads boards to shift spending; a cut to capex guidance "
        "on an earnings call comes months before it shows up here, so read the calls: Microsoft, "
        "Alphabet, Amazon and Meta report in late January or early February, late April, late July "
        "and late October; Oracle in mid-March, mid-June, mid-September and mid-December.",
        True if capex_down else (None if unknown else False), detail)
    out += line(
        "free cash flow over the last four quarters is below zero: over a full year, capex and "
        "lease repayments took more than the cash the business generated. Single quarters swing "
        "with working capital and the seasons (Amazon's first quarter is often negative), so they "
        "don't count on their own. Ares pointed to Alphabet and Amazon posting negative free cash "
        "flow in a recent quarter.",
        True if fcf_negative else (None if fcf_unknown else False),
        (f"Negative over the last four quarters: {', '.join(fcf_negative)}." if fcf_negative else "")
        + (f" No four-quarter figure for {', '.join(fcf_unknown)}, so check by hand." if fcf_unknown else ""))
    out += ('<p class="muted">Free cash flow here = operating cash flow − purchases of property and '
            "equipment − principal repayments of finance leases (where reported), as filed; this is "
            "Meta's own definition. Amazon's headline figure nets proceeds from asset sales against "
            "capex, and Microsoft's headline capex includes assets taken on through finance leases, so "
            f"their own figures differ.{notes}</p>")
    return out + source("SEC EDGAR XBRL filings", "https://www.sec.gov/edgar/search/")


def block_nvidia():
    rev = quarterly_flows(sec_concept(NVDA_CIK, "Revenues"))
    ends = list(rev)[-6:]
    gap = not consecutive(ends[-2:])
    change = None if gap else (rev[ends[-1]] / rev[ends[-2]] - 1) * 100
    out = ('<p class="muted">Quarterly revenue by quarter-end month, $B. Hover a bar for its '
           'exact value.</p><div style="max-width:480px">'
           + column_chart(ends, [("Revenue", SERIES[0], [rev[e] / 1e9 for e in ends])], money_b,
                          w=480, h=220, tip=money_tip, aria="Nvidia quarterly revenue, last 6 quarters") + "</div>")
    out += line("revenue falls from the previous quarter. Nvidia's sales are AI spending arriving; "
                "a sequential drop means buyers are slowing down. Nvidia also guides the next "
                "quarter's revenue in its results (late February, May, August and November); a guide "
                "below the latest quarter is an earlier warning than this line.",
                None if gap else change < 0,
                (f"The latest two filed quarters ({ends[-2]} and {ends[-1]}) aren't consecutive, so check by hand."
                 if gap else f"Latest quarter vs previous: {pct(change, True)}.") + stale(ends[-1], "Nvidia revenue"))
    return out + source("SEC EDGAR XBRL filings (Nvidia)",
                        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810")


def block_backlog():
    rows, crossed, unknown, panels, dips, notes = [], [], [], [], [], ""
    for ticker, cik in BACKLOG:
        rpo = point_in_time(sec_concept(cik, "RevenueRemainingPerformanceObligation"))
        ends = list(rpo)
        q = [(rpo[e] / rpo[p] - 1) * 100 if consecutive([p, e]) else None  # None: a quarter is missing
             for p, e in zip(ends, ends[1:])]
        prior = year_earlier(rpo, ends[-1])
        yoy = (rpo[ends[-1]] / prior - 1) * 100 if prior else None
        if (yoy is not None and yoy < 0) or (len(q) >= 2 and None not in q[-2:] and q[-1] < 0 and q[-2] < 0):
            crossed.append(ticker)
        elif None in q[-2:]:
            unknown.append(ticker)
        worst = min(((c, e) for c, e in zip(q, ends[1:]) if c is not None), default=None)
        if worst and worst[0] < 0:
            dips.append(f"{ticker} {worst[0]:.1f}% ({date.fromisoformat(worst[1]):%b %Y})")
        notes += stale(ends[-1], f"{ticker} backlog")
        last6 = ends[-6:]
        panels.append((ticker, last6, [rpo[e] / 1e9 for e in last6]))
        rows.append([ticker, esc(ends[-1]), pct(q[-1], True) if q else "n/a", pct(yoy, True)])
    domain = (0, max(max(v) for *_, v in panels))
    out = ('<p class="muted">Contract backlog, last 6 quarters by quarter-end month, $B, same scale '
           'for every company. Hover a bar for its exact value.</p><div class="multiples">')
    out += "".join(f"<div><h5>{t}</h5>" + column_chart(ends, [("Backlog", SERIES[0], v)], money_b, domain,
                                                      tip=money_tip, aria=f"{t} contract backlog, last 6 quarters")
                   + "</div>" for t, ends, v in panels) + "</div>"
    out += table(["Company", "Latest quarter end", "vs previous quarter", "vs a year earlier"], rows)
    out += line(
        "backlog at any of the three is lower than a year earlier, or has fallen two quarters in "
        "a row. Much of it is reported to be OpenAI and Anthropic compute commitments (e.g. OpenAI's "
        "reported $300B deal with Oracle), so a sustained fall can mean commitments are being cut "
        "or renegotiated. A single quarterly dip isn't enough: backlog is worked off as it is "
        "delivered (Microsoft expects to recognize about 30% of its backlog within 12 months), and "
        "the largest one-quarter dips in the filed history are " + "; ".join(dips) + ".",
        True if crossed else (None if unknown else False),
        (f"Crossed: {', '.join(crossed)}." if crossed else "")
        + (f" A quarter is missing from {', '.join(unknown)}'s latest filed figures, so check by hand." if unknown else "")
        + notes)
    return out + source("SEC EDGAR XBRL filings (remaining performance obligations)",
                        "https://www.sec.gov/edgar/search/")


def block_commitments():
    rows = []
    for ticker, cik, concept, what in COMMITMENTS:
        series = point_in_time(sec_concept_or_empty(cik, concept))
        if not series:
            rows.append([ticker, esc(what), "not reported", "n/a", "n/a"])
            continue
        last = list(series)[-1]
        earlier = [(e, v) for e, v in series.items() if 300 <= days_between(e, last) <= 460]
        prev = earlier[-1] if earlier else None
        note = stale(last, f"{ticker} commitments", 400)  # some are tagged only once a year
        rows.append([ticker, esc(what), f"${series[last] / 1e9:,.1f}B ({esc(last)})",
                     f"${prev[1] / 1e9:,.1f}B ({esc(prev[0])})" if prev else "n/a",
                     (f"×{series[last] / prev[1]:.1f}" if prev and prev[1] > 0 else "n/a") + (" *" if note else "")])
    out = table(["Company", "What the filing says it is", "Latest", "About a year earlier", "Change"], rows)
    out += no_line(
        "These are payments the companies have signed up for that are not on their balance sheets as "
        "debt: leases for data centers that haven't started yet, and long-term purchase and supply "
        "agreements. Ares counts about $3 trillion of such lease and purchase commitments across the "
        "eight largest AI companies. Each company tags a different thing, so compare a company with "
        "its own past, not with the others. Microsoft and Amazon don't tag a comparable figure; "
        "Alphabet's and Oracle's leases not yet started, and Nvidia's much larger supply and capacity "
        "commitments, appear only in the text of their filings. "
        "Worry when these keep growing much faster than the companies' cash flow.")
    return out + source("SEC EDGAR XBRL filings", "https://www.sec.gov/edgar/search/")


def block_gpu_prices():
    rows, crossed, lines, worst_text = [], [], [], []
    for i, (slug, name) in enumerate(GPU_MODELS):
        weeks = [w for w in gpu_weekly(slug) if w[0] >= GPU_START.isoformat()]
        lines.append((name, SERIES[i], [(w, p) for w, p, _ in weeks]))
        week, price, providers = weeks[-1]
        avg = lambda j: st.mean(p for _, p, _ in weeks[j - GPU_AVG_WEEKS + 1:j + 1])  # 4-week average ending at week j

        def change(n, j=len(weeks) - 1):
            return (avg(j) / avg(j - n) - 1) * 100 if j - n >= GPU_AVG_WEEKS - 1 else None

        c13 = change(13)
        if c13 is not None and c13 <= -GPU_DROP_13W_PCT:
            crossed.append(name)
        worst = min((change(13, j) for j in range(13 + GPU_AVG_WEEKS - 1, len(weeks))), default=None)
        if worst is not None:
            worst_text.append(f"{name} {worst:+.1f}%")
        mix = " (provider count changed by more than a fifth this week)" if abs(providers - weeks[-2][2]) > 0.2 * weeks[-2][2] else ""
        rows.append([
            name, f"${price:.2f}", f"{esc(week)}, {providers} providers{mix}",
            pct(change(4)),
            f'<span class="bad">{pct(c13)}</span>' if name in crossed else pct(c13),
            pct(worst), pct((avg(len(weeks) - 1) / avg(GPU_AVG_WEEKS - 1) - 1) * 100) + f" (since {esc(weeks[0][0])})",
        ])
    out = legend([(n, c) for n, c, _ in lines])
    out += ('<p class="muted">Weekly median advertised on-demand price, $/GPU-hour (sold-out listings '
            'excluded; a median also moves when providers come and go); vertical lines mark the start '
            'of each month. Hover a line for the week and price.</p>')
    fmt = lambda v, tick=False: f"${v:.0f}" if tick else f"${v:.2f}"
    out += (f'<div class="wide">{line_chart(lines, fmt, aria="Weekly median GPU rental prices")}</div>'
            f'<div class="narrow">{line_chart(lines, fmt, w=380, h=260, label_every=3, aria="Weekly median GPU rental prices")}</div>')
    out += table(["GPU", "Latest weekly median, $/GPU-hour", "Latest week", "4 weeks", "13 weeks",
                  "Worst 13-week change", "Whole series"], rows)
    out += line(
        f"the {GPU_AVG_WEEKS}-week average of the on-demand median falls {GPU_DROP_13W_PCT}% or more over "
        "13 weeks (about 90 days). Changes compare 4-week averages, because single weeks jump when "
        "listings sell out or providers come and go. Since the source switched to automated price "
        f"collection ({GPU_START:%b %-d, %Y}), the worst 13-week changes were: {', '.join(worst_text)}. "
        "Falling rental prices mean the chips backing GPU loans are losing value faster than lenders "
        "assumed. (Ares's chart covers 2023–25, when H100 rentals fell from about $8.50 to $3.00; Ares "
        "ties the risk to new chip generations, which Nvidia now ships roughly every year.)",
        bool(crossed), f"Crossed: {', '.join(crossed)}." if crossed else "")
    return out + (
        '<p class="muted">Source: <a href="https://getdeploying.com/dataset/gpu-prices">'
        "GetDeploying, GPU rental price history</a>, "
        '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. Weekly median '
        "advertised on-demand price across cloud providers, each provider counted once; the current "
        "week is left out until it is complete. Changes use 4-week averages from "
        f"{GPU_START:%b %-d, %Y}, when the series switched from hand-entered to automated prices.</p>")


def spread_episodes(rows, limit):
    """(first date scanned, [(start, end, peak excess)]) for runs of consecutive observations at
    least limit bp above the trailing 1-year low. Only dates with a full year of history before
    them are scanned, since FRED serves just the last three years of these series."""
    pts = [(date.fromisoformat(d), v) for d, v in rows]
    first = pts[0][0] + timedelta(days=365)
    episodes, run = [], None
    for d, v in pts:
        if d < first:
            continue
        excess = v - min(x for e, x in pts if d - timedelta(days=365) <= e <= d)
        if excess >= limit:
            run = [d, d, excess] if run is None else [run[0], d, max(run[2], excess)]
        elif run:
            episodes.append(tuple(run))
            run = None
    if run:
        episodes.append(tuple(run))
    return first, episodes


def describe_episodes(name, limit, first, episodes):
    if not episodes:
        return f"Since {first:%b %Y}, {name} have not been {limit}bp above their 1-year low."
    spans = "; ".join(f"{a:%b %-d, %Y}" + ("" if a == b else f" – {b:%b %-d, %Y}") + f" (peak +{p:.0f}bp)"
                      for a, b, p in episodes)
    times = "once" if len(episodes) == 1 else f"{len(episodes)} times"
    return f"Since {first:%b %Y}, {name} crossed their line {times}: {spans}."


def block_spreads():
    out_rows, crossed, history = [], [], []
    for series_id, name, short, limit in [("BAMLC0A4CBBB", "BBB corporate (lowest investment grade)",
                                           "BBB spreads", BBB_WIDENING_BP),
                                          ("BAMLH0A0HYM2", "High yield", "high-yield spreads", HY_WIDENING_BP)]:
        rows = fred_series(series_id)
        history.append(describe_episodes(short, limit, *spread_episodes(rows, limit)))
        d, latest = rows[-1]
        if (TODAY - date.fromisoformat(d)).days > 6:
            warn(f"{name} spread: the newest FRED value is for {d}")
        year_ago = (date.fromisoformat(d) - timedelta(days=365)).isoformat()
        low_d, low = min(((x, v) for x, v in rows if x >= year_ago), key=lambda p: p[1])
        month_ago = [v for x, v in rows
                     if x <= (date.fromisoformat(d) - timedelta(days=30)).isoformat()][-1]
        above = latest - low
        if above >= limit:
            crossed.append(name)
        widening = f"+{above:.0f}bp"
        out_rows.append([name, f"{latest:.0f}bp ({esc(d)})", f"{month_ago:.0f}bp",
                         f"{low:.0f}bp ({esc(low_d)})",
                         f'<span class="bad">{widening}</span>' if above >= limit else widening,
                         f"+{limit}bp"])
    out = table(["Spread over Treasuries", "Latest", "30 days ago", "1-year low",
                 "Above 1-year low", "Worry line"], out_rows)
    out += line(
        f"BBB spreads are {BBB_WIDENING_BP}bp or more above their 1-year low, or high-yield "
        f"spreads {HY_WIDENING_BP}bp or more. {' '.join(history)} "
        "These cover the whole market, not just AI debt, so they confirm stress more than predict it.",
        bool(crossed), f"Crossed: {', '.join(crossed)}." if crossed else "")
    return out + source("ICE BofA option-adjusted spreads via FRED",
                        "https://fred.stlouisfed.org/series/BAMLC0A4CBBB")


# ---------- CDS levels reported in the news ----------
# The DTCC-based estimate is checked against 5-year CDS levels reported in the press: a curated
# list (cds/published_levels.csv) plus levels stated in news headlines from now on. DeepSeek reads
# each candidate headline once; a level is used only if its number and unit appear in the
# headline word for word.

CDS_NEWS_STORE = os.path.join(HERE, "data", "cds_news.json")
CDS_NEWS_DAYS = 30
CDS_WORDS = re.compile(r"credit[- ]default[- ]swaps?|\bCDS\b", re.I)
CDS_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:bp\b|bps\b|basis points?|percentage points?)", re.I)
CDS_NEWS_INSTRUCTIONS = """\
You read news headlines about credit default swaps (CDS). For each numbered headline, say whether it \
states the LEVEL of the named company's 5-year CDS spread: the spread itself, not a change in it, \
and not a bond spread, a bond yield, an index or another company.

Answer with one line per headline:
ID | LEVEL | <the number exactly as written in the headline> | <its unit exactly as written>
or
ID | NONE

A change ("up 40bp", "widens by 40 basis points") is NONE, and so is a bound ("tops 1,000bp", \
"above 300 basis points", "nearly 900bp"). If the headline gives several levels, use the latest one. \
If it doesn't say which maturity, assume 5 years; any other stated maturity is NONE."""


BOUND_WORDS = re.compile(r"(above|over|exceed(s|ed|ing)?|tops?|topped|surpass(es|ed)?|beyond|more than|at least|"
                         r"nearly|almost|near|under|below|past|crosse?s?d?)\s+$", re.I)


def is_bound(title, number):
    """True when the number is a threshold ("tops 1,000bp") rather than a level."""
    i = title.find(number)
    return i < 0 or bool(BOUND_WORDS.search(title[:i]))


def cds_news_levels():
    """{ticker: [dict(date, bp, source, url, title)]} from the headline store, after reading any new
    candidate headlines."""
    store = load_json_store(CDS_NEWS_STORE)
    candidates = []
    for ticker, name in ([] if CACHED else CDS_NAMES):  # a --cached run only reuses what's stored
        query = f'"{name}" ("credit default swap" OR "credit default swaps" OR CDS) when:{CDS_NEWS_DAYS}d'
        for h in (x for part in search(query) for x in part):
            title, key = h["title"], headline_key(h["title"])
            if (key in store or name.lower() not in title.lower() or not CDS_WORDS.search(title)
                    or not CDS_NUMBER.search(title)):
                continue
            candidates.append((ticker, name, h, key))
    if candidates and not CACHED:
        check_model("reading CDS levels in headlines")
        r = deepseek("/chat/completions", {
            "model": DEEPSEEK_MODEL, "reasoning_effort": "high", "max_tokens": 16000,
            "messages": [{"role": "system", "content": CDS_NEWS_INSTRUCTIONS},
                         {"role": "user", "content": "\n".join(f"{i}. [{name}] {h['title']}"
                                                               for i, (_, name, h, _) in enumerate(candidates))}]})
        choice = r["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise RuntimeError(f"the model stopped early ({choice.get('finish_reason')})")
        answers = {}
        for row in (choice["message"].get("content") or "").splitlines():
            m = re.match(r"\s*(\d+)\s*\|\s*(LEVEL|NONE)\s*(?:\|\s*([\d,.]+)\s*\|\s*(.+?))?\s*$", row)
            if m:
                answers[int(m.group(1))] = m.groups()[1:]
        for i, (ticker, name, h, key) in enumerate(candidates):
            if i not in answers:
                continue  # left out: try again next run
            kind, number, unit = answers[i]
            bp = None
            if (kind == "LEVEL" and number and unit and f"{number}" in h["title"] and unit.lower() in h["title"].lower()
                    and not is_bound(h["title"], number)):
                value = float(number.replace(",", ""))
                bp = value * 100 if unit.lower().startswith("percentage") else value
                if not 1 <= bp <= 10000:
                    bp = None
            store[key] = dict(ticker=ticker, date=h["when"].date().isoformat(), title=h["title"],
                              url=h["link"], bp=bp, model=DEEPSEEK_MODEL_NAME)
        save_json_store(CDS_NEWS_STORE, store)
    out = {t: [] for t, _ in CDS_NAMES}
    for v in store.values():
        if v.get("bp") and v["ticker"] in out and not is_bound(v["title"], f"{v['bp']:,.0f}"):
            title = v["title"]
            source = title.rsplit(" - ", 1)[1] if " - " in title else "news"
            out[v["ticker"]].append(dict(date=date.fromisoformat(v["date"]), bp=v["bp"], source=source,
                                         url=v["url"], title=title))
    return out


def reported_levels():
    """{ticker: [dict(date, bp, source, url, note, out_of_sample)]}: the curated published levels
    plus those read from headlines (a headline within a day and 3% of a curated level is the same
    report). Levels found after the method was fixed count as out of sample."""
    out = {}
    curated = cds_signal.published_levels()
    try:
        news_levels, problem = cds_news_levels(), None
    except Exception as e:
        news_levels, problem = {}, f"CDS levels in today's headlines weren't read: {short_error(e)}"
    for ticker, _ in CDS_NAMES:
        rows = [dict(r, out_of_sample=r["role"] == "holdout") for r in curated.get(ticker, [])]
        for n in news_levels.get(ticker, []):
            if not any(abs((n["date"] - r["date"]).days) <= 1 and abs(n["bp"] / r["bp"] - 1) <= 0.03 for r in rows):
                rows.append(dict(n, note=n["title"], out_of_sample=True))
        out[ticker] = sorted(rows, key=lambda r: r["date"])
    return out, problem


def pct_text(p):
    """A default probability with honest precision: whole percents from 10% up."""
    return f"{p * 100:.0f}%" if p >= 0.095 else f"{p * 100:.1f}%"


def block_cds():
    today = TODAY
    start = today - timedelta(days=cds_signal.WINDOW_DAYS)
    data = cds_signal.series(today, download=not CACHED)
    reported, problem = reported_levels()
    to_pd = cds_signal.pd_from_spread
    # one probability scale for all three, so they can be compared
    peak = max([s.hi or s.median for series, *_ in data.values() for s in series if s.median is not None]
               + [to_pd(r["bp"] / 1e4) for rows in reported.values() for r in rows if start <= r["date"] <= today] + [0.01])
    top = cds_signal.nice_pd_max(peak)
    out = ('<p class="legend">'
           '<span class="key" style="background:#2a78d6;border:2px solid #2a78d6;width:6px;height:6px;border-radius:50%"></span>'
           'Estimate from CDS trades (DTCC), monthly median; bar = 90% interval; faded = fewer than 3 trading days '
           '<span class="key" style="background:#2a78d6;opacity:.25;width:8px;height:14px;border-radius:4px"></span>'
           'month where the trade data can\'t tell who paid the up-front (both readings shown) '
           '<span class="key" style="background:#eb6834;width:9px;height:9px;transform:rotate(45deg);border-radius:1px"></span>'
           'level reported in the news '
           '<span class="key" style="background:#8f8d87;height:2px"></span>'
           'stock price, daily close (left axis, log scale)</p>'
           '<p class="muted">Right axis: 5-year default probability, one scale for all three companies. Hover a '
           'point for details and sources.</p>')
    rows, gaps = [], []
    for ticker, name in CDS_NAMES:
        try:
            closes = [(date.fromisoformat(d), c) for d, c in yahoo_closes(ticker, "2y")]
            note = ""
        except Exception as e:
            closes, note = [], f'<p class="error">Stock price unavailable today: {esc(short_error(e))}</p>'
        series, by_rec, pts = data[ticker]
        marks = []
        for r in reported[ticker]:
            mine, n = cds_signal.estimate_near(pts, r["date"])
            check = (f"; trade-based estimate {mine:,.0f}bp from {n} trade{'s' * (n != 1)} in the days before"
                     if mine else "")
            if mine and r["out_of_sample"] and start <= r["date"] <= today:
                gaps.append(abs(mine / r["bp"] - 1))
            note_text = f" ({r['note']})" if r.get("note") and r["note"] != r.get("title") else ""
            marks.append((r["date"], to_pd(r["bp"] / 1e4),
                          f"{r['source']}, {r['date']:%b %-d, %Y}: 5-year CDS {r['bp']:,.0f}bp{note_text} "
                          f"= {pct_text(to_pd(r['bp'] / 1e4))} on this page's assumptions{check}"))
        out += (f"<h4>{esc(name)} ({ticker})</h4>"
                + cds_signal.svg_chart(ticker, series, closes, start, today, top, marks) + note)
        have = [s for s in series if s.median is not None]
        latest_news = max(reported[ticker], key=lambda r: r["date"], default=None)
        news_cell = (f'<a href="{esc(latest_news["url"])}">{latest_news["bp"]:,.0f}bp</a> on '
                     f'{latest_news["date"]:%b %-d} ({esc(latest_news["source"])}) = {pct_text(to_pd(latest_news["bp"] / 1e4))}'
                     if latest_news else "none found")
        if not have:
            rows.append([f"{esc(name)} ({ticker})", "no trades", "n/a", "n/a", "n/a", news_cell, "0"])
            continue
        s = have[-1]
        period = (f"{s.month:%b} 1–{s.last.day}, {s.month:%Y}" if (s.last.year, s.last.month) == (today.year, today.month)
                  else f"{s.month:%b %Y}")
        bp = lambda p: cds_signal.par_spread(-math.log(1 - p) / cds_signal.HORIZON) * 1e4
        if s.unresolved:
            est = f"{pct_text(s.alt)} or {pct_text(s.median)} (who paid isn't settled)"
            spread = f"{bp(s.alt):,.0f} or {bp(s.median):,.0f}bp"
            rec = "n/a"
        else:
            est = f"{pct_text(s.median)} ({pct_text(s.lo)}–{pct_text(s.hi)})"
            spread = f"{bp(s.median):,.0f}bp"
            rec_lo, rec_hi = (by_rec.get(r) for r in cds_signal.RECOVERY_RANGE)
            rec = f"{pct_text(rec_lo)}–{pct_text(rec_hi)}" if rec_lo is not None and rec_hi is not None else "n/a"
        rows.append([f"{esc(name)} ({ticker})", period, est, spread, rec, news_cell, f"{s.n} on {s.days} days"])
    rec_lo, rec_hi = (f"{r:.0%}" for r in cds_signal.RECOVERY_RANGE)
    out += table(["Company", "Latest month", "5-year default probability (90% interval)", "≈ 5-year spread",
                  f"At {rec_lo}–{rec_hi} recovery", "Latest level in the news", "Trades"], rows)
    accuracy = (f"Against the {len(gaps)} reported levels that were not used to build the method and have trades "
                f"in the days before them, the trade-based estimate differs by a median of {st.median(gaps) * 100:.0f}% "
                f"(at most {max(gaps) * 100:.0f}%). " if gaps else "")
    out += no_line(
        "This is the 5-year default probability implied by what credit default swap buyers pay for "
        "protection against the company failing to pay its debts or going bankrupt (including on debt "
        "it guarantees; a restructuring or an out-of-court debt exchange generally doesn't count, nor "
        "does a default by a subsidiary whose debt it doesn't guarantee). It is a market price, not a "
        "forecast: it includes a premium for bearing the risk, so the real-world odds are likely lower, "
        f"and it assumes lenders recover {cds_signal.R:.0%} in a default (the recovery column shows "
        f"{rec_lo} and {rec_hi}; lower recovery means a lower probability for the same price). " + accuracy +
        "Worry when it climbs for several months in a row, and most when CoreWeave and Oracle climb "
        "together while Nvidia stays flat, which points to the AI financing chain rather than chip demand.")
    if problem:
        out += f'<p class="error">{esc(problem)}</p>'
    return out + (
        '<p class="muted">Source: <a href="https://pddata.dtcc.com/ppd/secdashboard">DTCC public '
        "security-based swap data</a> (SEC Regulation SBSR), reported levels from the press as linked, and "
        "Yahoo Finance daily closes. The estimate uses US-dollar standard-coupon trades on each company's "
        "own credit with 4.5–5.5 years left; legs of credit-index trades, novations, modifications and "
        "unwind fees are left out, as are single prints far out of line with the trades around them. "
        "A trade prices from its quoted spread or from its up-front payment and exact size. DTCC reports "
        "the up-front as net cash, after the accrued coupon the seller hands back, without saying who paid "
        "it; that is settled by quoted spreads and by how prices behave across quarterly coupon dates, and "
        "where the data don't settle it both readings are shown. Some spreads are reported 100 times too "
        f"small; these are scaled up only when they match properly reported quotes within {cds_signal.RESCALE_DAYS} "
        "days. Reported levels are converted to probabilities with the same assumptions. Method and checks: "
        "cds/CDS_VALIDATION.md in the project.</p>")


def block_weak_borrowers():
    rows, moves, last_day = [], {}, None
    for ticker in WEAK_BORROWERS + ["QQQ"]:
        series = yahoo_closes(ticker)
        closes = [c for _, c in series]
        last, last_day = closes[-1], series[-1][0]
        r1m = (last / closes[-22] - 1) * 100 if len(closes) > 22 else None
        r3m = (last / closes[-64] - 1) * 100 if len(closes) > 64 else None
        dd = (last / max(closes) - 1) * 100
        moves[ticker] = r1m
        label = "QQQ (Nasdaq-100, for comparison)" if ticker == "QQQ" else ticker
        rows.append([label, f"{last:,.2f}", pct(r1m), pct(r3m), pct(dd)])
    out = table(["Ticker", f"Close ({esc(last_day)})", "1 month", "3 months", "From 52-week high"], rows)
    group = [t for t in WEAK_BORROWERS if t != "ORCL" and moves.get(t) is not None]
    basket = st.mean(moves[t] for t in group) if group else None
    fell = [t for t in group if moves[t] <= -WEAK_EACH_PCT]
    qqq = moves.get("QQQ")
    crossed = (None if basket is None or qqq is None
               else basket <= -WEAK_BASKET_PCT and len(fell) >= 4 and qqq > -QQQ_CALM_PCT)
    out += line(
        f"the former bitcoin miners and CoreWeave fall together while the market holds up: over a "
        f"month, the six ({', '.join(group)}) are down {WEAK_BASKET_PCT}% or more on average, at least "
        f"four of them by {WEAK_EACH_PCT}% or more, while QQQ is down less than {QQQ_CALM_PCT}%. Ares "
        "profiles these miners as having gone \"from bitcoin miner to investment grade issuer in three "
        "years (or less)\", and CoreWeave as one of the largest borrowers in GPU financing; Oracle is "
        "shown for comparison. Their stocks are a free stand-in for their bond prices, which usually "
        "move first but aren't freely available, and they routinely swing 50% or more without any "
        "credit event. A fall that comes with news of a pulled bond deal, a downgrade or a covenant "
        "problem (see the headlines below) matters more.",
        crossed, (f"Over the last month: the six {basket:+.1f}% on average, {len(fell)} down "
                  f"{WEAK_EACH_PCT}% or more, QQQ {qqq:+.1f}%." if crossed is not None else ""))
    return out + source("Yahoo Finance daily closes", "https://finance.yahoo.com/")


HEADLINE_FEEDS = [  # id, title, Google News searches, what the model flags, why it matters
    ("A", "Hyperscalers pulling back",
     ['(Microsoft OR Google OR Amazon OR Meta OR Oracle OR OpenAI OR CoreWeave) "data center" '
      '(lease OR leases) (cancel OR canceled OR pause OR paused OR defer OR deferred OR '
      '"force majeure" OR "walks away" OR "pulls back") when:30d'],
     "a hyperscaler or AI lab cancelling, deferring, pausing or walking away from a data center "
     "lease or site, or invoking a contract exit such as force majeure",
     "Ares's take-or-pay example shows these exits are written into the contracts."),
    ("B", "Capex guidance",
     ["(Microsoft OR Alphabet OR Amazon OR Meta OR Oracle) capex (cut OR cuts OR lower OR "
      "reduce OR slows OR guidance) when:30d"],
     "Microsoft, Alphabet, Amazon, Meta or Oracle lowering its plan or guidance for future capital "
     "spending (not one quarter's spending coming in below analysts' estimates, and not an "
     "accounting reclassification of the same spending)",
     "That is the board-level shift Ares says would set off the guarantees, and it reaches the "
     "news months before the filings."),
    ("C", "OpenAI and Anthropic",
     ['OpenAI (funding OR revenue OR "cash burn" OR commitment OR valuation) when:30d',
      'Anthropic (funding OR revenue OR "cash burn" OR commitment OR valuation) when:30d'],
     "OpenAI or Anthropic raising less, later or at a lower valuation than planned, missing "
     "revenue targets, or cutting or renegotiating a compute commitment",
     "Much of the debt depends on them paying."),
    ("D", "Weak borrowers",
     ["CoreWeave (downgrade OR covenant OR refinancing OR default OR notes OR bond) when:30d",
      '(IREN OR TeraWulf OR "Cipher Mining" OR "Hut 8" OR "Applied Digital") (downgrade OR '
      "covenant OR refinancing OR default OR notes OR bond) when:30d"],
     "CoreWeave or a former bitcoin miner (IREN, TeraWulf, Cipher, Hut 8, Applied Digital) "
     "getting a credit-rating or bond downgrade, missing a payment, breaching or waiving a "
     "covenant, pulling a bond deal or pricing one at a much higher yield, or its bonds or loans "
     "being reported as trading at distressed levels (for example below 80 cents on the dollar or "
     "yielding over 12%), or its credit default swaps above 1,000 basis points",
     "These borrowers have to keep refinancing, so trouble shows here first."),
    ("E", "Rating agencies",
     ['(Moody\'s OR "S&P Global Ratings" OR Fitch) ("data center" OR hyperscaler OR GPU) '
      '(downgrade OR "negative outlook" OR review OR criteria) when:30d'],
     "a rating agency downgrading a data center, GPU or hyperscaler borrower or deal, putting "
     "one on negative outlook, or tightening its criteria for these deals",
     "A criteria change can strip the investment-grade rating from a whole class of "
     "structures at once."),
    ("F", "Annuity sales",
     ["LIMRA annuity sales when:180d"],
     "LIMRA reporting a quarter of total US annuity sales below the same quarter a year earlier",
     "Record annuity sales are the insurer money that buys the investment-grade AI debt "
     "(Ares's first flow)."),
]
FEEDS = {f[0]: f for f in HEADLINE_FEEDS}

CLASSIFY_INSTRUCTIONS = """\
You screen news headlines for a credit investor who is watching for signs that the AI \
data-center boom is unwinding. You do two things: group headlines that report the same event \
into stories, and decide which stories to flag.

Flag a story only if it is a concrete event that matches its section's "Flag when" line: \
something that happened or was announced, such as a cancellation, a cut, a downgrade, a \
default, a pulled deal or a reported decline. Do not flag opinion, analysis, previews, \
explainers, speculation, stock-price moves on their own, or equity analysts changing a stock \
rating (a credit-rating or bond downgrade does count). When unsure, do not flag.

Same event means the same underlying news, for example several outlets reporting one revenue \
figure or one downgrade. Two different events about the same company are different stories.

Each section lists known stories (already judged) and new headlines. For every new headline, \
write exactly one line, in one of these forms:
<id> SAME <story id>                  same event as a known story (S…)
<id> SAME <headline id>               same event as a new headline that starts a story
<id> NEW ALERT | <label> | <reason>   starts a new story that should be flagged
<id> NEW OK | <label>                 starts a new story that should not be flagged
Some sections also list stories to re-judge, because many new reports have joined them since \
they were judged; their recent headlines are shown. For each, write one line:
<story id> JUDGE ALERT | <label> | <reason>   or   <story id> JUDGE OK | <label>
<label> names the event in at most 10 words; <reason> says why it is flagged in at most \
12 words. Write nothing else."""

SLICE_EDGES = [0, 1, 3, 7, 14]  # days back; a search that hits Google's cap is split here
REJUDGE_MIN_NEW = 5  # a story with this many reports added since it was judged is judged again
NEW_DAYS = 3  # a story counts as new only if first reported within this many days


def headline_key(title):
    return " ".join(title.lower().split())


def criteria_hash(fid):
    """Changes whenever the instructions or a feed's line change, so stories
    judged under the old wording are re-judged."""
    text = CLASSIFY_INSTRUCTIONS + FEEDS[fid][3] + DEEPSEEK_MODEL_NAME
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def window_days(query):
    return int(re.search(r"when:(\d+)d", query).group(1))


def search(query):
    """One Google News search over its when:Nd window, as a list of date slices.
    Google returns at most 100 results per search, so a busy search is split into
    slices to reach back over the whole window."""
    days = window_days(query)
    oldest = NOW - timedelta(days=days)
    within = lambda items: [h for h in items if h["when"] >= oldest]  # when:Nd is loose
    first = news(query)
    if len(first) < 95:
        return [within(first)]
    base = re.sub(r"\s*when:\d+d", "", query)
    today = TODAY
    edges = [e for e in SLICE_EDGES if e < days] + [days]
    return [within(news(f"{base} after:{(today - timedelta(days=far)).isoformat()} "
                        f"before:{(today - timedelta(days=near - 1)).isoformat()}"))
            for near, far in zip(edges, edges[1:])]


def load_store(today):
    store = load_json_store(HEADLINE_STORE) or {"created": today}
    for k, v in (("created", today), ("next_story", 1), ("items", {}), ("stories", {})):
        store.setdefault(k, v)
    return store


def save_store(store):
    save_json_store(HEADLINE_STORE, store)


def feed_quotas(available, limit):
    """{feed: how many headlines it may keep}: feeds with no more than an equal share of the cap
    keep everything, and the busier ones split what is left evenly."""
    quota, left = {}, dict(available)
    while left:
        room = limit - sum(quota.values())
        small = {f: n for f, n in left.items() if n <= room // len(left)}
        if not small:
            for i, f in enumerate(left):
                quota[f] = room // len(left) + (i < room % len(left))
            break
        quota.update(small)
        left = {f: n for f, n in left.items() if f not in small}
    return quota


def cap_round_robin(lists, limit):
    """Takes items newest-first from each list in turn, so no date slice is starved."""
    picked = [[] for _ in lists]
    total = 0
    while total < limit and any(len(p) < len(l) for p, l in zip(picked, lists)):
        for p, l in zip(picked, lists):
            if len(p) < len(l) and total < limit:
                p.append(l[len(p)])
                total += 1
    return picked


@functools.lru_cache(maxsize=None)
def check_model(task):
    """Raises if deepseek-flash no longer points at the model this script was checked against."""
    models = {m["id"]: m.get("name") for m in deepseek("/models")["data"]}
    if models.get(DEEPSEEK_MODEL) != DEEPSEEK_MODEL_NAME:
        raise RuntimeError(
            f"{DEEPSEEK_MODEL} is now {models.get(DEEPSEEK_MODEL) or 'not offered'}, not "
            f"{DEEPSEEK_MODEL_NAME}, so {task} is paused. Change DEEPSEEK_MODEL_NAME in "
            "the script to accept the new model.")


def classify(sections):
    """One DeepSeek call that groups and judges every new headline.
    `sections` is [(feed id, [(story id, story)], [new headline], [(story id, story, [headline])])],
    the last list being stories to re-judge. Returns ({id: (verdict, label, reason)} for new
    stories and re-judged ones, {id: reference} for same-event lines, tokens used)."""
    check_model("classification")
    parts = []
    for fid, known_stories, new, rejudge in sections:
        text = f"## {FEEDS[fid][1]}\nFlag when: {FEEDS[fid][3]}\n\nKnown stories:\n"
        text += "\n".join(f"{sid} [{'flagged' if s['verdict'] == 'alert' else 'not flagged'}] "
                          f"{s['label']}" for sid, s in known_stories) or "(none)"
        text += "\n\nNew headlines:\n"
        text += "\n".join(f"{h['id']}. {h['title']} ({h['date']})" for h in new) or "(none)"
        if rejudge:
            text += "\n\nStories to re-judge:\n" + "\n".join(
                f"{sid} [{'flagged' if st_['verdict'] == 'alert' else 'not flagged'}] {st_['label']}: "
                + " / ".join(f"{h['title']} ({h['date']})" for h in hs[-8:]) for sid, st_, hs in rejudge)
        parts.append(text)
    r = deepseek("/chat/completions", {
        "model": DEEPSEEK_MODEL, "reasoning_effort": "high", "max_tokens": 64000,
        "messages": [{"role": "system", "content": CLASSIFY_INSTRUCTIONS},
                     {"role": "user", "content": "\n\n".join(parts)}]})
    choice = r["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise RuntimeError(f"the model stopped early ({choice.get('finish_reason')})")
    new_stories, same = {}, {}
    for row in (choice["message"].get("content") or "").splitlines():
        m = re.match(r"\s*(S\d+)\s+JUDGE\s+(ALERT|OK)\s*\|\s*([^|]*?)\s*(?:\|\s*(.*?))?\s*$", row)
        if m:
            new_stories[m.group(1)] = (m.group(2), m.group(3), m.group(4) or "")
            continue
        m = re.match(r"\s*([A-F]\d+)\s+NEW\s+(ALERT|OK)\s*\|\s*([^|]*?)\s*(?:\|\s*(.*?))?\s*$", row)
        if m:
            new_stories[m.group(1)] = (m.group(2), m.group(3), m.group(4) or "")
            continue
        m = re.match(r"\s*([A-F]\d+)\s+SAME\s+([A-F]\d+|S\d+)\s*$", row)
        if m:
            same[m.group(1)] = m.group(2)
    if not new_stories and not same:
        raise RuntimeError("the reply had no usable lines")
    return new_stories, same, r.get("usage", {}).get("total_tokens", 0)


@functools.lru_cache(maxsize=None)
def headlines():
    """Fetches every feed once, groups headlines about the same event into stories,
    and judges new headlines in a single DeepSeek call. Results are remembered, so
    each headline is judged once; a story that gains REJUDGE_MIN_NEW reports after it was
    judged is judged again. Returns ({feed id: {"stories": [...], "error": str or None,
    "capped": bool, "available": int}}, status note)."""
    today = TODAY.isoformat()
    store = load_store(today)
    known, stories = store["items"], store["stories"]

    # One bucket per feed and date slice. Each feed gets a fair share of the cap (not one in
    # proportion to how many slices its searches hit), and its slices share that evenly.
    buckets, errors, taken = {fid: [] for fid in FEEDS}, {fid: [] for fid in FEEDS}, set()
    available = {fid: 0 for fid in FEEDS}
    for fid, _, queries, _, _ in HEADLINE_FEEDS:
        for q in queries:
            try:
                slices = search(q)
            except Exception as e:
                errors[fid].append(short_error(e))
                continue
            for part in slices:
                bucket = []
                for h in sorted(part, key=lambda h: h["when"], reverse=True):
                    k = headline_key(h["title"])
                    if k not in taken:
                        taken.add(k)
                        bucket.append(dict(h, key=k, feed=fid))
                available[fid] += len(bucket)
                buckets[fid].append(bucket)
    quotas = feed_quotas(available, MAX_HEADLINES)
    picked = [h for fid, bs in buckets.items() for b in cap_round_robin(bs, quotas[fid]) for h in b]

    current = lambda sid, fid: (sid in stories and stories[sid]["feed"] == fid
                                and stories[sid]["criteria"] == criteria_hash(fid))
    same_title = lambda key: key.rsplit(" - ", 1)[0]  # the same headline from another outlet
    by_feed, pending, twins = {}, [], {}
    for fid in FEEDS:
        items = sorted((h for h in picked if h["feed"] == fid), key=lambda h: h["when"],
                       reverse=True)
        by_title = {same_title(h["key"]): known[h["key"]]["story"] for h in items
                    if h["key"] in known and current(known[h["key"]].get("story"), fid)}
        first_of = {}
        for n, h in enumerate(items, 1):
            h.update(id=f"{fid}{n}", date=h["when"].date().isoformat())
            rec = known.setdefault(h["key"], {"first_seen": today})
            rec["last_seen"] = today
            if current(rec.get("story"), fid):
                continue
            rec.pop("story", None)
            t = same_title(h["key"])
            if t in by_title:          # syndicated copy of a judged headline: same story
                rec["story"] = by_title[t]
            elif t in first_of:        # syndicated copy of another new headline: follows it
                twins[h["id"]] = first_of[t]
            else:
                first_of[t] = h["id"]
                pending.append(h)
        by_feed[fid] = items

    # Stories that have gained many reports since they were judged get judged again.
    size = {}
    for items in by_feed.values():
        for h in items:
            sid = known[h["key"]].get("story")
            if sid:
                size[sid] = size.get(sid, 0) + 1
    rejudge = {}
    for sid, n in size.items():
        st_ = stories[sid]
        st_.setdefault("judged_size", n)
        if n >= st_["judged_size"] + REJUDGE_MIN_NEW:
            hs = sorted((h for h in by_feed[st_["feed"]] if known[h["key"]].get("story") == sid), key=lambda h: h["when"])
            rejudge.setdefault(st_["feed"], []).append((sid, st_, hs))

    total = len(picked)
    status = f"{total} headlines."
    if pending or rejudge:
        sections = []
        for fid, items in by_feed.items():
            new = [h for h in items if h in pending]
            if new or rejudge.get(fid):
                sids = dict.fromkeys(known[h["key"]]["story"] for h in items
                                     if "story" in known[h["key"]])
                sections.append((fid, [(sid, stories[sid]) for sid in sids], new, rejudge.get(fid, [])))
        try:
            verdicts, same, tokens = classify(sections)
            ids = {h["id"]: h for h in pending}
            assigned, rejudged = {}, 0
            for hid, (verdict, label, reason) in verdicts.items():
                if hid.startswith("S") and hid in size:  # a re-judged story
                    stories[hid].update(label=label, reason=reason, judged_size=size[hid],
                                        verdict="alert" if verdict == "ALERT" else "ok")
                    rejudged += 1
                elif hid in ids:
                    sid = f"S{store['next_story']}"
                    store["next_story"] += 1
                    stories[sid] = {"feed": hid[0], "label": label, "reason": reason,
                                    "verdict": "alert" if verdict == "ALERT" else "ok",
                                    "criteria": criteria_hash(hid[0])}
                    assigned[hid] = sid
            for _ in range(5):  # a SAME line can point at another SAME line
                for hid, ref in same.items():
                    sid = ref if ref.startswith("S") else assigned.get(ref)
                    if hid in ids and hid not in assigned and current(sid, hid[0]):
                        assigned[hid] = sid
            for hid, first in twins.items():
                if first in assigned:
                    assigned[hid] = assigned[first]
            every = {h["id"]: h for items in by_feed.values() for h in items}
            for hid, sid in assigned.items():
                known[every[hid]["key"]]["story"] = sid
            for sid in set(assigned.values()):
                stories[sid].setdefault("judged_size", sum(1 for x in assigned.values() if x == sid))
            status += f" Judged {len(assigned)} new ones in one call ({tokens:,} tokens)."
            if rejudged:
                status += f" Re-judged {rejudged} stor{'y' if rejudged == 1 else 'ies'} that had gained {REJUDGE_MIN_NEW}+ reports."
            if len(assigned) < len(pending) + len(twins):
                status += (f" {len(pending) + len(twins) - len(assigned)} were missing from the reply and "
                           "will be retried on the next run.")
        except Exception as e:
            status += f" {len(pending)} new ones couldn't be judged today: {short_error(e)}"
    else:
        status += " None new to judge today."
    capped = {fid for fid in FEEDS if sum(1 for h in picked if h["feed"] == fid) < available[fid]}
    if capped:
        status += (f" Capped at {MAX_HEADLINES} headlines in all; the oldest in the busiest searches "
                   "were dropped.")

    feeds = {}
    for fid, items in by_feed.items():
        groups = {}
        for h in items:  # unjudged headlines stand alone
            groups.setdefault(known[h["key"]].get("story") or h["key"], []).append(h)
        out = []
        for gid, hs in groups.items():
            s_ = stories.get(gid)
            hs.sort(key=lambda h: h["when"])
            first_seen = min(known[h["key"]]["first_seen"] for h in hs)
            fresh = hs[0]["when"] >= NOW - timedelta(days=NEW_DAYS)
            out.append({"headlines": hs, "verdict": s_["verdict"] if s_ else None,
                        "reason": s_["reason"] if s_ else "", "latest": hs[-1]["when"],
                        "new": first_seen == today and today != store["created"] and fresh})
        out.sort(key=lambda g: g["latest"], reverse=True)
        feeds[fid] = {"stories": out, "error": "; ".join(errors[fid]) or None, "capped": fid in capped,
                      "available": available[fid]}

    cutoff = (TODAY - timedelta(days=STORE_DAYS)).isoformat()
    store["items"] = {k: v for k, v in known.items() if v.get("last_seen", "") >= cutoff}
    used = {v.get("story") for v in store["items"].values()}
    store["stories"] = {sid: s_ for sid, s_ in stories.items() if sid in used}
    save_store(store)
    return feeds, status


def headline_link(h):
    return f'{esc(h["date"])} · <a href="{esc(h["link"])}">{esc(h["title"])}</a>'


def story_item(s):
    """One story: its first report, with the other reports folded underneath."""
    first, more = s["headlines"][0], s["headlines"][1:]
    tags = '<span class="bad">ALERT</span> ' if s["verdict"] == "alert" else ""
    tags += "<b>NEW</b> " if s["new"] else ""
    reason = (f' <span class="muted">— {esc(s["reason"])}</span>'
              if s["verdict"] == "alert" and s["reason"] else "")
    extra = ""
    if more:
        extra = (f"<details><summary>{len(more)} more report{'s' * (len(more) > 1)}, "
                 f"latest {esc(more[-1]['date'])}</summary><ul>"
                 + "".join(f"<li>{headline_link(h)}</li>" for h in more) + "</ul></details>")
    return f"<li>{tags}{headline_link(first)}{reason}{extra}</li>"


def feed_block(fid):
    """Stories for one feed: flagged first, then the most recent, the rest folded
    away. Returns (html, crossed, status detail)."""
    feed = headlines()[0][fid]
    items = feed["stories"]
    flagged = [s for s in items if s["verdict"] == "alert"]
    rest = [s for s in items if s["verdict"] != "alert"]
    unjudged = [s for s in items if s["verdict"] is None]
    n_headlines = sum(len(s["headlines"]) for s in items)
    dates = sorted(h["date"] for s_ in items for h in s_["headlines"])
    span = f"from {dates[0]} to {dates[-1]}" if dates else "none found"
    out = (f'<p class="muted">{n_headlines} headlines {span}, grouped into {len(items)} stories'
           + (f" (searches cover the last {window_days(FEEDS[fid][2][0])} days, but the cap on headlines "
              "dropped the oldest here)" if feed["capped"] else "") + ".</p>")
    if feed["error"]:
        out += f'<p class="error">Some searches failed today: {esc(feed["error"])}</p>'
    if flagged:
        out += "<ul>" + "".join(map(story_item, flagged)) + "</ul>"
    if rest:
        out += "<ul>" + "".join(map(story_item, rest[:VISIBLE_HEADLINES])) + "</ul>"
    more = rest[VISIBLE_HEADLINES:]
    if more:
        out += (f"<details><summary>{len(more)} more stories "
                f"({sum(s['new'] for s in more)} new today)</summary><ul>"
                + "".join(map(story_item, more)) + "</ul></details>")
    detail = []
    if flagged:
        detail.append(f"{len(flagged)} flagged {'story' if len(flagged) == 1 else 'stories'} "
                      f"({sum(s['new'] for s in flagged)} new today). The model can be wrong; "
                      "read them.")
    elif items and not unjudged and feed["capped"]:
        detail.append(f"Nothing flagged among the newest {n_headlines} of {feed['available']} headlines, but "
                      "the cap on headlines left out the oldest, so check by hand.")
    elif items and not unjudged:
        detail.append(f"Nothing flagged among {len(items)} stories.")
    if unjudged:
        detail.append(f"{len(unjudged)} headlines not judged yet (see the note at the top of "
                      "the Headlines section).")
    if feed["error"] and not flagged:
        detail.append("Some searches failed, so stories may be missing: check by hand.")
    crossed = True if flagged else (None if unjudged or feed["error"] or feed["capped"] else False)
    return out, crossed, " ".join(detail)


# ---------- LIMRA quarterly annuity sales ----------
#
# LIMRA has no data feed, but every news release is listed in its sitemap and states the quarter's
# total in prose, worded differently each time. DeepSeek reads each release once and copies out the
# figures with the sentence they come from; a figure is used only if that sentence is in the
# release word for word and contains the exact total and change. Releases are remembered in
# LIMRA_STORE, so each is read once.

LIMRA_SITEMAP = "https://www.limra.com/sitemap.xml"
LIMRA_STORE = os.path.join(HERE, "data", "limra.json")
LIMRA_YEARS = 3  # releases this many calendar years back are read
QUARTER_WORDS = {1: "first quarter", 2: "second quarter", 3: "third quarter", 4: "fourth quarter"}
UP_WORDS = ("increase", "higher", "above", "up", "jump", "rose", "grew", "growth", "gain", "rise")
DOWN_WORDS = ("decrease", "lower", "below", "down", "fell", "decline", "drop", "slip", "dip")
STATUS_RANK = {"final": 1, "preliminary": 0}
LIMRA_CHANGE_SLACK = 3  # points a stated change may differ from the one computed from the totals
LIMRA_LAG_DAYS = 40     # days after a quarter ends by which LIMRA has published preliminary figures
LIMRA_LEAD_CHARS = 600  # a release's opening lines, which report total sales before any product line

LIMRA_INSTRUCTIONS = """\
You extract figures from a LIMRA news release about U.S. annuity sales.

Report every calendar quarter for which the release states TOTAL U.S. annuity sales (all products \
combined) for that single quarter. Ignore product lines (fixed indexed, RILA, variable, fixed-rate \
deferred, income annuities and so on), half-year, year-to-date and full-year totals, and forecasts.

For each such quarter output exactly one line:
QUARTER YYYY-Qn | TOTAL $X billion | CHANGE <change> | EVIDENCE <sentence>

TOTAL is the dollar figure exactly as written in the release. CHANGE is the change from the same \
quarter one year earlier, copied exactly as written (for example "1% increase", "2% below", \
"down 12%", "level with"), or NONE if the release doesn't state it. A change against the previous \
quarter is not a year-over-year change. EVIDENCE is the one sentence that states the total, copied \
exactly, character for character.

If the release states no single-quarter total, output only: NONE"""


def release_text(raw):
    """(plain text of a LIMRA release from its dateline on, release date, year LIMRA's sales
    definition changed if the release says so). The date comes from the dateline ("WINDSOR,
    Conn., June 11, 2026 —"), falling back to the page's own date."""
    s = re.sub(r"(?s)<(script|style).*?</\1>", " ", raw.decode("utf-8", "replace"))
    t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())
    released = None
    m = re.search(r"WINDSOR, Conn\.,?\s*([A-Z][a-z]+)\.?\s+(\d{1,2}),\s*(20\d\d)", t)
    if m:
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                released = datetime.strptime(f"{m.group(1)[:3] if fmt == '%b %d %Y' else m.group(1)} {m.group(2)} {m.group(3)}", fmt).date()
                break
            except ValueError:
                pass
    if released is None:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", t)
        released = date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
    d = re.search(r"Beginning with the \w+ (20\d\d) report, LIMRA updated its sales definition", t)
    i = t.find("WINDSOR, Conn.")
    return (t[i:] if i >= 0 else t)[:15000], released, int(d.group(1)) if d else None


ABBREVIATIONS = re.compile(r"\b(U\.S|Inc|Conn|Corp|Co|No|vs|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.")
PRODUCT_WORDS = re.compile(r"\b(fixed[- ]indexed|indexed|variable|fixed[- ]rate|fixed|registered|rila|fia|frd|va|"
                           r"spia|dia|income|immediate|deferred|traditional|structured|buffer)\b")
PART_YEAR = re.compile(r"first half|six months|nine months|year[- ]to[- ]date|\bYTD\b|full[- ]year|for the year|annual",
                       re.I)


def yoy_cue(year, q):
    """Words that tie a change to the same quarter a year earlier."""
    w = QUARTER_WORDS[q]
    return re.compile(rf"year[- ]over[- ]year|same (period|quarter)|prior[- ]year|(a|the) year (earlier|ago|before)|"
                      rf"{w}( of)? {year - 1}|{w} {year - 1}", re.I)


def parse_change(change, evidence, year=None, q=None):
    """Signed % change from the CHANGE field, or None unless the evidence sentence states it
    unambiguously as a change from the same quarter a year earlier."""
    if not change or change.upper() == "NONE" or change not in evidence:
        return None
    if year is not None:
        # The year-ago reference has to sit in the change's own clause: "up 17% from the first
        # quarter and down 9% from the second quarter of 2025" makes only the second one count.
        pos = evidence.index(change)
        clause = change + re.split(r",| and | but |;", evidence[pos + len(change):], maxsplit=1)[0]
        if not yoy_cue(year, q).search(clause):
            return None
    low = change.lower()
    if re.search(r"\b(level|flat|unchanged)\b", low) and "%" not in low:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", low)
    if not m:
        return None
    up = any(re.search(rf"\b{w}", low) for w in UP_WORDS)
    down = any(re.search(rf"\b{w}", low) for w in DOWN_WORDS)
    if up == down:
        return None
    return float(m.group(1)) * (1 if up else -1)


def verify_quarter(line, text, released):
    """One model line -> {quarter, total, yoy, evidence} if it checks out against the text: the
    evidence is a single sentence copied word for word, about total annuity sales (not a product
    line, a half-year or year-to-date figure), naming the quarter and containing the exact total."""
    m = re.match(r"\s*(?:[-*•]\s*)?(?:QUARTER\s+)?(20\d\d)-Q([1-4])\s*\|\s*TOTAL\s+\$([\d.,]+)\s*billion\s*\|"
                 r"\s*CHANGE\s+(.*?)\s*\|\s*EVIDENCE\s+(.+?)\s*$", line)
    if not m:
        return None
    year, q, total_s, change, evidence = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4), m.group(5)
    evidence = evidence.strip('"“” ')
    total = float(total_s.replace(",", ""))
    masked = ABBREVIATIONS.sub(r"\1", evidence).rstrip('.”" ')
    # About total sales: "annuity sales" with no product name in front, or, in the release's
    # opening lines (which report totals before any product line), "sales" with none.
    i = evidence.lower().find("annuity sales")
    if i < 0 and 0 <= text.find(evidence) < LIMRA_LEAD_CHARS:
        i = evidence.lower().find("sales")
    ok = (evidence in text                                      # copied, not paraphrased
          and len(evidence) <= 400
          and not re.search(r"[.!?][”\"']?\s+[A-Z“\"]", masked)  # one sentence, not a stretch of text
          and f"${total_s} billion" in evidence                 # the total is in that sentence
          and QUARTER_WORDS[q] in evidence.lower()              # and so is the quarter
          and i >= 0 and not PRODUCT_WORDS.search(evidence.lower()[max(0, i - 40):i])  # total sales
          and not PART_YEAR.search(evidence)                    # not a half-year or year-to-date total
          and 50 <= total <= 300                                # a market total, not a product line
          and released is not None and year in (released.year, released.year - 1))
    if not ok:
        return None
    return dict(quarter=f"{year}-Q{q}", total=total, yoy=parse_change(change, evidence, year, q), evidence=evidence)


def read_release(url):
    """Fetches one release and extracts its quarterly totals with DeepSeek."""
    text, released, definition = release_text(fetch(url))
    # Early figures say "according to preliminary results"; later releases revise them.
    status = "preliminary" if re.search(r"\bpreliminary results\b", text, re.I) else "final"
    rec = dict(released=released.isoformat() if released else None, status=status, quarters=[],
               definition_change=definition, model=DEEPSEEK_MODEL_NAME)
    if released is None:
        return rec
    check_model("reading LIMRA releases")
    r = deepseek("/chat/completions", {
        "model": DEEPSEEK_MODEL, "reasoning_effort": "high", "max_tokens": 16000,
        "messages": [{"role": "system", "content": LIMRA_INSTRUCTIONS},
                     {"role": "user", "content": text}]})
    choice = r["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise RuntimeError(f"the model stopped early ({choice.get('finish_reason')})")
    for line in (choice["message"].get("content") or "").splitlines():
        if re.search(r"20\d\d-Q[1-4]\s*\|", line):  # the model sometimes drops "QUARTER"
            q = verify_quarter(line, text, released)
            if q:
                rec["quarters"].append(q)
            else:
                rec.setdefault("rejected", []).append(line.strip()[:300])
    return rec


def limra_quarters():
    """({quarter: chosen figure}, [problems], info) from every annuity release in the window. Per
    quarter, a final figure beats a preliminary one, then the later release wins. info holds the
    year LIMRA changed its sales definition (or None), how far final changes have moved from
    preliminary ones, and how many figures failed the checks. A stated change that disagrees
    with the two totals by more than LIMRA_CHANGE_SLACK points is dropped."""
    store = load_json_store(LIMRA_STORE)
    problems = []
    first_year = TODAY.year - LIMRA_YEARS
    xml = fetch(LIMRA_SITEMAP).decode("utf-8", "replace")
    for url in re.findall(r"<loc>([^<]+)</loc>", xml):
        low = url.lower()
        if "/newsroom/news-releases/" not in low or "annuit" not in low:
            continue
        y = re.search(r"/news-releases/(20\d\d)/", url)
        if url in store or (y and int(y.group(1)) < first_year):
            continue
        try:
            store[url] = read_release(url)
        except Exception as e:  # leave it out of the store so the next run tries again
            problems.append(f"{url}: {short_error(e)}")
    save_json_store(LIMRA_STORE, store)
    best, by_quarter = {}, {}
    for url, rec in store.items():
        for q in rec["quarters"]:
            cand = dict(q, url=url, status=rec["status"], released=rec["released"])
            by_quarter.setdefault(q["quarter"], []).append(cand)
            key = (STATUS_RANK[rec["status"]], rec["released"] or "")
            cur = best.get(q["quarter"])
            if cur is None or key > (STATUS_RANK[cur["status"]], cur["released"] or ""):
                best[q["quarter"]] = cand
    breaks = [rec.get("definition_change") for rec in store.values() if rec.get("definition_change")]
    definition = min(breaks) if breaks else None
    across = lambda q: definition is not None and int(q[:4]) == definition  # compares new with old
    for q, v in best.items():
        prev = best.get(f"{int(q[:4]) - 1}{q[4:]}")
        if prev and not across(q):
            computed = (v["total"] / prev["total"] - 1) * 100
            if v["yoy"] is None:
                v["yoy"], v["computed"] = computed, True
            elif abs(v["yoy"] - computed) > LIMRA_CHANGE_SLACK:
                problems.append(f"{q}: LIMRA's stated change ({v['yoy']:+.0f}%) doesn't match its totals "
                                f"({computed:+.0f}%), so it isn't used")
                v["yoy"] = None
    revisions = [abs(f["yoy"] - p["yoy"]) for vs in by_quarter.values()
                 for f in vs if f["status"] == "final" and f["yoy"] is not None
                 for p in vs if p["status"] == "preliminary" and p["yoy"] is not None]
    rejected = sum(len(rec.get("rejected", [])) for rec in store.values()
                   if (rec.get("released") or "") >= f"{first_year + 1}")
    return best, problems, dict(definition=definition, revision=max(revisions, default=None), rejected=rejected)


def next_quarter(q):
    y, n = int(q[:4]), int(q[-1])
    return f"{y + n // 4}-Q{n % 4 + 1}"


def quarter_end(q):
    y, n = int(q[:4]), int(q[-1])
    return date(y + n // 4, n % 4 * 3 + 1, 1) - timedelta(days=1)


def expected_limra_quarter(today):
    """The newest quarter LIMRA should have reported by today: preliminary figures come about
    four weeks after a quarter ends."""
    y, n = today.year, (today.month - 1) // 3  # quarters fully ended this year
    q = f"{y}-Q{n}" if n else f"{y - 1}-Q4"
    if (today - quarter_end(q)).days < LIMRA_LAG_DAYS:
        q = f"{int(q[:4]) - 1}-Q4" if q[-1] == "1" else f"{q[:4]}-Q{int(q[-1]) - 1}"
    return q


def fmt_change(v):
    """A whole-percent change with its sign, and plain "0%" when it rounds to zero."""
    return "0%" if round(v) == 0 else f"{v:+.0f}%"


def block_annuities():
    rule = ("LIMRA's quarterly total U.S. annuity sales come in below the same quarter a year "
            "earlier, two quarters in a row. Annuity sales bring in much of the insurer money that "
            "Ares says buys the investment-grade AI debt (its first flow).")
    try:
        best, problems, info = limra_quarters()
    except Exception as e:
        best, problems, info = {}, [f"LIMRA releases unavailable today: {short_error(e)}"], {}
    definition = info.get("definition")
    across = lambda q: definition is not None and int(q[:4]) == definition
    out = ""
    quarters = sorted(best)
    if quarters:
        rows = []
        for q in reversed(quarters[-6:]):
            v = best[q]
            yoy = "not stated" if v["yoy"] is None else fmt_change(v["yoy"]) + (" (computed)" if v.get("computed") else "")
            if v["yoy"] is not None and v["yoy"] < 0:
                yoy = f'<span class="bad">{yoy}</span>'
            if across(q) and v["yoy"] is not None:
                yoy += " †"
            rows.append([f"Q{q[-1]} {q[:4]}", f"${v['total']:,.1f}B", yoy, v["status"],
                         f'<a href="{esc(v["url"])}">{esc(v["released"])}</a>'])
        out += table(["Quarter", "Total U.S. annuity sales", "vs. same quarter a year earlier",
                      "Figures", "LIMRA release"], rows)
    expected = expected_limra_quarter(TODAY)
    crossed, detail = None, ""
    if len(quarters) < 2:
        detail = "Too few quarters found."
    elif expected not in best:
        detail = (f"LIMRA's figures for {expected} should be out by now but weren't found (the release may be "
                  "missing from LIMRA's sitemap, or its figures failed the checks), so check by hand.")
    else:
        a, b = best[quarters[-2]], best[quarters[-1]]
        if next_quarter(quarters[-2]) != quarters[-1]:
            detail = f"{quarters[-2]} and {quarters[-1]} aren't consecutive quarters, so check by hand."
        elif a["yoy"] is None or b["yoy"] is None:
            detail = "A year-over-year change is missing for one of the last two quarters, so check by hand."
        elif across(quarters[-1]) or across(quarters[-2]):
            detail = (f"Last two quarters: {fmt_change(a['yoy'])} and {fmt_change(b['yoy'])} as LIMRA states them, "
                      f"but LIMRA changed its sales definition from its January {definition} report (sales now "
                      "include internal transfers between a company's own contracts), and its "
                      f"{definition} changes compare with {definition - 1} figures as first published, which may "
                      "use the old definition. The comparison may not be like for like, so check by hand.")
        else:
            crossed = a["yoy"] < 0 and b["yoy"] < 0
            detail = f"Last two quarters: {fmt_change(a['yoy'])} and {fmt_change(b['yoy'])} year over year."
            if b["status"] == "preliminary":
                rev = info.get("revision")
                detail += (" The latest figures are preliminary" +
                           (f"; LIMRA's final figures have moved such changes by up to {rev:.0f} points." if rev else "."))
    out += line(rule, crossed, detail)
    for p in problems:
        out += f'<p class="error">{esc(p)}</p>'
    news, _, news_detail = feed_block("F")
    out += f"<h4>Annuity sales news</h4>{news}"
    if news_detail:
        out += f'<p class="muted">{news_detail}</p>'
    rejected = info.get("rejected")
    return out + (
        '<p class="muted">Source: <a href="https://www.limra.com/en/newsroom/news-releases/">LIMRA '
        "news releases</a>, found through LIMRA's sitemap. "
        f"{esc(DEEPSEEK_MODEL_NAME)} copies each quarter's total and year-over-year change from the "
        "release text; a figure is used only if the sentence it quotes appears in the release word "
        "for word, is a single sentence about total annuity sales (not a product line, half-year or "
        "year-to-date figure), and contains that exact total and quarter, and a change only if the "
        "sentence ties it to the same quarter a year earlier and it agrees with the totals. Final "
        "figures replace preliminary ones. If anything is missing, out of date or not like for like, "
        "the line falls back to checking by hand."
        + (f" {rejected} figure{'s' * (rejected != 1)} read from recent releases failed the checks and "
           f"{'were' if rejected != 1 else 'was'} left out." if rejected else "")
        + (" † compares across LIMRA's change of definition." if definition else "") + "</p>")


def block_news():
    out = ('<p class="muted">Headlines reporting the same event are grouped into one story, '
           "listed under its first report. ALERT = "
           f"{esc(DEEPSEEK_MODEL_NAME)} judged that the story is a concrete event matching "
           f"that section's line; it can be wrong, so read the story. NEW = first seen by this dashboard "
           f"today and first reported in the last {NEW_DAYS} days. "
           f"{esc(headlines()[1])}</p>")
    for fid in "ABCDE":
        _, title, _, flag_when, why = FEEDS[fid]
        html_, crossed, detail = feed_block(fid)
        out += f"<h3>{esc(title)}</h3>" + html_
        out += line(f"a story reports {flag_when}. {why}", crossed, detail)
    return out + source("Google News search", "https://news.google.com/")


SECTIONS = [
    ("1. Credit", [
        ("Corporate bond spreads", block_spreads),
        ("Credit default swaps: market-implied default probability", block_cds),
        ("Most heavily borrowed AI names (stock prices)", block_weak_borrowers),
    ]),
    ("2. Capex and income", [
        ("Hyperscaler capex and free cash flow", block_hyperscalers),
        ("Nvidia revenue", block_nvidia),
        ("Contract backlog at Oracle, Microsoft and Alphabet", block_backlog),
        ("GPU rental prices", block_gpu_prices),
        ("Commitments not on the balance sheet", block_commitments),
    ]),
    ("3. Insurance", [
        ("Annuity sales", block_annuities),
    ]),
    ("4. Headlines", [
        ("", block_news),
    ]),
]

NOT_HERE = (
    "Not tracked here: prices of the private placements and securitizations Ares describes, the exit "
    "clauses inside individual deals, insurers' return on equity (it is in their filings, but not as "
    "a comparable tagged figure), Microsoft's and Amazon's off-balance-sheet commitments (not tagged "
    "comparably), the leases not yet started at Alphabet and Oracle and Nvidia's supply and capacity "
    "commitments (stated only in the text of their filings). The quarterly company figures lag by up to four months. Ares's own advice is to "
    "\"reef early\": reduce sail well before the storm arrives, because \"cycles are easy, but timing "
    "is hard.\""
)

CSS = """
body{font:14px/1.45 system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#111;background:#fff}
h1{font-size:20px;margin-bottom:4px}
h2{font-size:16px;margin-top:36px;border-bottom:1px solid #bbb;padding-bottom:4px}
h3{font-size:14px;margin:22px 0 6px}
table{border-collapse:collapse;width:100%;margin:6px 0}
td,th{border:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#f3f3f3;font-weight:600}
ul{margin:6px 0;padding-left:20px}
details{margin:4px 0}
summary{cursor:pointer;color:#333}
.line{background:#f6f6f6;padding:6px 8px;margin:6px 0}
.bad{color:#b00020;font-weight:700}
.warnings{border:1px solid #b00020;background:#fff5f5;padding:6px 12px;margin:8px 0}
.warnings ul{margin:4px 0}
.muted{color:#666;font-size:12px;margin:4px 0}
h4{font-size:13px;margin:16px 0 2px}
.legend{font-size:12px;color:#52514e;margin:6px 0}
.multiples{display:flex;flex-wrap:wrap;gap:4px 12px}
.multiples>div{flex:0 1 300px;min-width:260px}
.narrow{display:none}
@media (max-width:600px){.wide{display:none}.narrow{display:block}}
.multiples h5{margin:8px 0 0;font-size:12px}
.key{display:inline-block;width:14px;height:8px;vertical-align:middle;margin:0 5px 0 12px;border-radius:2px}
.legend .key:first-child{margin-left:0}
.error{color:#b00020}
"""


def main():
    generated = f"{NOW:%Y-%m-%d %H:%M} UTC"
    out = OUT
    if CACHED:
        if not os.path.exists(FETCHED_AT):
            sys.exit("No saved responses yet: run once without --cached first.")
        generated = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC from data fetched {generated}"
        out = PREVIEW  # never the live page
    else:
        os.makedirs(HTTP_CACHE, exist_ok=True)
        with open(FETCHED_AT, "w") as f:
            f.write(f"{NOW:%Y-%m-%d %H:%M}")
    body = []
    for heading, blocks in SECTIONS:
        body.append(f"<h2>{esc(heading)}</h2>")
        for title, fn in blocks:
            if title:
                body.append(f"<h3>{esc(title)}</h3>")
            try:
                body.append(fn())
            except Exception as e:  # one broken source shouldn't sink the page
                print(f"ERROR in {title or heading}: {e!r}", file=sys.stderr)
                warn(f"{title or heading}: could not be built today")
                body.append(f'<p class="error">Could not fetch this signal today: {esc(short_error(e))}</p>')
    body.append("<h2>Not on this dashboard</h2>")
    body.append(f"<p>{esc(NOT_HERE)}</p></body></html>")
    head = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>AI Bubble Watch</title><style>{CSS}</style></head><body>",
        "<h1>AI Bubble Watch</h1>",
        f'<p class="muted">Generated {generated}. Signals from free public sources; the CDS, annuity '
        "and headline sections use a language model (DeepSeek) to read news and LIMRA releases, with the "
        "checks described there. Each signal shows a line where this dashboard suggests starting to worry; "
        "these lines are the dashboard's own, not Ares's, and there is no overall score. The signals follow "
        'Ares\'s Fall 2026 <a href="https://www.ares.com/content/dam/aresmgmt/Documents/In-the-Gaps/'
        'Ares_InTheGaps_Newsletter_Fall-2026.pdf">In the Gaps</a> newsletter.</p>',
    ]
    if WARNINGS:
        head.append('<div class="warnings"><b>Problems in this run:</b><ul>'
                    + "".join(f"<li>{esc(w)}</li>" for w in WARNINGS) + "</ul></div>")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(head + body))
    os.replace(tmp, out)
    print(f"Wrote {out}" + (f" with {len(WARNINGS)} warning(s)" if WARNINGS else ""))


if __name__ == "__main__":
    main()
