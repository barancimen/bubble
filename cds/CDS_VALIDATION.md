# Checking the CDS default probabilities

How to check the CDS numbers on the AI bubble dashboard (computed by `cds_signal.py` in this
folder) against levels published by people with Bloomberg/ICE/Markit access, and what can make
them wrong. Last full check: 2026-10-20 (results below).

The dashboard now does part of this itself: every 5-year level in `published_levels.csv`, plus
levels read from news headlines, is drawn on the chart next to the trade-based estimate, and the
text reports how far apart they are for the levels that were not used to build the method.

## Quick start

```
cd /root/bubble/cds
python3 cds_check.py ORCL 2026-10-15        # estimate from trades in the 4 days to that date
python3 cds_check.py NVDA 2026-07-29 7      # ...or a wider window, for thinly traded names
python3 cds_check.py --tweet <status id>    # date an X post from the number in its URL
python3 -m unittest test_cds_signal         # tests
```

`cds_check.py` reads only the cached DTCC files (`dtcc_cache/`, refreshed by the dashboard's
daily cron run). It downloads nothing and doesn't change what the dashboard shows. It prints the
median 5-year default probability and its 5-year spread, the range and number of trades, the two
readings for any trades where the data don't say who paid the up-front, and any level in
`published_levels.csv` within a day of the date.

Published figures are almost always **5-year spreads in basis points**, not probabilities, so
compare spreads. The code converts between them exactly (flat hazard, 40% recovery, 3.75% flat
rate): a 5-year spread corresponds to the constant hazard that prices a 5-year contract at zero
up-front, and P₅ = 1 − e^(−5 × hazard). Roughly, spread ≈ hazard × 0.6, so 288bp → P₅ ≈ 21%.
When a source quotes a "default probability" itself, check its recovery assumption.

## Adding a published level

Add a row to `published_levels.csv`: date (the trading day the level refers to, not the
publication date), ticker, spread in bp, source, URL, a note (e.g. "intraday", "as low as"), and
`role`: `design` if it was looked at while changing the method, `holdout` if it was found only
afterwards. Only holdout levels (and levels read from headlines) count towards the accuracy
figure on the dashboard. Prefer sources that name a vendor and a date; leave out intraday highs
unless noted, and figures you can't date.

## Where to find cross-references

