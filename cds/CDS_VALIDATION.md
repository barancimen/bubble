# Checking the CDS default probabilities

How to check the CDS numbers on the AI bubble dashboard (computed by `cds_signal.py` in this
folder) against levels published by people with Bloomberg/ICE/Markit access, and what can make
them wrong. Last full check: 2026-10-18 (results below).

## Quick start

```
cd /root/bubble/cds
python3 cds_check.py ORCL 2026-10-15        # my estimate from trades in the 4 days to that date
python3 cds_check.py NVDA 2026-07-29 7      # ...or a wider window, for thinly traded names
python3 cds_check.py --tweet <status id>    # date an X post from the number in its URL
```

`cds_check.py` reads only the cached DTCC files (`dtcc_cache/`, refreshed by the dashboard's
daily cron run). It downloads nothing and doesn't change what the dashboard shows.

It prints:
- the median 5-year default probability and its equivalent spread
- the range of trade values
- the trade count
- how many trades had their payment direction assumed (see distortion 1)

**What the helper does not do:** it doesn't read posts or articles. `--tweet` only decodes the
number in a post's URL, which encodes when it was posted. That is useful because search results
often show X posts without a date. Reading the post and noting the company, the level and whether
it is the 5-year contract is done by hand (or by asking Claude), as is the comparison itself.

Published figures are almost always **5-year spreads in basis points**, not probabilities.
Compare spreads. Conversion used throughout (flat hazard, 40% recovery):

- spread ≈ hazard × (1 − 0.40), with hazard = −ln(1 − P₅) / 5
- e.g. 288bp → hazard 4.8%/yr → P₅ = 1 − e^(−0.24) = 21.3%

When a source quotes a "default probability" itself, check whether it uses the same recovery.
Most quote 40%, but some use simpler ratios, so compare the spread when one is given.

## Procedure

1. **Find published levels** for each name, with dates (sources below). Aim for 3+ per name,
   spread over the period, and note who the data vendor is.
2. **Date each one precisely.** For X posts, use `cds_check.py --tweet` on the status ID, or open
   the post. Posts are often relayed days later. For articles, use the trading day the level
   refers to, not the publication date.
3. **Run `cds_check.py`** for the days ending on that date. Use 4 days for liquid periods,
   7–14 days for NVDA or any month with few trades (n < 5 is noise).
4. **Record the gap** in the results table below, as a percentage of the published spread.
5. **Judge it**, using the rules below.

**Rules of thumb:**
- Within ±10% is a match.
- A consistent 5–10% shortfall is expected (distortions 3 and 4).
- A miss above 20% with plenty of trades (n ≥ 10) is a real problem. The usual cause is a
  wrong payment direction (distortion 1): check whether the alternative reading matches.
- A miss on n < 5 is not evidence of anything.

## Where to find cross-references

