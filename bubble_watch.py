#!/usr/bin/env python3
"""Daily AI-bubble watch.

Fetches raw signals from free public sources and writes dashboard.html next
to this script. Each signal shows its own "start worrying" line; there is no
score. The signals follow Ares's Fall 2026 "In the Gaps" newsletter.

Run once a day, e.g. from cron:
    0 7 * * * SEC_USER_AGENT="Your Name you@example.com" /usr/bin/python3 /root/bubble/bubble_watch.py

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
SEC_UA = os.environ.get("SEC_USER_AGENT", "bubble-watch admin@example.com")
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64)"

# Worry lines. Edit these to taste.
BBB_WIDENING_BP = 30      # BBB spread this far above its 1-year low
HY_WIDENING_BP = 100      # High-yield spread this far above its 1-year low
BACKLOG_DROP_PCT = 5      # Backlog (RPO) falls this much in one quarter
GPU_DROP_13W_PCT = 10     # GPU on-demand median falls this much over 13 weeks

HYPERSCALERS = [  # ticker, SEC CIK, XBRL concept the company uses for capex
    ("MSFT", 789019, "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("GOOGL", 1652044, "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("AMZN", 1018724, "PaymentsToAcquireProductiveAssets"),
    ("META", 1326801, "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("ORCL", 1341439, "PaymentsToAcquirePropertyPlantAndEquipment"),
]
BACKLOG = [("ORCL", 1341439), ("MSFT", 789019), ("GOOGL", 1652044)]
NVDA_CIK = 1045810
WEAK_BORROWERS = ["CRWV", "IREN", "HUT", "CIFR", "WULF", "APLD", "ORCL"]
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
            rows.append((r[0], float(r[1]) * 100))  # percent -> basis points
    return rows


def yahoo_closes(ticker, span="1y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={span}&interval=1d"
    r = fetch_json(url)["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), c)
            for t, c in zip(r["timestamp"], closes) if c is not None]


def gpu_weekly(slug):
    """Weekly on-demand median $/GPU-hour (each provider counted once) as
    [(week start, price, providers)], oldest first, complete weeks only."""
    text = fetch(f"https://getdeploying.com/dataset/gpu-prices/{slug}.csv").decode()
    today = date.today()
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
        body = e.read()[:200].decode(errors="replace")
        raise RuntimeError(f"DeepSeek API error {e.code}: {body}") from None


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


def column_chart(cats, series, fmt, domain=None, w=300, h=200, label_last=True, aria=""):
    """Grouped columns per category on one shared axis, a vertical gridline between quarters and a
    date under each. series: [(name, color, [value per cat])]. domain=(lo, hi) shares a scale across
    small multiples. Only the last value of a single series is labeled; the rest are on hover."""
    vals = [v for _, _, vs in series for v in vs]
    lo, hi = domain or (min(0, *vals), max(0, *vals))
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
        out.append(f'<text x="{ml - 5}" y="{y(t) + 4:.1f}" text-anchor="end" fill="{MUTED}">{fmt(t, tick=True)}</text>')
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
            out.append(f'<path d="{bar_path(x, bw, y(0), y(v))}" fill="{color}"><title>{esc(name)}, quarter ending {esc(cat)}: {fmt(v)}</title></path>')
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
        out.append(f'<text x="{ml - 6}" y="{y(t) + 4:.1f}" text-anchor="end" fill="{MUTED}">{fmt(t, tick=True)}</text>')
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


# ---------- signal blocks ----------

def block_hyperscalers():
    rows, fcf_negative, capex_down, panels = [], [], [], []
    for ticker, cik, capex_concept in HYPERSCALERS:
        ocf = quarterly_flows(sec_concept(cik, "NetCashProvidedByUsedInOperatingActivities"))
        capex = quarterly_flows(sec_concept(cik, capex_concept))
        ends = [e for e in capex if e in ocf][-6:]
        last = ends[-1]
        fcf = [ocf[e] - capex[e] for e in ends]
        prior = year_earlier(capex, last)
        yoy = (capex[last] / prior - 1) * 100 if prior else None
        if fcf[-1] < 0:
            fcf_negative.append(ticker)
        if yoy is not None and yoy < 0:
            capex_down.append(ticker)
        panels.append((ticker, ends, [capex[e] / 1e9 for e in ends], [v / 1e9 for v in fcf]))
        rows.append([ticker, esc(last), pct(yoy, True)])
    domain = (min(min(f) for *_, f in panels), max(max(c) for _, _, c, _ in panels))
    out = legend([("Capex", SERIES[0]), ("Free cash flow", SERIES[1])])
    out += ('<p class="muted">Last 6 quarters by quarter-end month, $B, same scale for every '
            'company. Hover a bar for its exact value.</p><div class="multiples">')
    out += "".join(
        f"<div><h5>{t}</h5>" + column_chart(ends, [("Capex", SERIES[0], c), ("Free cash flow", SERIES[1], f)],
                                           money_b, domain,
                                           aria=f"{t} capex and free cash flow, last 6 quarters") + "</div>"
        for t, ends, c, f in panels) + "</div>"
    out += table(["Company", "Latest quarter end", "Latest capex vs same quarter a year earlier"], rows)
    out += line(
        "any company's quarterly capex is lower than in the same quarter a year earlier. "
        "This is the pull-back Ares names as the trigger. A capex-guidance cut on an earnings "
        "call comes before it shows up here, so read the calls (late Jan, Apr, Jul, Oct).",
        bool(capex_down), f"Lower than a year earlier: {', '.join(capex_down)}." if capex_down else "")
    out += line(
        "free cash flow is below zero, meaning capex is bigger than the cash the business "
        "generates and the gap is borrowed. Ares flagged this as already happening.",
        bool(fcf_negative),
        f"Negative in the latest quarter: {', '.join(fcf_negative)}." if fcf_negative else "")
    out += ('<p class="muted">Free cash flow here = operating cash flow − purchases of property '
            "and equipment, as filed. Companies' own definitions differ (e.g. finance leases).</p>")
    return out + source("SEC EDGAR XBRL filings", "https://www.sec.gov/edgar/search/")


def block_nvidia():
    rev = quarterly_flows(sec_concept(NVDA_CIK, "Revenues"))
    ends = list(rev)[-6:]
    change = (rev[ends[-1]] / rev[ends[-2]] - 1) * 100
    out = ('<p class="muted">Quarterly revenue by quarter-end month, $B. Hover a bar for its '
           'exact value.</p><div style="max-width:480px">'
           + column_chart(ends, [("Revenue", SERIES[0], [rev[e] / 1e9 for e in ends])], money_b,
                          w=480, h=220, aria="Nvidia quarterly revenue, last 6 quarters") + "</div>")
    out += line("revenue falls from the previous quarter. Nvidia's sales are AI spending arriving; "
                "a sequential drop means buyers are slowing down.",
                change < 0, f"Latest quarter vs previous: {pct(change, True)}.")
    return out + source("SEC EDGAR XBRL filings (Nvidia)",
                        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810")


def block_backlog():
    rows, dropped, panels = [], [], []
    for ticker, cik in BACKLOG:
        rpo = point_in_time(sec_concept(cik, "RevenueRemainingPerformanceObligation"))
        ends = list(rpo)[-6:]
        change = (rpo[ends[-1]] / rpo[ends[-2]] - 1) * 100
        if change <= -BACKLOG_DROP_PCT:
            dropped.append(ticker)
        panels.append((ticker, ends, [rpo[e] / 1e9 for e in ends]))
        rows.append([ticker, esc(ends[-1]), pct(change, True)])
    domain = (0, max(max(v) for *_, v in panels))
    out = ('<p class="muted">Contract backlog, last 6 quarters by quarter-end month, $B, same scale '
           'for every company. Hover a bar for its exact value.</p><div class="multiples">')
    out += "".join(f"<div><h5>{t}</h5>" + column_chart(ends, [("Backlog", SERIES[0], v)], money_b, domain,
                                                      aria=f"{t} contract backlog, last 6 quarters")
                   + "</div>" for t, ends, v in panels) + "</div>"
    out += table(["Company", "Latest quarter end", "Latest vs previous quarter"], rows)
    out += line(
        f"backlog falls more than {BACKLOG_DROP_PCT}% in a quarter at any of the three. "
        "Much of this backlog is OpenAI and Anthropic compute commitments (e.g. OpenAI's $300B "
        "with Oracle), so a drop means commitments are being cut or renegotiated. Normal dips "
        "over the last three years were under 4%.",
        bool(dropped), f"Dropped: {', '.join(dropped)}." if dropped else "")
    return out + source("SEC EDGAR XBRL filings (remaining performance obligations)",
                        "https://www.sec.gov/edgar/search/")


def block_gpu_prices():
    rows, crossed, lines = [], [], []
    for i, (slug, name) in enumerate(GPU_MODELS):
        weeks = gpu_weekly(slug)
        lines.append((name, SERIES[i], [(w, p) for w, p, _ in weeks]))
        week, price, providers = weeks[-1]

        def change(n, i=len(weeks) - 1):
            return (weeks[i][1] / weeks[i - n][1] - 1) * 100 if i >= n else None

        c13 = change(13)
        if c13 is not None and c13 <= -GPU_DROP_13W_PCT:
            crossed.append(name)
        worst = min((change(13, i) for i in range(13, len(weeks))), default=None)
        rows.append([
            name, f"${price:.2f}", f"{esc(week)} ({providers} providers)",
            pct(change(4)),
            f'<span class="bad">{pct(c13)}</span>' if name in crossed else pct(c13),
            pct(worst), pct(change(len(weeks) - 1)) + f" (since {esc(weeks[0][0])})",
        ])
    out = legend([(n, c) for n, c, _ in lines])
    out += ('<p class="muted">Weekly median on-demand price, $/GPU-hour; vertical lines mark the '
            'start of each month. Hover a line for the week and price.</p>')
    fmt = lambda v, tick=False: f"${v:.0f}" if tick else f"${v:.2f}"
    out += (f'<div class="wide">{line_chart(lines, fmt, aria="Weekly median GPU rental prices")}</div>'
            f'<div class="narrow">{line_chart(lines, fmt, w=380, h=260, label_every=3, aria="Weekly median GPU rental prices")}</div>')
    out += table(["GPU", "Latest weekly median, $/GPU-hour", "Latest week", "4 weeks", "13 weeks",
                 "Worst 13-week change in the data", "Whole series"], rows)
    out += line(
        f"the on-demand median falls {GPU_DROP_13W_PCT}% or more over 13 weeks (about 90 days). "
        "Over the past year these prices rose and their worst 13-week dips were small (see the "
        "table), so a drop this size would break the trend. Falling rental prices mean the chips "
        "backing GPU loans are losing value faster than lenders assumed. (Ares's chart covers "
        "2023–25, when H100 rentals fell from about $8.50 to $3.00 as the chip shortage eased.)",
        bool(crossed), f"Crossed: {', '.join(crossed)}." if crossed else "")
    return out + (
        '<p class="muted">Source: <a href="https://getdeploying.com/dataset/gpu-prices">'
        "GetDeploying, GPU rental price history</a>, "
        '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. Weekly median '
        "on-demand price across cloud providers, each provider counted once; the current week "
        "is left out until it is complete.</p>")


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


def block_cds():
    today = date.today()
    start = today - timedelta(days=cds_signal.WINDOW_DAYS)
    data = cds_signal.series(today, download=not CACHED)
    out = ('<p class="legend"><span class="key" style="background:#2a78d6;border:2px solid #2a78d6;'
           'width:6px;height:6px;border-radius:50%"></span>5-year default '
           'probability, monthly median (left axis, from 0%); bar = 90% interval '
           '<span class="key" style="background:#fff;border:2px solid #2a78d6;width:6px;height:6px;'
           'border-radius:50%"></span>hollow = payment direction assumed (see note below) '
           '<span class="key" style="background:#8f8d87;height:2px"></span>Stock price, daily close '
           "(right axis, log scale)</p>")
    rows = []
    for ticker, name in CDS_NAMES:
        try:
            closes = [(date.fromisoformat(d), c) for d, c in yahoo_closes(ticker, "2y")]
            note = ""
        except Exception as e:
            closes, note = [], f'<p class="error">Stock price unavailable today: {esc(e)}</p>'
        series, by_rec = data[ticker]
        out += f"<h4>{esc(name)} ({ticker})</h4>" + cds_signal.svg_chart(ticker, series, closes, start, today) + note
        idx = [i for i, s in enumerate(series) if s[1] is not None]
        if not idx:
            rows.append([f"{esc(name)} ({ticker})", "no trades", "n/a", "0"])
            continue
        m, med, lo, hi, n, assumed = series[idx[-1]]
        rec_lo, rec_hi = (by_rec.get(r) for r in cds_signal.RECOVERY_RANGE)
        rec = (f"{rec_lo * 100:.1f}–{rec_hi * 100:.1f}%" if rec_lo is not None and rec_hi is not None
               else "n/a")
        rows.append([f"{esc(name)} ({ticker})",
                     f"{med * 100:.1f}% ({lo * 100:.1f}–{hi * 100:.1f}%), {m:%b %Y}" + " *" * assumed,
                     rec, str(n)])
    rec_lo, rec_hi = (f"{r:.0%}" for r in cds_signal.RECOVERY_RANGE)
    out += table(["Company", "5-year default probability, latest month (90% interval)",
                  f"Same month at {rec_lo}–{rec_hi} recovery", "Trades in latest month"], rows)
    out += no_line(
        "This is what credit default swap buyers pay for protection against each company's "
        "bankruptcy or failure to pay on its own debt within 5 years (a restructuring doesn't "
        "count, nor does a default by a separate financing subsidiary). It is a market price, "
        "not a forecast: it includes a premium for bearing the risk, so true odds are lower, and "
        f"it assumes lenders recover {cds_signal.R:.0%} in a default. That assumption matters: "
        f"the recovery column shows the same month priced at {rec_lo} and {rec_hi} recovery "
        "(lower recovery means a lower implied default probability for the same price). Worry when it climbs for several months "
        "in a row, and most when CoreWeave and Oracle climb together while Nvidia stays flat, "
        "which points to the AI financing chain rather than chip demand.")
    return out + (
        '<p class="muted">Source: <a href="https://pddata.dtcc.com/ppd/secdashboard">DTCC public '
        "security-based swap data</a> (SEC Regulation SBSR) and Yahoo Finance daily closes. Uses "
        "US-dollar standard-coupon trades with 4–6 years left, priced from their quoted spread or, "
        'failing that, their up-front payment and exact size (trades reported only as "$5,000,000+" '
        "without a spread are left out). Some spreads are reported 100 times too small; these are "
        "scaled up only when the result matches properly reported quotes within "
        f"{cds_signal.RESCALE_DAYS} days, and otherwise left out. DTCC reports an up-front's size but not who paid it: "
        "the buyer pays when the spread is above the contract's fixed coupon, the seller when "
        "it is below. A nearby quoted spread settles the direction; where none exists since the "
        "spread last came near the coupon, the buyer is assumed to pay (hollow dots, * in the "
        "table). Months with no trades are left blank. Each chart has its own "
        "default-probability scale.</p>")


def block_weak_borrowers():
    rows = []
    for ticker in WEAK_BORROWERS + ["QQQ"]:
        closes = [c for _, c in yahoo_closes(ticker)]
        last = closes[-1]
        r1m = (last / closes[-22] - 1) * 100 if len(closes) > 22 else None
        r3m = (last / closes[-64] - 1) * 100 if len(closes) > 64 else None
        dd = (last / max(closes) - 1) * 100
        label = "QQQ (Nasdaq-100, for comparison)" if ticker == "QQQ" else ticker
        rows.append([label, f"{last:,.2f}", pct(r1m), pct(r3m), pct(dd)])
    out = table(["Ticker", "Last close", "1 month", "3 months", "From 52-week high"], rows)
    out += no_line(
        "These are the weakest borrowers in Ares's map (CoreWeave and the former bitcoin miners) "
        "plus Oracle. Their stocks are a free stand-in for their bond prices, which usually move "
        "first but aren't freely available. They routinely swing 50% or more without any credit "
        "event, so a fixed line would fire constantly. Worry when they fall together while QQQ "
        "holds up, which points to the AI financing chain and not the whole market. Also worry "
        "when a fall comes with news of a pulled bond deal, a downgrade or a covenant problem "
        "(see headlines below).")
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
     "Microsoft, Alphabet, Amazon, Meta or Oracle lowering its capex plan or guidance",
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
     "covenant, or pulling a bond deal or pricing one at a much higher yield",
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
<label> names the event in at most 10 words; <reason> says why it is flagged in at most \
12 words. Write nothing else."""

