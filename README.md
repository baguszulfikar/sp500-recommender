# S&P 500 Recommender

Automated script to fetch S&P 500 stock data, analyze fundamental valuations with **7 metrics**, and email **Top 10 undervalued stocks** every weekday — runs automatically via **GitHub Actions** at **8:30 PM WIB** (1 hour before NYSE opens).

## How It Works

1. Fetch the live S&P 500 constituent list from **Wikipedia** (always up-to-date)
2. Pull financial data from **Yahoo Finance / yfinance** (7 fundamental metrics)
3. Filter stocks that are potential **value traps** before scoring
4. Calculate **composite score** with different weights for financial vs non-financial stocks
5. Send an HTML email with **Top 10 stocks**, Top 3 analysis, and a metrics guide via **Gmail API (OAuth)**

---

## Scoring Methodology

### Metrics & Weights

Based on research into US equity fundamental analysis, this script uses **7 metrics** with different weights for **financial sector** (banks, insurance, etc.) vs **non-financial** stocks.

| Metric | Non-Financial | Financial | Rationale |
|--------|:---:|:---:|-----------|
| P/E Ratio | 20% | 20% | Low = undervalued relative to earnings |
| P/B Ratio | 15% | 25% | Low = below book value; higher weight for financials |
| ROE | 20% | 30% | High = efficient; primary metric for financial companies |
| D/E Ratio | 10% | — | Low = healthy debt; **not applicable for financials** |
| EV/EBITDA | 15% | — | Low = cheap on enterprise basis; more robust than P/E |
| FCF Yield | 10% | 10% | High = strong free cash flow relative to market cap |
| Revenue Growth | 10% | 15% | High = growing revenue; higher weight for financials |

> **Why different weights for financials?** Banks and insurance companies use debt as raw material (leverage by nature) — high D/E is normal, not a risk signal. EV/EBITDA is also not meaningful for financial businesses. ROE is the primary efficiency metric for financials.

### Value Trap Guard

Before scoring, stocks meeting any of these criteria are **removed** to avoid buying cheap-looking stocks with deteriorating fundamentals:

| Filter | Threshold | Reason |
|--------|-----------|--------|
| Minimum ROE | < 8% | Low ROE + cheap price = classic value trap |
| Current Ratio (non-financial) | < 0.8 | Short-term liquidity risk |
| Revenue Growth | < −15% YoY | Revenue collapse = fundamental deterioration signal |
| P/E outlier | > 300x | Data error or near-zero earnings |
| P/B outlier | > 50x | Data error |

### How the Score is Calculated

**Step 1 — Filter & Value Trap Guard** (see table above)

**Step 2 — Rank Score per Metric (0–100 scale)**

Each metric is converted to a 0–100 score based on **relative ranking** among stocks that passed the filter:

```
rank_score = (rank / total_stocks) × 100
```

| Metric | Rule |
|--------|------|
| P/E, P/B, D/E, EV/EBITDA | Lower = better → highest rank = score 100 |
| ROE, FCF Yield, Rev Growth | Higher = better → highest rank = score 100 |

> Ranking is used (not raw values) because P/E and EV/EBITDA operate on different scales. Ranking makes all metrics comparable on a 0–100 scale.

**Step 3 — Composite Score**

```
# Non-Financial
Score = (PE×20%) + (PB×15%) + (ROE×20%) + (DE×10%) + (EV_EBITDA×15%) + (FCF_Yield×10%) + (Rev_Growth×10%)

# Financial Sector
Score = (PE×20%) + (PB×25%) + (ROE×30%) + (FCF_Yield×10%) + (Rev_Growth×15%)
```

### US Market Sector Benchmarks

