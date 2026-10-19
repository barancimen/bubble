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
"""
import csv
import functools
import hashlib
import html
import json
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

def fetch(url, ua=WEB_UA, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": ua} if ua else {})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except OSError:  # covers URLError, HTTPError and timeouts
            if attempt == tries - 1:
                raise
            time.sleep(5)


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


# ---------- signal blocks ----------

def block_hyperscalers():
    rows, fcf_negative, capex_down = [], [], []
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
        rows.append([
            ticker, esc(last),
            " · ".join(billions(v, True) for v in fcf),
            " · ".join(billions(capex[e]) for e in ends),
            pct(yoy, True),
        ])
    out = table(["Company", "Latest quarter end",
                 "Free cash flow, last 6 quarters ($B, oldest → newest)",
                 "Capex, last 6 quarters ($B)", "Latest capex vs same quarter a year earlier"], rows)
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
    out = table(["Quarter end"] + [esc(e) for e in ends],[["Revenue ($B)"] + [billions(rev[e]) for e in ends]])
    out += line("revenue falls from the previous quarter. Nvidia's sales are AI spending arriving; "
                "a sequential drop means buyers are slowing down.",
                change < 0, f"Latest quarter vs previous: {pct(change, True)}.")
    return out + source("SEC EDGAR XBRL filings (Nvidia)",
                        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810")


def block_backlog():
    rows, dropped = [], []
    for ticker, cik in BACKLOG:
        rpo = point_in_time(sec_concept(cik, "RevenueRemainingPerformanceObligation"))
        ends = list(rpo)[-6:]
        change = (rpo[ends[-1]] / rpo[ends[-2]] - 1) * 100
        if change <= -BACKLOG_DROP_PCT:
            dropped.append(ticker)
        rows.append([ticker, esc(ends[-1])," · ".join(billions(rpo[e]) for e in ends),
                     pct(change, True)])
    out = table(["Company", "Latest quarter end", "Contract backlog, last 6 quarters ($B)",
                 "Latest vs previous quarter"], rows)
    out += line(
        f"backlog falls more than {BACKLOG_DROP_PCT}% in a quarter at any of the three. "
        "Much of this backlog is OpenAI and Anthropic compute commitments (e.g. OpenAI's $300B "
        "with Oracle), so a drop means commitments are being cut or renegotiated. Normal dips "
        "over the last three years were under 4%.",
        bool(dropped), f"Dropped: {', '.join(dropped)}." if dropped else "")
    return out + source("SEC EDGAR XBRL filings (remaining performance obligations)",
                        "https://www.sec.gov/edgar/search/")


def block_gpu_prices():
    rows, crossed = [], []
    for slug, name in GPU_MODELS:
        weeks = gpu_weekly(slug)
        week, price, providers = weeks[-1]

        def change(n, i=len(weeks) - 1):
            return (weeks[i][1] / weeks[i - n][1] - 1) * 100 if i >= n else None

        c13 = change(13)
        if c13 is not None and c13 <= -GPU_DROP_13W_PCT:
            crossed.append(name)
        worst = min((change(13, i) for i in range(13, len(weeks))), default=None)
        rows.append([
            name, f"${price:.2f} (week of {esc(week)}, {providers} providers)",
            pct(change(4)),
            f'<span class="bad">{pct(c13)}</span>' if name in crossed else pct(c13),
            pct(worst), pct(change(len(weeks) - 1)) + f" (since {esc(weeks[0][0])})",
            " · ".join(f"{p:.2f}" for _, p, _ in weeks[-14:]),
        ])
    out = table(["GPU", "Latest weekly median, $/GPU-hour", "4 weeks", "13 weeks",
                 "Worst 13-week change in the data", "Whole series",
                 "Weekly medians, last 14 weeks ($, oldest → newest)"], rows)
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
    data = cds_signal.series(today)
    out = ('<p class="legend"><span class="key" style="background:#2a78d6"></span>5-year default '
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
            rows.append([f"{esc(name)} ({ticker})", "no trades", "n/a", "n/a", "n/a", "0"])
            continue
        i = idx[-1]
        ago = lambda k: (f"{series[i - k][1] * 100:.1f}%" if i >= k and series[i - k][1] is not None
                         else "n/a")
        m, med, lo, hi, n, assumed = series[i]
        rec_lo, rec_hi = (by_rec.get(r) for r in cds_signal.RECOVERY_RANGE)
        rec = (f"{rec_lo * 100:.1f}–{rec_hi * 100:.1f}%" if rec_lo is not None and rec_hi is not None
               else "n/a")
        rows.append([f"{esc(name)} ({ticker})",
                     f"{med * 100:.1f}% ({lo * 100:.1f}–{hi * 100:.1f}%), {m:%b %Y}" + " *" * assumed,
                     rec, ago(3), ago(12), str(n)])
    rec_lo, rec_hi = (f"{r:.0%}" for r in cds_signal.RECOVERY_RANGE)
    out += table(["Company", "5-year default probability, latest month (90% interval)",
                  f"Same month at {rec_lo}–{rec_hi} recovery", "3 months earlier",
                  "12 months earlier", "Trades in latest month"], rows)
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


def classify(sections):
    """One DeepSeek call that groups and judges every new headline.
    `sections` is [(feed id, [(story id, story)], [new headline])].
    Returns ({id: (verdict, label, reason)} for new stories, {id: reference} for
    same-event lines, tokens used)."""
    models = {m["id"]: m.get("name") for m in deepseek("/models")["data"]}
    if models.get(DEEPSEEK_MODEL) != DEEPSEEK_MODEL_NAME:
        raise RuntimeError(
            f"{DEEPSEEK_MODEL} is now {models.get(DEEPSEEK_MODEL) or 'not offered'}, not "
            f"{DEEPSEEK_MODEL_NAME}, so classification is paused. Change DEEPSEEK_MODEL_NAME in "
            "the script to accept the new model.")
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


def block_annuities():
    out, _, detail = feed_block("F")
    out += line(
        "LIMRA's quarterly total annuity sales come in below the same quarter a year earlier, "
        "two quarters in a row. Annuity sales bring in much of the insurer money that Ares says "
        "buys the investment-grade AI debt (its first flow). This dashboard does not have LIMRA's "
        "figures, so this test is manual: compare the last two quarterly totals in LIMRA's releases "
        "with a year earlier. The stories above are news only; a flagged one means a headline "
        "reported some decline, which may be a single quarter or one product line, not that the "
        "test is met.",
        None, detail)
    return out + source("LIMRA annuity research", "https://www.limra.com/en/research/annuities/")


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
    ("1. The trigger: is AI spending still being paid for?", [
        ("Hyperscaler capex and free cash flow", block_hyperscalers),
        ("Nvidia revenue", block_nvidia),
        ("Contract backlog at Oracle, Microsoft and Alphabet", block_backlog),
        ("GPU rental prices", block_gpu_prices),
    ]),
    ("2. Credit markets: are lenders getting nervous?", [
        ("Corporate bond spreads", block_spreads),
        ("Credit default swaps: market-implied default probability", block_cds),
        ("Weakest AI borrowers (stock prices)", block_weak_borrowers),
    ]),
    ("3. Insurance money: is the funding still coming in?", [
        ("Annuity sales news (manual check)", block_annuities),
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
.key{display:inline-block;width:14px;height:8px;vertical-align:middle;margin:0 5px 0 12px;border-radius:2px}
.legend .key:first-child{margin-left:0}
.error{color:#b00020}
"""


def main():
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
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