SLICE_EDGES = [0, 1, 3, 7, 14]  # days back; a search that hits Google's cap is split here


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
    oldest = datetime.now(timezone.utc) - timedelta(days=days)
    within = lambda items: [h for h in items if h["when"] >= oldest]  # when:Nd is loose
    first = news(query)
    if len(first) < 95:
        return [within(first)]
    base = re.sub(r"\s*when:\d+d", "", query)
    today = date.today()
    edges = [e for e in SLICE_EDGES if e < days] + [days]
    return [within(news(f"{base} after:{(today - timedelta(days=far)).isoformat()} "
                        f"before:{(today - timedelta(days=near - 1)).isoformat()}"))
            for near, far in zip(edges, edges[1:])]


def load_store(today):
    try:
        with open(HEADLINE_STORE) as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {"created": today}
    for k, v in (("next_story", 1), ("items", {}), ("stories", {})):
        store.setdefault(k, v)
    return store


def save_store(store):
    os.makedirs(os.path.dirname(HEADLINE_STORE), exist_ok=True)
    tmp = HEADLINE_STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, HEADLINE_STORE)


def cap_round_robin(lists, limit):
    """Takes items newest-first from each list in turn, so no feed or date slice
    is starved."""
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
    `sections` is [(feed id, [(story id, story)], [new headline])].
    Returns ({id: (verdict, label, reason)} for new stories, {id: reference} for
    same-event lines, tokens used)."""
    check_model("classification")
    parts = []
    for fid, known_stories, new in sections:
        text = f"## {FEEDS[fid][1]}\nFlag when: {FEEDS[fid][3]}\n\nKnown stories:\n"
        text += "\n".join(f"{sid} [{'flagged' if s['verdict'] == 'alert' else 'not flagged'}] "
                          f"{s['label']}" for sid, s in known_stories) or "(none)"
        text += "\n\nNew headlines:\n"
        text += "\n".join(f"{h['id']}. {h['title']} ({h['date']})" for h in new)
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
    each headline is judged once. Returns ({feed id: {"stories": [...],
    "error": str or None}}, status note)."""
    today = date.today().isoformat()
    store = load_store(today)
    known, stories = store["items"], store["stories"]

    # One bucket per feed and date slice, so the cap trims evenly across both.
    buckets, errors, taken = [], {fid: [] for fid in FEEDS}, set()
    for fid, _, queries, _, _ in HEADLINE_FEEDS:
        for q in queries:
            try:
                slices = search(q)
            except Exception as e:
                errors[fid].append(str(e))
                continue
            for part in slices:
                bucket = []
                for h in sorted(part, key=lambda h: h["when"], reverse=True):
                    k = headline_key(h["title"])
                    if k not in taken:
                        taken.add(k)
                        bucket.append(dict(h, key=k, feed=fid))
                buckets.append(bucket)
    picked = [h for b in cap_round_robin(buckets, MAX_HEADLINES) for h in b]

    by_feed, pending = {}, []
    for fid in FEEDS:
        items = sorted((h for h in picked if h["feed"] == fid), key=lambda h: h["when"],
                       reverse=True)
        for n, h in enumerate(items, 1):
            h.update(id=f"{fid}{n}", date=h["when"].date().isoformat())
            rec = known.setdefault(h["key"], {"first_seen": today})
            rec["last_seen"] = today
            story = stories.get(rec.get("story"))
            if not (story and story["feed"] == fid and story["criteria"] == criteria_hash(fid)):
                rec.pop("story", None)
                pending.append(h)
        by_feed[fid] = items

    total = len(picked)
    status = f"{total} headlines."
    if pending:
        sections = []
        for fid, items in by_feed.items():
            new = [h for h in items if "story" not in known[h["key"]]]
            if new:
                sids = dict.fromkeys(known[h["key"]]["story"] for h in items
                                     if "story" in known[h["key"]])
                sections.append((fid, [(sid, stories[sid]) for sid in sids], new))
        try:
            new_stories, same, tokens = classify(sections)
            ids = {h["id"]: h for h in pending}
            assigned = {}
            for hid, (verdict, label, reason) in new_stories.items():
                if hid in ids:
                    sid = f"S{store['next_story']}"
                    store["next_story"] += 1
                    stories[sid] = {"feed": hid[0], "label": label, "reason": reason,
                                    "verdict": "alert" if verdict == "ALERT" else "ok",
                                    "criteria": criteria_hash(hid[0])}
                    assigned[hid] = sid
            for _ in range(5):  # a SAME line can point at another SAME line
                for hid, ref in same.items():
                    sid = ref if ref.startswith("S") else assigned.get(ref)
                    if (hid in ids and hid not in assigned and sid in stories
                            and stories[sid]["feed"] == hid[0]
                            and stories[sid]["criteria"] == criteria_hash(hid[0])):
                        assigned[hid] = sid
            for hid, sid in assigned.items():
                known[ids[hid]["key"]]["story"] = sid
            status += (f" Judged {len(assigned)} new ones in one call ({tokens:,} tokens).")
            if len(assigned) < len(pending):
                status += (f" {len(pending) - len(assigned)} were missing from the reply and "
                           "will be retried on the next run.")
        except Exception as e:
            status += f" {len(pending)} new ones couldn't be judged today: {e}"
    else:
        status += " None new to judge today."
    if total == MAX_HEADLINES:
        status += f" Capped at {MAX_HEADLINES}; the oldest in the busiest searches were dropped."

    feeds = {}
    for fid, items in by_feed.items():
        groups = {}
        for h in items:  # unjudged headlines stand alone
            groups.setdefault(known[h["key"]].get("story") or h["key"], []).append(h)
        out = []
        for gid, hs in groups.items():
            s = stories.get(gid)
            hs.sort(key=lambda h: h["when"])
            first_seen = min(known[h["key"]]["first_seen"] for h in hs)
            out.append({"headlines": hs, "verdict": s["verdict"] if s else None,
                        "reason": s["reason"] if s else "", "latest": hs[-1]["when"],
                        "new": first_seen == today and today != store["created"]})
        out.sort(key=lambda g: g["latest"], reverse=True)
        feeds[fid] = {"stories": out, "error": "; ".join(errors[fid]) or None}

    cutoff = (date.today() - timedelta(days=STORE_DAYS)).isoformat()
    store["items"] = {k: v for k, v in known.items() if v.get("last_seen", "") >= cutoff}
    used = {v.get("story") for v in store["items"].values()}
    store["stories"] = {sid: s for sid, s in stories.items() if sid in used}
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
    out = f'<p class="muted">Last {window_days(FEEDS[fid][2][0])} days: '
    out += f"{n_headlines} headlines grouped into {len(items)} stories.</p>"
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
    elif items and not unjudged:
        detail.append(f"Nothing flagged among {len(items)} stories.")
    if unjudged:
        detail.append(f"{len(unjudged)} headlines not judged yet (see the note at the top of "
                      "the Headlines section).")
    crossed = True if flagged else (None if unjudged else False)
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
    """Plain text of a LIMRA release page from its dateline on, plus the release date."""
    s = re.sub(r"(?s)<(script|style).*?</\1>", " ", raw.decode("utf-8", "replace"))
    t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", t)
    released = date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
    i = t.find("WINDSOR, Conn.")
    return (t[i:] if i >= 0 else t)[:15000], released


