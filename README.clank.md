# Notes on the signals taken from John Hempton's interview

`README.md` explains what the dashboard is and how to run it. This file covers the parts that were added in November 2026, which `README.md` does not mention.

## The third source

The dashboard now follows a third source next to the Ares newsletter and Groundbreaker. It is John Hempton's interview on the Joe Walker Podcast, titled "Why Data Centres Will Change the World and Bankrupt Their Builders". The link is https://www.youtube.com/watch?v=GY5YyPLfqp0 and it is stored as `HEMPTON_URL` in `bubble_watch.py`.

Hempton argues that AI compute has a high capital cost and a low marginal cost, that one provider's compute is much like another's, and that the technology keeps improving. He expects a business like that to lose money for the people who build it once supply catches up with demand, as happened with fiber optics around 2000. He thinks the companies that own the chips are the most exposed, and he is less sure about the companies that make the models.

## What was added to the page

The GPU rental section now also shows spot prices and one-year reserved prices for the same three GPUs. They come from the GetDeploying files that the dashboard was already downloading. There is a new warning line for a 20% fall in the spot price over 13 weeks. Weeks with fewer than five spot sellers are left out of that comparison.

A new section compares the revenue that each of the five large companies added over its last four quarters with its capital spending in those quarters. It uses Hempton's estimate that $7 trillion of capital spending needs about $2 trillion of revenue a year. The section also shows capital spending as a share of revenue and how fast depreciation is growing.

A new section shows Nvidia's stakes in private companies, its shares in listed companies and the guarantees it has given. The warning line is a quarter in which Nvidia reports a net loss on its investments.

A new section shows private investment in computers, communications equipment and software in the United States as a share of GDP since 1990, so that today's spending can be compared with the late 1990s. The warning line is a fall of 0.25 points of GDP from the highest level of the last two years.

A new section shows the share of businesses in the United States that say they used AI in the last two weeks. The warning line is that share no longer rising over about three months.

The share price section now includes Digital Realty and Equinix, which own data center buildings and do not own the chips inside them. A second warning line compares the six heavily indebted companies with these two.

The news section has a new feed called "News of too much computing power". It looks for idle capacity, price cuts, cancelled contracts for computing power, bankruptcies of GPU cloud companies and write-downs of AI chips. The limit on headlines per run was raised from 499 to 600 so that the new feed does not take room from the others.

The top of the page names the interview next to the other two sources. Below that is a list of every section, placed under the worst state of its warning lines, with a link to each section.

## A fix to the annuity figures

The annuity section used to find LIMRA's releases only through LIMRA's sitemap. In November 2026 that sitemap had not been updated for two months, so the third-quarter release was missed and the line said to check by hand. The dashboard now also reads LIMRA's newsroom page, which lists the newest releases.

## Where the new figures come from

| Figure | Source |
|---|---|
| Spot and reserved GPU prices | The GetDeploying price files, which also hold the on-demand prices |
| Revenue, depreciation and capital spending | SEC EDGAR, the same company data service as the other filed figures |
| Nvidia's stakes, guarantees and investment gains | SEC EDGAR |
| Investment as a share of GDP | FRED series `A679RC1Q027SBEA`, `B935RC1Q027SBEA` and `GDP` |
| Businesses using AI | The national spreadsheet of the Census Bureau's Business Trends and Outlook Survey |
| Digital Realty and Equinix share prices | Yahoo Finance |
| News of too much computing power | Google News, judged by the same DeepSeek model as the other feeds |

None of these needs an API key. The Census spreadsheet is an `.xlsx` file, and `xlsx_rows` in `bubble_watch.py` reads it with the standard library.

## Changing the thresholds

The thresholds are constants near the top of `bubble_watch.py`. They are `GPU_SPOT_DROP_13W_PCT`, `GPU_SPOT_MIN_SELLERS`, `REVENUE_PER_CAPEX`, `BUILDOUT_DROP_PTS` and `ADOPTION_BACK`. The page text is built from them, so changing a constant also changes the wording on the page.

## What is still missing

Anthropic filed an S-1 with the SEC in October 2026. The financial statements in that filing are not tagged for machines, so the dashboard does not read them. Anthropic's CIK number is 2130357. Once it files quarterly reports, its revenue and spending can be read with `sec_concept`, the function that reads the other companies.

The dashboard also has no figures for the prices in private compute contracts, for how much installed capacity is idle, or for what the model makers charge per token.

## Checking a change

Run `python3 -m unittest test_bubble_watch` for the tests. Run `python3 bubble_watch.py --cached` to rebuild the page from the responses saved by the last full run. It writes `dashboard-preview.html` and makes no network or DeepSeek calls. A source that was added since the last full run has no saved response yet, so its section shows an error in the preview until the next full run.

## Wording on the page

All text on the page is written in plain English. Each warning line is two sentences, one that says when to start worrying and one that says whether there is a warning today. Tests look for the phrases "There is a warning today", "There is no warning today" and "This has to be checked by hand today", so a change to that wording means changing the tests as well. The rules that the news model is given are kept separate from the rules shown on the page. Changing a rule the model is given makes it judge every stored story again, while changing the text on the page does not.