- **X/Twitter.** These accounts regularly post CDS levels, usually from Bloomberg screens or
  ICE data: [@KobeissiLetter](https://x.com/KobeissiLetter),
  [@GlobalMktObserv](https://x.com/GlobalMktObserv), [@HedgieMarkets](https://x.com/HedgieMarkets),
  [@TheShortBear](https://x.com/TheShortBear). Use `cds_check.py --tweet` to date a post.
- **News quoting vendor data:** CNBC (S&P Global Market Intelligence / LSEG), Reuters,
  Bloomberg (often paywalled; look for relays such as Longbridge, 36Kr, BigGo Finance, KuCoin
  News, Yahoo Finance), Barron's, IFR, ROIC.ai.
- **Substack:** `site:substack.com "<company>" CDS`; substacks often post the Bloomberg chart.
- **Searches that worked:** `"<Company> 5-year CDS spread basis points Bloomberg"`,
  `"<Company> credit default swaps record basis points <year>"`.

Vendors differ by 5–10% on the same day, and posts cluster on record days.

## How the estimate is built, and what can make it wrong

1. **Which trades.** US-dollar standard-coupon (1% or 5%) trades on the company's own credit,
   matched by RED code (CRWV 239FAV, ORCL 6EC42L, NVDA 6D76CM) or reference-bond ISIN, with the
   name as a fallback; another entity's RED code (e.g. ORACLE SYSTEMS CORP, 6EC42I) never matches.
   Only fresh trades count (new trades with event TRAD, and corrections of them): novations,
   modifications and terminations carry fees on existing trades. Legs of credit-index trades are
   dropped: DTCC disseminates an index trade as one leg per company with the same payment, so a
   payment repeated on 20+ lines of a day's file marks a leg (about 100 such Oracle "trades" were
   being priced as Oracle's own credit, making most of its line from Nov 2024 to May 2025 the
   investment-grade index). Only contracts with 4.5–5.5 years left count (the 5-year point, not
   the 4-year one).
2. **Net cash, not up-front.** DTCC's payment is the net cash at settlement: the clean up-front
   minus the accrued coupon the seller hands back (accrual runs actual/360 from the last coupon
   date on or before trade date + 1; coupon dates are the 20th of Mar/Jun/Sep/Dec, moved to
   Monday at weekends). Confirmed on trades that report both a spread and a payment: this reading
   fits within about 2bp of notional, the opposite sign misses by about 33bp. The earlier code
   had the sign wrong, which biased every up-front-priced trade low (the "consistent 5–10%
   shortfall" the 2026-10-18 check put down to other causes).
3. **Who paid (can be badly wrong, and is shown when unsettled).** DTCC never reports the sign of
   the payment. The two readings can be far apart (CoreWeave since July: about 170bp versus
   1,040bp). The rule, fixed before the out-of-sample check below:
   - a reading that needs a negative default rate is impossible, which settles that trade;
   - a quoted spread settles who paid around it;
   - a coupon-date test: accrual restarts on each coupon date, so the right reading runs straight
     through it and the wrong one jumps by about twice the accrued coupon. It compares the last
     5 days before with the first 4 after, and decides only if one reading jumps at most 2/3 as
     much as the other. A quote within 30 days that disagrees overrules it;
   - evidence on both sides of a trade that agrees settles it; where it disagrees, the side flips
     on the day the net cash came closest to zero; evidence on one side only counts within 30 days.
   Trades still unsettled keep both readings; a month where most are unsettled is drawn as a band
   between the two (Oracle Nov 2024–May 2025, Nvidia Jun 2026 at the 2026-10-20 run).
   The coupon-date test is weakest when the market moves about as much as the accrued coupon over
   the same few days (CoreWeave's Sep 2026 test decided by 51bp versus 86bp).
4. **Bad prints.** A settled trade more than 3× above or below the median of at least three
   others within 5 days is dropped. Spreads reported 100× too small (e.g. `0.000223` for 223bp)
   are scaled up only when they match properly reported quotes within 10 days (±25%).
5. **Thin data.** About 60% of trades are reported as "$5,000,000+" without a spread and can't
   be used. Months with fewer than 3 trading days are drawn faded; NVDA CDS only began trading in
   Nov 2025. The 90% interval comes from resampling whole trading days, because prints on one day
   (and the pieces of a split block) are not independent; a split block counts once.
6. **Trades versus quotes, and peaks.** These are executed trade prices; vendor levels are
   composite quotes, often end-of-day or intraday highs on record days. Expect the estimate to lag
   on fast days.
7. **Model simplifications.** Flat hazard over each contract's life, 40% recovery (30% and 50%
   are shown), flat 3.75% discount rate, monthly grid. Given the right up-front, this is within
   about 0.35pp of the ISDA standard model from 40 to 1,500bp; moving the rate by 75bp shifts the
   result by about 0.4pp.
8. **Probabilities are risk-neutral** (they include a premium for bearing default risk, so real
   odds are likely lower), and **what counts as default** is narrow: bankruptcy or failure to pay
   by the company, including on debt it guarantees (e.g. CoreWeave guarantees its DDTL 3.0 loan);
   not a restructuring or out-of-court exchange, and not a subsidiary's unguaranteed debt.

CoreWeave's 2026 "quotes" at 105–130bp are total-return-swap financing spreads on its loans and
convertibles (blank coupon, loan identifiers); they are never priced.

## Results of the 2026-10-20 check

Gap = (dashboard − published) / published, using settled trades from 4 days before to 1 day after
the date (15 days either side for the month-only April level).

**Out of sample** (levels found after the rule in point 3 was fixed):

| Date | Name | Published | Dashboard | Gap |
|---|---|---|---|---|
| 2026-04 | ORCL | 198 (Spinel Capital, month only) | 181 | −8.5% |
| 2026-07-15 | NVDA | 57 (Crypto Briefing) | 55 | −4.0% |
| 2026-07-20 | ORCL | 203 (Bloomberg) | 199 | −2.2% |
| 2026-07-24 | ORCL | 203 (KuCoin) | 206 | +1.7% |
| 2026-07-27 | ORCL | 198 (goldesel.de) | 210 | +6.2% |
| 2026-07-31 | ORCL | 219 (goldesel.de) | 203 | −7.4% |
| 2026-10-08 | CRWV | ~818 (derived from CNBC) | 851 | +4.1% |
| 2026-10-16 | CRWV | ~1,104 (derived from CNBC) | 1,040 | −5.8% |
| 2026-08-11 | NVDA | 72.11 (Crypto Briefing) | no usable trades | |

Median gap about 5%, worst 8.5%. The first holdout set found a real bug: on 2026-08-11 the
dashboard showed 357bp for Nvidia, from a modification record carrying a 13% fee and a single new
trade paying 6.3% (five times its neighbours) whose only possible reading was forced. Fixed by
pricing only fresh trades and dropping isolated bad prints (point 4); after that, no usable trades
remain near that date. The second holdout set was checked only after that fix.

**In sample** (looked at while the method was being changed, so not a fair test): 20 levels from
Oct 2025 to Oct 2026, median gap 2.9%, worst 9.5% (NVDA 2026-10-08, one trade). Nvidia on
2026-06-01 (~38bp) falls in an unresolved month whose two readings are 38bp and 175bp.

**Sources:** see `published_levels.csv` for the URL of each level.