def parse_change(change, evidence):
    """Signed % change from the CHANGE field, or None unless it is stated unambiguously in the
    evidence sentence."""
    if not change or change.upper() == "NONE" or change not in evidence:
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
    """One model line -> {quarter, total, yoy, evidence} if it checks out against the text."""
    m = re.match(r"\s*(?:[-*•]\s*)?(?:QUARTER\s+)?(20\d\d)-Q([1-4])\s*\|\s*TOTAL\s+\$([\d.,]+)\s*billion\s*\|"
                 r"\s*CHANGE\s+(.*?)\s*\|\s*EVIDENCE\s+(.+?)\s*$", line)
    if not m:
        return None
    year, q, total_s, change, evidence = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4), m.group(5)
    evidence = evidence.strip('"“” ')
    total = float(total_s.replace(",", ""))
    ok = (evidence in text                                      # copied, not paraphrased
          and f"${total_s} billion" in evidence                 # the total is in that sentence
          and QUARTER_WORDS[q] in evidence.lower()              # and so is the quarter
          and 50 <= total <= 300                                # a market total, not a product line
          and released is not None and year in (released.year, released.year - 1))
    if not ok:
        return None
    return dict(quarter=f"{year}-Q{q}", total=total, yoy=parse_change(change, evidence), evidence=evidence)


