# AI Bubble Watch

This repo builds a single web page that tracks signs that the AI investment boom is turning into a bust. It is live at gestaltschaft.com/bubble. `README.md` belongs to the owner and should only be changed when they ask. This file is for agents working on the code.

## What the page shows

Each signal gets a section with a chart or table and one or more warning lines. A warning line says when to worry and whether there's a warning today, no warning, or something that needs checking by hand. There is no overall score. A list at the top of the page sorts every section by the most serious state of its lines.

The signals come from three sources. The first is Ares's Fall 2026 newsletter "In the Gaps", which is about the debt financing AI data centers. The second is Groundbreaker. The third is John Hempton's interview "Why Data Centres Will Change the World and Bankrupt Their Builders", whose link is stored as `HEMPTON_URL`. Hempton argues that computing power is expensive to build, cheap to run and sold in competition, so its builders lose money once supply catches up with demand.

| Section | Data | Function |
|---|---|---|
| Corporate bond spreads | FRED | `block_spreads` |
| Default risk from credit default swaps | DTCC swap trades, via `cds/cds_signal.py` | `block_cds` |
| Share prices of heavily indebted AI companies, with QQQ and two data center landlords | Yahoo Finance | `block_weak_borrowers` |
| Big tech's capital spending and free cash flow | SEC EDGAR | `block_hyperscalers` |
| Revenue against capital spending | SEC EDGAR | `block_payback` |
| Nvidia's revenue and receivables | SEC EDGAR | `block_nvidia` |
| Nvidia's stakes in and guarantees for other companies | SEC EDGAR | `block_nvidia_financing` |
| Contract backlog | SEC EDGAR | `block_backlog` |
| Commitments not on the balance sheet | SEC EDGAR | `block_commitments` |
| GPU rental prices, on demand, spot and reserved | GetDeploying CSV files | `block_gpu_prices` |
| Investment in computers and software as a share of GDP | FRED | `block_buildout` |
| Businesses using AI | Census Bureau BTOS spreadsheet | `block_adoption` |
| Annuity sales | LIMRA press releases, read by DeepSeek | `block_annuities` |
| News, in six feeds | Google News RSS, judged by DeepSeek | `block_news` |

None of these needs an API key except DeepSeek, which reads its key from `DEEPSEEK_API_KEY` or `.deepseek_api_key`.

## Files

| Path | What it is |
|---|---|
| `bubble_watch.py` | The whole page builder. Standard library only. |
| `test_bubble_watch.py` | Unit tests for it. |
| `cds/cds_signal.py` | Prices DTCC credit default swap trades into default probabilities and draws the CDS charts. |
| `cds/test_cds_signal.py` | Tests for the CDS code. |
| `cds/CDS_VALIDATION.md` | How the CDS estimate is built and checked against published levels. |
| `cds/published_levels.csv` | CDS levels reported in the press, used to check the estimate. |
| `cds/cds_check.py`, `cds/pd.py`, `cds/pd_history.py`, `cds/common.py` | Command-line tools for checking CDS numbers. |
| `cds/cds.py` | An older standalone DTCC downloader that needs pandas and requests. The page doesn't use it. |
| `serve_dashboard.py` | A small server for the page on port 8080. Its systemd service, `bubble-dashboard`, is installed but disabled. |
| `site/` | The Cloudflare Worker that serves the page. `publish.sh` copies `dashboard.html` into it and deploys with wrangler. |

These are generated and not in git: `dashboard.html`, `dashboard-preview.html`, `data/`, `cds/dtcc_cache/`, `site/public/` and `site/node_modules/`.

## How it runs

Cron runs `bubble_watch.py` at 03:00 and 15:00 UTC and then `site/publish.sh`, with output appended to `data/run.log`. A full run takes about three and a half minutes. `site/publish.sh` reads a Cloudflare token from `~/.cloudflare-token`. The Worker shows a red banner if the page is more than 30 hours old.

To rebuild and publish by hand, run `SEC_USER_AGENT="bubble-watch admin@example.com" python3 bubble_watch.py` and then `./site/publish.sh`.

## How `bubble_watch.py` is put together

Every download goes through `fetch`. It saves each response in `data/http_cache` so that `--cached` runs can replay it. It also keeps the last response that passed its checks in `data/last_good`. When a download fails or returns something other than data, such as a maintenance page, `fetch` uses the saved copy and says so on the page.

`main` builds the page from `SECTIONS`, a list of headings and block functions. Each block returns HTML. If a block raises an exception, its section shows the error and the rest of the page is still built. `per_item` lets a block go on when one company or series fails, and it marks the line as needing a check by hand.

`line(rule, crossed, detail)` draws a warning line. `crossed` is True for a warning, False for none and None for a check by hand. `rule` completes the sentence "Worry if ...". Each call is also recorded in `STATUS`, which `glance` uses for the list at the top of the page. `no_line` is for signals without a fixed line.

Thresholds are constants near the top of the file, and the page text is built from them.

Some data is kept between runs in `data/`. `headlines.json` stores every headline and the model's verdict on it, so each headline is judged once. `limra.json` stores the figures read from each LIMRA release. `oracle_prepayments.json` stores a figure that Oracle tags only in its own XBRL extension. `cds_news.json` stores CDS levels read from headlines.

## The CDS estimate

DTCC doesn't report who paid a trade's up-front payment, and the two readings can be several times apart. `cds_signal.py` settles this from quoted spreads and coupon dates where it can. A month it can't settle takes the reading closer, on a log scale, to the nearest settled months, and the chart shows it as a hollow dot. `cds/CDS_VALIDATION.md` explains the whole method.

## The language model

DeepSeek is used in three places, always as `deepseek-flash`. `check_model` stops each use if that name no longer points at `DEEPSEEK_MODEL_NAME`. Its answers are checked against the source text before they are used.

For the news feeds, each entry in `HEADLINE_FEEDS` has an id, a title, the Google News searches, the rule the model is given and the rule shown on the page. Changing the model's rule or `CLASSIFY_INSTRUCTIONS` changes `criteria_hash`, which makes the model judge every stored story in that feed again. Changing the title or the rule shown on the page costs nothing. `MAX_HEADLINES` caps the headlines per run at 600, shared fairly between feeds.

## Checking a change

Run `python3 -m unittest test_bubble_watch`, which takes about a second. The CDS tests, `cd cds && python3 -m unittest test_cds_signal`, take about 30 seconds.

Run `python3 bubble_watch.py --cached` to rebuild the page from the responses saved by the last full run. It writes `dashboard-preview.html`, makes no network or model calls and takes under a minute. A source added since the last full run has no saved response, so its section shows an error until the next full run.

The tests look for the phrases "There's a warning today", "There's no warning today" and "This needs checking by hand today". If you change that wording, change the tests too.

The machine is small, with one CPU and less than 300 MB of free memory, so keep test runs small and put a time limit on them.

## Writing for the page

The owner wants every word on the page in plain, natural English and as short as it can be. That means full sentences, contractions where they sound natural, and no bold labels, colons before a punchline, asides in brackets or dashes, strings of semicolons or coined names.

## Known gaps

The page doesn't cover private credit prices, the exit clauses in individual deals, or commitments that only appear in the text of filings. It also lacks what OpenAI and Anthropic earn and spend on computing power. Anthropic filed an S-1 in October 2026 under CIK 2130357, but its financial statements aren't tagged for machines yet. Once it files quarterly reports, `sec_concept` can read them as it does for the other companies.

The cron line still uses a placeholder contact for `SEC_USER_AGENT`.