- **X/Twitter.** These accounts regularly post CDS levels, usually from Bloomberg screens or
  ICE data:
  - [@KobeissiLetter](https://x.com/KobeissiLetter)
  - [@GlobalMktObserv](https://x.com/GlobalMktObserv) (Global Markets Investor)
  - [@HedgieMarkets](https://x.com/HedgieMarkets)
  - [@TheShortBear](https://x.com/TheShortBear)

  Search X for `"<company> CDS" basis points` or `"<ticker>" "credit default swap"`.
- **News quoting vendor data:**
  - CNBC (S&P Global Market Intelligence / LSEG data)
  - Reuters (S&P/LSEG)
  - Bloomberg (its own CBGN/ICE data; often paywalled, so look for relays: Longbridge,
    36Kr, BigGo Finance, KuCoin News, Yahoo Finance)
  - Barron's
  - ROIC.ai and GuruFocus news snippets
- **Substack:** search `site:substack.com "<company>" CDS`. One useful example is
  markmancapitalinsight.substack.com (Nvidia). Substacks often post the Bloomberg chart itself,
  which gives a whole history rather than one point.
- **Rating-agency and bank research** (Morgan Stanley, S&P) quoted in the press, e.g. Morgan
  Stanley's Nvidia credit initiation (Aug 2026) quoted 84/89bp bid/offer.
- **Searches that worked:** `"<Company> 5-year CDS spread basis points Bloomberg"`,
  `"<Company> credit default swaps record basis points <year>"`.

Prefer sources that name a vendor and a date. Treat "record"/"highest ever" posts as snapshots
of a peak (distortion 3), and expect vendors to differ by 5–10% on the same day.

## Likely causes of distortion, most to least important

1. **Unknown payment direction (can be badly wrong).** DTCC reports the up-front payment's size
   but never its sign: zero negatives in 73,696 standard-coupon trades checked. The buyer pays
   when the spread is above the contract's fixed coupon (1% for investment grade, 5% for high
   yield); below it, the seller pays. Reading it the wrong way turns 40bp into about 160bp. The
   code takes the direction from the nearest quoted-spread trade, unless the up-front passed near
   zero in between (a possible crossing of the coupon). With no usable quote it assumes the buyer
   pays: hollow dots on the chart, `*` in the table, and the "assumed" count in `cds_check.py`.
   - **Most exposed:** names trading near their coupon, i.e. Nvidia and Oracle up to late 2025
     versus 100bp, and CoreWeave in Oct 2025 and Apr–Jun 2026 versus 500bp.
   - **CoreWeave since Nov 2025** depends entirely on the assumption (no usable quotes). External
     checks in Jul and Oct 2026 agree with it.
2. **Thin data.** Only standard-coupon trades with 4–6 years left count, and only if they have a
   quoted spread or an exact size. About 60% are reported as "$5,000,000+" and are unusable
   without a spread.
   - NVDA CDS only began trading in Nov 2025 and is sparse until Jun 2026.
   - A handful of trades can set a month's median. Check `n` before trusting a miss.
3. **Peak selection in the sources.** People post on record days, often intraday highs (e.g.
   CoreWeave's 1,194.5bp intraday versus about 1,040bp at the close the same day). My figures are
   medians of trades over several days, so they sit below headline numbers when spreads are
   rising fast.
4. **Trades versus quotes.** Mine are executed trade prices. Bloomberg/ICE levels are composite
   end-of-day or intraday quotes. On fast days quotes lead trades, and trades fill at bid or offer.
5. **Model simplifications:**
   - Flat hazard over each contract's life, 40% recovery, flat 3.75% discount rate, monthly grid.
   - Contracts of 4–6 years are treated as 5-year. Most trade out to Dec 2031 (about 5.2 years),
     which slightly overstates the spread when the curve slopes upward.
   - The reported up-front is assumed to include accrued coupon since the last IMM date (20th of
     Mar/Jun/Sep/Dec). This is inferred, not documented; an error shifts every point by up to
     about 0.25%/yr of coupon.
   - "Spread ≈ hazard × 0.6" is an approximation; vendors convert with the ISDA standard model.
     The difference is small (a few percent).
6. **Probabilities are risk-neutral.** They include a premium for bearing default risk, so real
   odds are lower. This doesn't affect the comparison (sources use the same convention) but
   matters when reading the numbers.
7. **What counts as default.** Contracts pay on bankruptcy or failure to pay by the parent entity,
   not restructuring and not defaults inside financing subsidiaries (e.g. CoreWeave's DDTL SPVs).
   Not a distortion, but explains gaps versus rating-agency default definitions.

## Known issues / to-do

- **Spreads in odd units (guarded rescale, done 2026-10-19).** Some trades report spreads 100×
  too small, e.g. `0.000223` (ORCL, Oct 2026). `cds_signal._rescaled_spread` multiplies values
  below 0.001 by 100 only when the result falls within the range of properly reported quotes
  within 10 days (±25%); otherwise the print is dropped. This adds ~17 ORCL, 3 CRWV and 2 NVDA
  trades. Of the three documented examples, only ORCL's is confirmed: `0.000042` (NVDA, Mar 2026)
  has no proper quote within 96 days, and `0.00045` (CRWV, 2025-10-24) none within 22 days, so
  both stay out. Oracle also has recurring `0.000048`/`0.00005`/`0.000056` prints that the guard
  rejects whenever Oracle trades far from 50bp, which suggests some of these are not simple unit
  errors. Tests: `python3 -m unittest test_cds_signal` from `cds/`.
- **Recovery sensitivity.** The dashboard now shows the latest month at 30% and 50% recovery next
  to the 40% estimate (e.g. CRWV Oct 2026: 48.8% / 55.1% / 63.1%).
- **Trade filter.** Rows are matched on `Underlying Asset Name` or `UPI Underlier Name` (many
  Oracle and Nvidia trades name the entity only in the latter) instead of anywhere in the row, and
  only USD trades are priced. The currency check sits in `resolve`, not `scan`, because some
  error-cancellation (`EROR`) rows have a blank currency and must still cancel their trades.

## Results of the 2026-10-18 check

Spreads in bp. "Mine" is the median of trades in the days ending on the date (window in brackets).

| Date | Name | Published | Source | Mine | Gap |
|---|---|---|---|---|---|
| ~2025-10-06 | CRWV | 368 | relayed report | 362 (Oct 1–10, quote-anchored) | ✓ |
| 2026-07-29 | CRWV | ~855 ("~50%") | Tech Times | 787, P₅ 48% (Jul 24–31) | −8% |
| 2026-10-15 | CRWV | 1,038 close, 1,194.5 intraday ("60%+") | CNBC; ICE via Longbridge/36Kr | 984, P₅ 56% (Oct 13–16) | −5 to −18% |
| 2025-11-02 | ORCL | 81 | @GlobalMktObserv | 75 (Oct 27–Nov 7) | ✓ |
| 2025-12-11 | ORCL | 139 | ROIC.ai | 108 (Dec 8–12, n=8) | **−22%** |
| early 2026 | ORCL | 144 | @GlobalMktObserv ("144 at start of year") | 144 (Jan 2–9) | ✓ |
| 2026-03-28 | ORCL | 191–198 | @KobeissiLetter, @GlobalMktObserv | 184 (Mar 23–31) | ✓ |
| 2026-07-28/08-08 | ORCL | 210–215 | @TheShortBear, @GlobalMktObserv, @KobeissiLetter | 195 (Jul 20–Aug 5) | −8% |
| 2026-09-26 | ORCL | 230 | @HedgieMarkets | 210 (Sep 22–30) | −9% |
| 2026-10-14/15 | ORCL | 263–288 | @GlobalMktObserv; Reuters via Securitas; CNBC 282 | 279 (Oct 13–16) | ✓ |
| 2026-06-01 | NVDA | ~38 | @KobeissiLetter | 26 (Jun 1–10, n=3) | **−30%** (thin) |
| late Jun 2026 | NVDA | ~42 | Crypto Briefing (ICE) | 44 (Jun 20–30, n=1) | ✓ |
| 2026-07-29 | NVDA | ~82 | @GlobalMktObserv; ICE | 75 (Jul 24–31) | −9% |
| Aug 2026 | NVDA | 80–89 | @GlobalMktObserv 79.8; Morgan Stanley 84/89 | 78 (Aug 18–31) | −7 to −12% |
| early Oct 2026 | NVDA | 80–87 | press summaries | 83 (Oct 1–16) | ✓ |

**Verdict:** 15 of 19 within about 10%, with a mild low bias explained by distortions 3–4. The two
>20% misses are on thin data, or (ORCL Dec 2025) during the crossing of Oracle's 1% coupon. The
published levels also confirm the direction logic: Oracle near 50bp in mid-2025 and Nvidia near
40bp in June 2026 rule out the buyer-pays reading, which gave about 150bp for both. CoreWeave at
855bp and 1,000bp+ confirms the buyer-pays assumption for its unanchored months.

**Source links from this check:**
- [CNBC, Oct 15 2026](https://www.cnbc.com/2026/10/15/coreweave-credit-default-swaps-hit-distress-levels-as-investors-sour-on-ai-debt.html)
- [Longbridge](https://longbridge.com/en/news/302048150)
- [36Kr](https://eu.36kr.com/en/p/4028016585986439)
- [Tech Times](https://www.techtimes.com/articles/322222/20260730/coreweave-cds-hits-50-default-odds-bond-market-calls-time-gpu-debt-spiral.htm)
- [Securitas](https://www.securitasglobal.com/risk-perspectives/oracle-cds-record-288-basis-points-ai-debt/)
- [ROIC.ai](https://www.roic.ai/news/oracles-5-year-cds-surges-to-139-bps-signaling-heightened-credit-risk-perception-amid-strong-fundamentals-12-11-2025)
- [Crypto Briefing](https://cryptobriefing.com/nvidia-cds-spreads-surpass-google/)
- [Markman Capital Insight](https://markmancapitalinsight.substack.com/p/nvidias-cds-recorded-its-largest)
- X posts, by status ID (date with `--tweet`):
  - 1985006488658604354
  - 2038023871690100765
  - 2038245292743463049
  - 2061587962224325092
  - 2082110078031634558
  - 2082173351192047817
  - 2082456663798227341
  - 2083962534005010761
  - 2086123017428512828
  - 2087168157031039028
  - 2103918659873779811
  - 2110363371631468871