Based on [Damodaran NYU](https://pages.stern.nyu.edu/~adamodar/) and [S&P Global](https://www.spglobal.com/) data:

| Sector | Fair P/E | Fair P/B | Healthy ROE | Notes |
|--------|----------|----------|-------------|-------|
| Technology | 28–35x | 6–10x | >20% | High P/E is normal for growth; focus on FCF & ROE |
| Communication Services | 20–25x | 3–5x | >15% | |
| Consumer Discretionary | 22–28x | 4–6x | >18% | |
| Consumer Staples | 20–24x | 4–5x | >20% | Defensive, premium for stability |
| Energy | 10–14x | 1.5–2.5x | >12% | Use EV/EBITDA, not P/E, during commodity cycles |
| Financial Services | 12–16x | 1–2x | >12% | Use ROE & P/B as primary metrics |
| Health Care | 20–25x | 3–5x | >18% | |
| Industrials | 18–22x | 3–4x | >16% | |
| Basic Materials | 14–18x | 2–3x | >14% | EV/EBITDA more reliable |
| Real Estate (REITs) | 35–45x | 2–3x | >8% | High P/E is structurally normal for REITs |
| Utilities | 16–20x | 1.5–2x | >10% | Dividend yield matters more |

---

## Setup

### 1. Gmail OAuth (One-Time)

#### Option A — Reuse from saham-recommender (Recommended)

If you already set up Gmail OAuth for the IDX project, just copy the files:

```bash
copy ..\saham-recommender\credentials.json .
copy ..\saham-recommender\token.json .
```

#### Option B — Fresh Setup

**a. Create Google Cloud Project & Enable Gmail API**

1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Create new project → name it e.g. `StockRecommender`
3. **APIs & Services** → **Library** → search `Gmail API` → **Enable**

**b. Create OAuth Credentials**

1. **APIs & Services** → **Credentials** → **+ Create Credentials** → **OAuth client ID**
2. Configure **OAuth consent screen** if prompted:
   - User Type: **External**, App name: `Stock Recommender`
   - Under **Scopes**, add: `https://www.googleapis.com/auth/gmail.send`
   - Under **Test users**, add your Gmail address
3. Application type: **Desktop app** → **Create**
4. Download JSON → save as `credentials.json` in this folder

**c. Generate Token**

```bash
pip install -r requirements.txt
python setup_gmail.py
```

Browser opens → login with Gmail → `token.json` is saved automatically.

---

### 2. Set GitHub Actions Secrets

Go to this repo on GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret Name | Value |
|-------------|-------|
| `GMAIL_TOKEN_JSON` | Full contents of `token.json` |
| `GMAIL_CREDENTIALS_JSON` | Full contents of `credentials.json` |
| `SENDER_EMAIL` | Gmail address used for sending |
| `RECIPIENT_EMAIL` | Recipient email(s), comma-separated: `a@gmail.com,b@gmail.com` |

---

### 3. GitHub Actions Schedule

Workflow is already configured in `.github/workflows/daily-recommender.yml`.

Schedule: **every weekday (Mon–Fri) at 8:30 PM WIB** (13:30 UTC) — 1 hour before NYSE/NASDAQ opens at 9:30 AM ET.

To trigger manually: **Actions** tab → **Daily S&P 500 Recommender** → **Run workflow**.

---

## Run Locally

```bash
pip install -r requirements.txt

# If using existing token from saham-recommender:
copy ..\saham-recommender\token.json .

# Or generate a new token:
python setup_gmail.py

# Run
python main.py
```

`preview.html` is saved automatically after each run as an email preview.

---

## File Structure

```
sp500-recommender/
├── .github/
│   └── workflows/
│       └── daily-recommender.yml  # GitHub Actions schedule
├── main.py                        # Main script
├── setup_gmail.py                 # Gmail OAuth setup (run once locally)
├── requirements.txt               # Python dependencies
├── credentials.json               # (create yourself — do NOT commit)
├── token.json                     # (auto-generated — do NOT commit)
└── preview.html                   # (auto-generated on every run)
```

---

## Comparison with saham-recommender (IDX)

| | [saham-recommender](https://github.com/baguszulfikar/saham-recommender) | sp500-recommender |
|--|--|--|
| **Market** | Indonesia (IDX/BEI) | United States (NYSE/NASDAQ) |
| **Universe** | LQ45 — 45 stocks | S&P 500 — 500 stocks (live from Wikipedia) |
| **P/E Benchmark** | ~16x | ~22x |
| **Financial detection** | Hardcoded `BANK_SYMBOLS` | Auto-detected via `sector` field |
| **Price data** | IDX API + yfinance | yfinance only |
| **Schedule** | 08:30 WIB | **20:30 WIB** (1hr before NYSE opens) |
| **Run duration** | ~2 minutes | ~10–15 minutes (500 stocks) |
| **Email language** | Bahasa Indonesia | English |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `credentials.json not found` | Download from Google Cloud Console or copy from saham-recommender |
| `invalid_scope` error | Ensure `gmail.send` scope is added in OAuth consent screen, then re-generate token |
| `Token OAuth invalid` | Re-run `python setup_gmail.py`, update `GMAIL_TOKEN_JSON` secret |
| Few stocks pass filters | Normal — value trap guard removes low-ROE or revenue-declining stocks |
| GitHub Actions timeout | Unlikely (30 min limit set), but if it occurs reduce universe to Nasdaq 100 |
| Wikipedia fetch fails | Script falls back to a hardcoded list of 50 blue-chip S&P 500 stocks |

---

## Research References

Scoring methodology based on the following sources:

- [S&P 500 Historical P/E Ratio — Multpl.com](https://www.multpl.com/s-p-500-pe-ratio) — Historical S&P 500 P/E since 1871
- [Valuation by Sector (US) — Damodaran NYU](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/vebitda.html) — EV/EBITDA and multiples by sector
- [EV/EBITDA vs P/E — The Footnotes Analyst](https://www.footnotesanalyst.com/relative-valuation-conflicts-ev-ebitda-versus-p-e/) — Why EV/EBITDA is more robust than P/E
- [FCF Yield 40-Year Study — Quant Investing](https://www.quant-investing.com/blog/top-free-cash-flow-yield-stocks-for-2025) — Free cash flow yield as a top long-term return predictor
- [Value Trap Explained — Investopedia](https://www.investopedia.com/terms/v/valuetrap.asp) — How to identify and avoid value traps
- [P/E Ratio — Investopedia](https://www.investopedia.com/terms/p/price-earningsratio.asp)
- [P/B Ratio — Investopedia](https://www.investopedia.com/terms/p/price-to-bookratio.asp)
- [ROE — Investopedia](https://www.investopedia.com/terms/r/returnonequity.asp)
- [EV/EBITDA — Investopedia](https://www.investopedia.com/ask/answers/072715/what-considered-healthy-evebitda.asp)
- [Free Cash Flow — Investopedia](https://www.investopedia.com/terms/f/freecashflow.asp)

---

## Important Notes

- **S&P 500 constituents** change periodically (additions/removals). The script fetches the live list from Wikipedia on every run — no manual update needed.
- Financial data from Yahoo Finance may have a 1-day delay.
- **Financial sector stocks** (`Financial Services`, `Financials`) receive different scoring weights — ROE is weighted at 30% and D/E/EV/EBITDA are excluded.
- This script is for **research reference only** and does not constitute professional investment advice.
