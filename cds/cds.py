#!/usr/bin/env python3
"""
Pull single-name CDS trade prints from DTCC's free public
security-based swap dissemination (SEC Regulation SBSR) and save them
to CSV, plus a spread chart if matplotlib is installed.

Usage:
    pip install requests pandas matplotlib
    python3 cds.py CRWV                   # last 30 days -> crwv/trades.csv
    python3 cds.py ORCL --days 90         # last 90 days -> orcl/trades.csv
    python3 cds.py XYZ --name "SOME CORP" # ticker not in NAMES below

If the download URL ever changes, grab the daily "SEC Credits" cumulative
zip files by hand from DTCC's Public Price Dissemination (PPD) dashboard,
put them in ./dtcc_cache/, and rerun: the script reads whatever zips are
cached there before trying to download.
"""
import argparse
import datetime as dt
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

URL = "https://pddata.dtcc.com/ppd/api/report/cumulative/sec/SEC_CUMULATIVE_CREDITS_{d}.zip"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "dtcc_cache"

# Ticker -> reference-entity text to search for in the DTCC files.
NAMES = {"CRWV": "COREWEAVE", "ORCL": "ORACLE"}


def fetch_day(day: dt.date) -> bytes | None:
    stamp = day.strftime("%Y_%m_%d")
    path = CACHE / f"SEC_CUMULATIVE_CREDITS_{stamp}.zip"
    if path.exists():
        return path.read_bytes()
    try:
        r = requests.get(URL.format(d=stamp), timeout=60,
                         headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as e:
        print(f"  {day}: request failed ({e})", file=sys.stderr)
        return None
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        return None  # weekend, holiday, or not published yet
    CACHE.mkdir(exist_ok=True)
    path.write_bytes(r.content)
    return r.content


def read_zip(blob: bytes) -> pd.DataFrame:
    frames = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for n in z.namelist():
            if n.lower().endswith(".csv"):
                frames.append(pd.read_csv(z.open(n), dtype=str, low_memory=False))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def match_rows(df: pd.DataFrame, needle: str) -> pd.DataFrame:
    # Column names have shifted across DTCC schema versions, so search every
    # text column for the reference name rather than relying on one header.
    mask = pd.Series(False, index=df.index)
    for c in df.columns:
        mask |= df[c].fillna("").str.upper().str.contains(needle, regex=False)
    return df[mask]


def pick(df: pd.DataFrame, *words: str) -> str | None:
    for c in df.columns:
        lc = c.lower()
        if all(w in lc for w in words):
            return c
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--name", help="reference-entity text (default: NAMES[ticker])")
    args = ap.parse_args()
    ticker = args.ticker.upper()
    if not args.name and ticker not in NAMES:
        ap.error(f"unknown ticker {ticker}; pass --name")
    needle = (args.name or NAMES[ticker]).upper()
    out_dir = HERE / ticker.lower()

    today = dt.date.today()
    hits = []
    for i in range(args.days, -1, -1):
        day = today - dt.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        blob = fetch_day(day)
        if blob is None:
            continue
        df = read_zip(blob)
        m = match_rows(df, needle)
        print(f"  {day}: {len(df):>6} credit trades, {len(m):>3} {needle}")
        if len(m):
            m = m.copy()
            m.insert(0, "file_date", day.isoformat())
            hits.append(m)

    if not hits:
        print(f"\nNo {needle} trades found. Either it didn't trade in the "
              "window, the files didn't download (check the URL note at the "
              "top), or the reference name is spelled differently.")
        return

    out = pd.concat(hits, ignore_index=True).drop_duplicates()
    out_dir.mkdir(exist_ok=True)
    out.to_csv(out_dir / "trades.csv", index=False)
    print(f"\nSaved {len(out)} trades to {out_dir / 'trades.csv'}")

    # Best-effort summary and chart: spread column, execution time, tenor.
    spread_col = pick(out, "spread") or pick(out, "price")
    time_col = pick(out, "execution", "timestamp") or pick(out, "execution")
    if spread_col and time_col:
        s = out[[time_col, spread_col]].copy()
        s[time_col] = pd.to_datetime(s[time_col], errors="coerce", utc=True)
        s[spread_col] = pd.to_numeric(s[spread_col], errors="coerce")
        s = s.dropna().sort_values(time_col)
        if len(s):
            print(s.tail(15).to_string(index=False))
            try:
                import matplotlib.pyplot as plt
                plt.figure(figsize=(10, 4))
                plt.plot(s[time_col], s[spread_col], "o-", ms=3)
                plt.title(f"{needle} CDS trade prints ({spread_col})")
                plt.tight_layout()
                plt.savefig(out_dir / "spreads.png", dpi=150)
                print(f"Chart saved to {out_dir / 'spreads.png'}")
            except ImportError:
                pass
    else:
        print("Couldn't identify spread/time columns automatically; "
              "open the CSV to inspect.")


if __name__ == "__main__":
    main()
