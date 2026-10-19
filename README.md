# Bubble Watch

This is a vibe-coded dashboard that tracks signs of distress that portend the popping of the present financial bubble. Most of these signs are from Ares's Fall 2026 [issue](https://www.ares.com/content/dam/aresmgmt/Documents/In-the-Gaps/Ares_InTheGaps_Newsletter_Fall-2026.pdf) of their newsletter:
- Capital spending and free cash flow at Microsoft, Alphabet, Amazon, Meta and Oracle (from SEC filings)
- Nvidia's quarterly revenue
- Contract backlog (i.e., signed but not yet delivered work) at Oracle, Microsoft and Alphabet
- Hourly rental prices for Nvidia H100, H200 and B200 GPUs
- Corporate bond spreads
- Stock prices of debt-heavy AI and data-center companies (CoreWeave, IREN, Hut 8, Cipher, TeraWulf, Applied Digital, Oracle)
- Quarterly U.S. annuity sales, a major source of the insurance money that Ares says is the main lender behind AI debt, gathered from LIMRA's news releases by deepseek-flash
- News about data-center lease cancellations, capex cuts, OpenAI and Anthropic, weak borrowers, credit-rating changes and annuity sales, filtered by deepseek-flash

In addition, the dashboard estimates default probabilities for CoreWeave, Oracle and Nvidia using public credit default swap trade data (code in `cds/`). These estimates are not very accurate. There are instructions for your agent in `cds/CDS_VALIDATION.md` to assess their accuracy. 

To create the dashboard:
1. Put your DeepSeek API key in `.deepseek_api_key` or the `DEEPSEEK_API_KEY` environment variable.
2. Run `python3 bubble_watch.py` to build `dashboard.html`.
3. Optionally, run `python3 serve_dashboard.py` to serve the html on port 8080.