def read_release(url):
    """Fetches one release and extracts its quarterly totals with DeepSeek."""
    text, released = release_text(fetch(url))
    # Early figures say "according to preliminary results"; later releases revise them.
    status = "preliminary" if re.search(r"\bpreliminary results\b", text, re.I) else "final"
    rec = dict(released=released.isoformat() if released else None, status=status, quarters=[],
               model=DEEPSEEK_MODEL_NAME)
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
    """({quarter: chosen figure}, [problems]) from every annuity sales release in the window. Per
    quarter, a final figure beats a preliminary one, then the later release wins."""
    try:
        with open(LIMRA_STORE) as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    problems = []
    first_year = date.today().year - LIMRA_YEARS
    xml = fetch(LIMRA_SITEMAP).decode("utf-8", "replace")
    for url in re.findall(r"<loc>([^<]+)</loc>", xml):
        low = url.lower()
        if "/newsroom/news-releases/" not in low or "annuit" not in low or "sales" not in low:
            continue
        y = re.search(r"/news-releases/(20\d\d)/", url)
        if url in store or (y and int(y.group(1)) < first_year):
            continue
        try:
            store[url] = read_release(url)
        except Exception as e:  # leave it out of the store so the next run tries again
            problems.append(f"{url}: {e}")
    os.makedirs(os.path.dirname(LIMRA_STORE), exist_ok=True)
    with open(LIMRA_STORE, "w") as f:
        json.dump(store, f, indent=1)
    best = {}
    for url, rec in store.items():
        for q in rec["quarters"]:
            cand = dict(q, url=url, status=rec["status"], released=rec["released"])
            key = (STATUS_RANK[rec["status"]], rec["released"] or "")
            cur = best.get(q["quarter"])
            if cur is None or key > (STATUS_RANK[cur["status"]], cur["released"] or ""):
                best[q["quarter"]] = cand
    for q, v in best.items():  # fill a missing change from the same quarter a year earlier
        prev = best.get(f"{int(q[:4]) - 1}{q[4:]}")
        if v["yoy"] is None and prev:
            v["yoy"], v["computed"] = (v["total"] / prev["total"] - 1) * 100, True
    return best, problems


