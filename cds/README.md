# Default probabilities from CDS trades

Each ticker has its own folder (`crwv/`, `orcl/`) holding `trades.csv` (DTCC public CDS prints) and `spreads.png`.
Raw DTCC daily files are cached once in `dtcc_cache/` and shared across tickers.

```
python3 cds.py ORCL --days 90   # pull trades -> orcl/trades.csv (add tickers to NAMES in cds.py, or pass --name)
python3 pd.py ORCL              # hazard curve + default probabilities from the latest two days
python3 pd_history.py ORCL      # weekly 5y default probability, ASCII chart with 90% error bars
```

`cds_signal.py` is the CDS code behind the AI bubble dashboard (`../bubble_watch.py` imports it from here).
It downloads new daily files into `dtcc_cache/` (`dtcc_missing.json` lists weekdays with no file), keeps only fresh trades on
each company's own credit (index legs and lifecycle records are dropped), and works out who paid each up-front where the data
allow it: DTCC reports the net cash but not who paid. Where the data don't say, both readings are shown.
`published_levels.csv` lists 5-year levels reported in the press; the dashboard draws them next to the estimate.
To check its numbers against published levels: `python3 cds_check.py ORCL 2026-10-15 [days]`, and `python3 cds_check.py --tweet <status id>`
to date an X post (method, procedure, sources and results: `CDS_VALIDATION.md`). Tests: `python3 -m unittest test_cds_signal`.

`common.py` loads each ticker's `trades.csv` through `cds_signal.py`, so `pd.py` and `pd_history.py` price trades exactly as the dashboard does. That covers both standard coupons (1% investment grade, e.g. ORCL; 5% high yield, e.g. CRWV) and which direction each up-front payment went.
`crwv/trades_oct15-16.csv` is the original two-day CoreWeave file the result below was first computed from.

The sections below are the original CoreWeave write-up (Oct 15–16 2026). They predate the 2026-10-20 fixes: DTCC's payment
is net of accrued coupon (the accrued is added back, not subtracted), so these up-fronts and probabilities are somewhat low.

## Result

Market-implied ("risk-neutral") chance that CoreWeave defaults:

| Within | Probability |
|---|---|
| 1 year | ~8% |
| 2 years | ~22% |
| 3 years | ~36% |
| 4 years | ~50% |
| 5 years | ~59% |

| Contract ends | Up-front payment (% of amount insured) | Implied annual default rate |
|---|---|---|
| Dec 2027 (1.2y) | ~0% | ~8% |
| Jun 2029 (2.7y) | 6.3% | |
| Dec 2029 (3.2y) | 9.2% | |
| Dec 2030 (4.2y) | 13.7% | |
| Dec 2031 (5.2y) | 16.3% | ~16% averaged over 5y |

The default rate the market prices goes up the further out you look: it's fairly calm about the next year and much more worried about years 2 to 5.

## Caveats

- Assumes lenders get back 40% of what they're owed in a default. A lower assumed recovery would make the default probabilities lower.
- Prices built into insurance include a premium for bearing risk, so real-world default odds are lower than these numbers.
- The data is thin, so the year-by-year rates jump around. The cumulative figures are more reliable.
- Trades reported only as "5,000,000+" are left out, because their exact size isn't given.
- The 10/16 14:02 trades share one timestamp and may be a batch of paperwork trades rather than fresh market prices.
- The reported payments are net cash: the clean up-front minus 26 days of accrued coupon, which the seller hands back. (This write-up subtracted the accrued instead of adding it back; see the note above.)

## How it works

A CDS is insurance against a company defaulting on its debt. The buyer pays a fixed 5% a year, plus a lump sum up front when the market thinks 5% is too cheap, and in return gets paid the lost part of the debt (about 60%, assuming 40% is recovered) if CoreWeave defaults before the contract ends. Default is modeled as a random arrival with a yearly rate λ, so the chance of surviving to time t is e^(−∫λ). On that basis, a fair price makes the expected present value of the payout, (1−R) times the chance of defaulting in each small slice of time, equal to the expected present value of the premiums, which stop at default. With R at 40% and interest rates at 3.75%, the script solves for the constant λ that makes the shortest contract's up-front payment come out right. It then moves to each longer contract in turn, keeping the earlier λ values and solving only for the new stretch of time. This builds a piece-by-piece curve of yearly default rates. One minus the survival probability at each horizon gives the cumulative default odds. A quick check: the 5-year contract works out to about 950bp a year all-in (the 5% coupon plus the up-front payment spread over the contract's life), and 950bp ÷ 0.6 ≈ 16% a year, which compounds to about 55% over 5 years.