def next_quarter(q):
    y, n = int(q[:4]), int(q[-1])
    return f"{y + n // 4}-Q{n % 4 + 1}"


def quarter_end(q):
    y, n = int(q[:4]), int(q[-1])
    return date(y + n // 4, n % 4 * 3 + 1, 1) - timedelta(days=1)


def block_annuities():
    rule = ("LIMRA's quarterly total U.S. annuity sales come in below the same quarter a year "
            "earlier, two quarters in a row. Annuity sales bring in much of the insurer money that "
            "Ares says buys the investment-grade AI debt (its first flow).")
    try:
        best, problems = limra_quarters()
    except Exception as e:
        best, problems = {}, [f"LIMRA releases unavailable today: {e}"]
    out = ""
    quarters = sorted(best)
    if quarters:
        rows = []
        for q in reversed(quarters[-6:]):
            v = best[q]
            yoy = ("not stated" if v["yoy"] is None else "0%" if round(v["yoy"]) == 0 else f"{v['yoy']:+.0f}%") + (" (computed)" if v.get("computed") else "")
            if v["yoy"] is not None and v["yoy"] < 0:
                yoy = f'<span class="bad">{yoy}</span>'
            rows.append([f"Q{q[-1]} {q[:4]}", f"${v['total']:,.1f}B", yoy, v["status"],
                         f'<a href="{esc(v["url"])}">{esc(v["released"])}</a>'])
        out += table(["Quarter", "Total U.S. annuity sales", "vs. same quarter a year earlier",
                      "Figures", "LIMRA release"], rows)
    crossed, detail = None, ""
    if len(quarters) >= 2:
        a, b = best[quarters[-2]], best[quarters[-1]]
        if next_quarter(quarters[-2]) != quarters[-1]:
            detail = f"{quarters[-2]} and {quarters[-1]} aren't consecutive quarters, so check by hand."
        elif a["yoy"] is None or b["yoy"] is None:
            detail = "A year-over-year change is missing for one of the last two quarters."
        elif (date.today() - quarter_end(quarters[-1])).days > 150:
            detail = f"The newest figures found are for {quarters[-1]}, which looks out of date."
        else:
            crossed = a["yoy"] < 0 and b["yoy"] < 0
            detail = (f"Last two quarters: {a['yoy']:+.0f}% and {b['yoy']:+.0f}% year over year"
                      + (" (latest figures preliminary)." if b["status"] == "preliminary" else "."))
    out += line(rule, crossed, detail)
    for p in problems:
        out += f'<p class="error">{esc(p)}</p>'
    news, _, news_detail = feed_block("F")
    out += f"<h4>Annuity sales news</h4>{news}"
    if news_detail:
        out += f'<p class="muted">{news_detail}</p>'
    return out + (
        '<p class="muted">Source: <a href="https://www.limra.com/en/newsroom/news-releases/">LIMRA '
        "news releases</a>, found through LIMRA's sitemap. "
        f"{esc(DEEPSEEK_MODEL_NAME)} copies each quarter's total and year-over-year change from the "
        "release text; a figure is used only if the sentence it quotes appears in the release word "
        "for word and contains that exact total and quarter. Final figures replace preliminary "
        "ones. If anything is missing or out of date, the line falls back to checking by hand.</p>")


def block_news():
    out = ('<p class="muted">Headlines reporting the same event are grouped into one story, '
           "listed under its first report. ALERT = "
           f"{esc(DEEPSEEK_MODEL_NAME)} judged that the story is a concrete event matching "
           "that section's line. NEW = first reported today. "
           f"{esc(headlines()[1])}</p>")
    for fid in "ABCDE":
        _, title, _, flag_when, why = FEEDS[fid]
        html_, crossed, detail = feed_block(fid)
        out += f"<h3>{esc(title)}</h3>" + html_
        out += line(f"a story reports {flag_when}. {why}", crossed, detail)
    return out + source("Google News search", "https://news.google.com/")


SECTIONS = [
    ("1. Capex and income", [
        ("Hyperscaler capex and free cash flow", block_hyperscalers),
        ("Nvidia revenue", block_nvidia),
        ("Contract backlog at Oracle, Microsoft and Alphabet", block_backlog),
        ("GPU rental prices", block_gpu_prices),
    ]),
    ("2. Credit", [
        ("Corporate bond spreads", block_spreads),
        ("Credit default swaps: market-implied default probability", block_cds),
        ("Weakest AI borrowers (stock prices)", block_weak_borrowers),
    ]),
    ("3. Insurance", [
        ("Annuity sales", block_annuities),
    ]),
    ("4. Headlines", [
        ("", block_news),
    ]),
]

NOT_HERE = (
    "No free source exists for: prices of the private placements and securitizations, the contract exit clauses inside individual deals, "
    "insurers' return on equity, and the off-balance-sheet commitments Ares cites: data-center "
    "leases signed but not yet started, and take-or-pay contracts for power and capacity. Those "
    "appear only in the text of filing footnotes, not in the tagged figures this dashboard reads "
    "(leases already started are tagged, but they sit on the balance sheet). The capex and backlog figures lag by one to "
    "three months. Ares's own advice is to cut risk before signals cross, not after: "
    "\"cycles are easy, but timing is hard.\""
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
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    if CACHED:
        try:
            with open(FETCHED_AT) as f:
                fetched = f.read().strip()
        except OSError:
            sys.exit("No saved responses yet: run once without --cached first.")
        generated = f"{generated} from data fetched {fetched}"
    else:
        os.makedirs(HTTP_CACHE, exist_ok=True)
        with open(FETCHED_AT, "w") as f:
            f.write(generated)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>AI Bubble Watch</title><style>{CSS}</style></head><body>",
        "<h1>AI Bubble Watch</h1>",
        f'<p class="muted">Generated {generated}. Raw signals from free public sources. '
        "Each one shows where to start worrying; there is no overall score. Based on Ares's "
        'Fall 2026 <a href="https://www.ares.com/content/dam/aresmgmt/Documents/In-the-Gaps/'
        'Ares_InTheGaps_Newsletter_Fall-2026.pdf">In the Gaps</a> newsletter.</p>',
    ]
    for heading, blocks in SECTIONS:
        parts.append(f"<h2>{esc(heading)}</h2>")
        for title, fn in blocks:
            if title:
                parts.append(f"<h3>{esc(title)}</h3>")
            try:
                parts.append(fn())
            except Exception as e:  # one broken source shouldn't sink the page
                parts.append(f'<p class="error">Could not fetch this signal today: {esc(e)}</p>')
    parts.append("<h2>Not on this dashboard</h2>")
    parts.append(f"<p>{esc(NOT_HERE)}</p></body></html>")

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    os.replace(tmp, OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
