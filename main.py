"""
S&P 500 Recommender - Top 10 Undervalued Stocks
Kirim rekomendasi via Gmail API setiap hari kerja pukul 20:30 WIB
(1 jam sebelum NYSE/NASDAQ buka jam 21:30 WIB).

Setup:
  1. pip install -r requirements.txt
  2. Copy token.json & credentials.json dari project saham-recommender
  3. python main.py
"""

import os
import base64
import json
import logging
import math
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests
import yfinance as yf
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------

RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "your_email@gmail.com")
SENDER_EMAIL    = os.environ.get("SENDER_EMAIL", "your_email@gmail.com")
RECIPIENT_LIST  = [e.strip() for e in RECIPIENT_EMAIL.split(",") if e.strip()]

TOKEN_FILE       = os.path.join(os.path.dirname(__file__), "token.json")
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")

# ---------------------------------------------------------------------------
# Bobot scoring — disesuaikan untuk pasar AS
#
# Perbedaan utama vs IDX:
#   - Financials (bank, insurance, dll) = skip D/E & EV/EBITDA seperti bank IDX
#   - Tech & Growth: P/E lebih tinggi adalah normal, bobot ROE & FCF lebih besar
#   - Benchmark pasar: S&P 500 P/E historis ~22x (vs IDX ~16x)
#
# Referensi:
#   - S&P 500 Historical P/E: https://www.multpl.com/s-p-500-pe-ratio
#   - US Sector Valuations: https://pages.stern.nyu.edu/~adamodar/
#   - FCF Yield study: https://www.quant-investing.com/blog/top-free-cash-flow-yield-stocks-for-2025
# ---------------------------------------------------------------------------

WEIGHTS_NONFINANCIAL = {
    "pe_score":             0.20,
    "pb_score":             0.15,
    "roe_score":            0.20,
    "de_score":             0.10,
    "ev_ebitda_score":      0.15,
    "fcf_yield_score":      0.10,
    "revenue_growth_score": 0.10,
}

WEIGHTS_FINANCIAL = {
    "pe_score":             0.20,
    "pb_score":             0.25,  # P/B sangat relevan untuk financial
    "roe_score":            0.30,  # ROE adalah metrik utama perusahaan finansial
    "de_score":             0.00,  # tidak relevan untuk financial/bank
    "ev_ebitda_score":      0.00,  # tidak relevan untuk financial
    "fcf_yield_score":      0.10,
    "revenue_growth_score": 0.15,
}

# Sektor yang diperlakukan seperti "bank" — skip D/E & EV/EBITDA
FINANCIAL_SECTORS = {"Financial Services", "Financials"}

# ---------------------------------------------------------------------------
# Benchmark valuasi per sektor untuk pasar AS
# Sumber: Damodaran NYU, Multpl.com, S&P Global
# ---------------------------------------------------------------------------

SECTOR_BENCH_US = {
    "Technology":               {"pe": 30, "pb": 8.0, "roe": 22},
    "Communication Services":   {"pe": 22, "pb": 4.0, "roe": 15},
    "Consumer Discretionary":   {"pe": 25, "pb": 5.0, "roe": 18},
    "Consumer Staples":         {"pe": 22, "pb": 4.5, "roe": 20},
    "Energy":                   {"pe": 12, "pb": 2.0, "roe": 12},
    "Financial Services":       {"pe": 14, "pb": 1.5, "roe": 12},
    "Financials":               {"pe": 14, "pb": 1.5, "roe": 12},
    "Healthcare":               {"pe": 22, "pb": 4.0, "roe": 18},
    "Health Care":              {"pe": 22, "pb": 4.0, "roe": 18},
    "Industrials":              {"pe": 20, "pb": 3.5, "roe": 16},
    "Basic Materials":          {"pe": 16, "pb": 2.5, "roe": 14},
    "Real Estate":              {"pe": 40, "pb": 2.5, "roe":  8},  # REIT: P/E tinggi wajar
    "Utilities":                {"pe": 18, "pb": 1.8, "roe": 10},
}
MARKET_BENCH_US = {"pe": 22, "pb": 3.5, "roe": 15}  # default jika sektor tidak ditemukan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Ambil daftar S&P 500 dari Wikipedia (diperbarui otomatis)
# ---------------------------------------------------------------------------

def fetch_sp500_symbols() -> list:
    """
    Ambil daftar ticker S&P 500 dari Wikipedia.
    Wikipedia menjaga list ini tetap up-to-date mengikuti perubahan konstituen.
    """
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        log.info("Mengambil daftar S&P 500 dari Wikipedia...")
        tables = pd.read_html(url)
        sp500_df = tables[0]
        symbols = sp500_df["Symbol"].str.replace(".", "-", regex=False).tolist()
        log.info(f"Berhasil: {len(symbols)} ticker S&P 500 ditemukan")
        return symbols
    except Exception as e:
        log.error(f"Gagal ambil S&P 500 dari Wikipedia: {e}")
        log.warning("Menggunakan daftar fallback 50 saham blue chip AS")
        return SP500_FALLBACK


# Fallback jika Wikipedia tidak bisa diakses
SP500_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "JPM",
    "XOM", "TSLA", "UNH", "V", "PG", "MA", "JNJ", "COST", "HD", "MRK",
    "ABBV", "CVX", "WMT", "BAC", "NFLX", "KO", "CRM", "AMD", "PEP", "TMO",
    "ACN", "MCD", "CSCO", "ABT", "GE", "DHR", "TXN", "ADBE", "WFC", "PM",
    "LIN", "CAT", "IBM", "MS", "GS", "AMGN", "INTU", "ISRG", "SPGI", "BKNG",
]

# ---------------------------------------------------------------------------
# 2. Ambil data finansial dari yfinance (batch per 50 untuk menghindari rate limit)
# ---------------------------------------------------------------------------

def fetch_financial_data(symbols: list) -> pd.DataFrame:
    """
    Ambil data fundamental S&P 500 dari yfinance.
    Diproses dalam batch untuk menghindari rate limiting.
    """
    records = []
    total = len(symbols)

    for i, symbol in enumerate(symbols, 1):
        if i % 50 == 0:
            log.info(f"Progress: {i}/{total} saham diproses...")
            time.sleep(2)  # jeda singkat per 50 saham

        try:
            ticker = yf.Ticker(symbol)
            info   = ticker.info

            close_price  = info.get("currentPrice") or info.get("regularMarketPrice") or 0
            volume       = info.get("regularMarketVolume", 0)
            change_pct   = info.get("regularMarketChangePercent", 0)
            pe_ratio     = info.get("trailingPE") or info.get("forwardPE")
            pb_ratio     = info.get("priceToBook")
            roe          = info.get("returnOnEquity")
            debt_to_eq   = info.get("debtToEquity")
            market_cap   = info.get("marketCap", 0)
            sector       = info.get("sector", "")
            company_name = info.get("longName") or info.get("shortName", symbol)
            ev_ebitda    = info.get("enterpriseToEbitda")
            free_cf      = info.get("freeCashflow")
            rev_growth   = info.get("revenueGrowth")
            current_r    = info.get("currentRatio")

            dividend_yield = info.get("dividendYield") or 0
            if dividend_yield > 1:
                dividend_yield /= 100

            roe_pct       = roe * 100 if roe is not None else None
            de_ratio      = debt_to_eq / 100 if debt_to_eq is not None else None
            rev_growth_pct = rev_growth * 100 if rev_growth is not None else None

            if free_cf and market_cap and market_cap > 0:
                fcf_yield_pct = (free_cf / market_cap) * 100
            else:
                fcf_yield_pct = None

            is_financial = sector in FINANCIAL_SECTORS

            records.append({
                "symbol":           symbol,
                "company":          company_name,
                "sector":           sector,
                "is_financial":     is_financial,
                "price":            close_price,
                "volume":           volume,
                "change_pct":       change_pct,
                "pe_ratio":         pe_ratio,
                "pb_ratio":         pb_ratio,
                "roe_pct":          roe_pct,
                "de_ratio":         de_ratio,
                "ev_ebitda":        ev_ebitda,
                "fcf_yield_pct":    fcf_yield_pct,
                "rev_growth_pct":   rev_growth_pct,
                "current_ratio":    current_r,
                "market_cap":       market_cap,
                "dividend_yield_pct": round(dividend_yield * 100, 2),
            })
        except Exception as e:
            log.warning(f"Gagal: {symbol} — {e}")

    return pd.DataFrame(records)

# ---------------------------------------------------------------------------
# 3. Scoring & ranking (disesuaikan untuk pasar AS)
# ---------------------------------------------------------------------------

def score_stocks(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # --- Filter outlier & data tidak valid ---
    # P/E: lebih longgar untuk pasar AS (tech bisa P/E 60-80x dan masih wajar)
    df = df[
        df["pe_ratio"].notna() & (df["pe_ratio"] > 0) & (df["pe_ratio"] < 300) &
        df["pb_ratio"].notna() & (df["pb_ratio"] > 0) & (df["pb_ratio"] < 50)  &
        df["roe_pct"].notna()  & (df["price"] > 0)
    ].copy()

    # --- Value Trap Guard ---
    # 1. ROE minimum 8%
    df = df[df["roe_pct"] >= 8].copy()

    # 2. Current ratio >= 0.8 untuk non-financial
    cr_ok = df["current_ratio"].notna()
    df = df[
        df["is_financial"] | ~cr_ok |
        (cr_ok & ~df["is_financial"] & (df["current_ratio"] >= 0.8))
    ].copy()

    # 3. Revenue tidak anjlok lebih dari 15%
    rg_ok = df["rev_growth_pct"].notna()
    df = df[~rg_ok | (df["rev_growth_pct"] >= -15)].copy()

    if df.empty:
        log.error("Tidak ada saham yang lolos filter!")
        return df

    log.info(f"Saham lolos filter: {len(df)}")

    def rank_score(series, lower_is_better=True):
        valid  = series.notna()
        scores = pd.Series(50.0, index=series.index)
        if valid.sum() > 1:
            n = valid.sum()
            ranked = series[valid].rank(ascending=not lower_is_better, method="min")
            scores[valid] = (ranked / n * 100).clip(0, 100)
        return scores

    df["pe_score"]             = rank_score(df["pe_ratio"],       lower_is_better=True)
    df["pb_score"]             = rank_score(df["pb_ratio"],       lower_is_better=True)
    df["roe_score"]            = rank_score(df["roe_pct"],        lower_is_better=False)
    df["de_score"]             = rank_score(df["de_ratio"],       lower_is_better=True)
    df["ev_ebitda_score"]      = rank_score(df["ev_ebitda"],      lower_is_better=True)
    df["fcf_yield_score"]      = rank_score(df["fcf_yield_pct"],  lower_is_better=False)
    df["revenue_growth_score"] = rank_score(df["rev_growth_pct"], lower_is_better=False)

    def apply_weights(row):
        w = WEIGHTS_FINANCIAL if row["is_financial"] else WEIGHTS_NONFINANCIAL
        return sum(row[metric] * weight for metric, weight in w.items())

    df["composite_score"] = df.apply(apply_weights, axis=1)
    df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    return df

# ---------------------------------------------------------------------------
# 4. Analisis Top 3
# ---------------------------------------------------------------------------

def generate_top3_analysis(top10: pd.DataFrame) -> str:
    medal = ["🥇", "🥈", "🥉"]
    cards = ""

    for i, (_, row) in enumerate(top10.head(3).iterrows()):
        symbol       = row["symbol"]
        company      = str(row["company"])[:45]
        sector       = str(row.get("sector", "")) or "N/A"
        is_financial = bool(row["is_financial"])

        bench = SECTOR_BENCH_US.get(sector, MARKET_BENCH_US)
        reasons = []

        pe = row.get("pe_ratio")
        if pe and not math.isnan(pe):
            disc = ((bench["pe"] - pe) / bench["pe"]) * 100
            if disc > 15:
                reasons.append(
                    f"<li><strong>P/E {pe:.1f}x</strong> — {disc:.0f}% below sector average "
                    f"({bench['pe']:.0f}x). Priced cheaply relative to earnings.</li>"
                )

        pb = row.get("pb_ratio")
        if pb and not math.isnan(pb):
            disc = ((bench["pb"] - pb) / bench["pb"]) * 100
            if disc > 15:
                reasons.append(
                    f"<li><strong>P/B {pb:.2f}x</strong> — {disc:.0f}% below sector benchmark "
                    f"({bench['pb']:.1f}x). Book value not fully reflected in market price.</li>"
                )

        roe = row.get("roe_pct")
        if roe and not math.isnan(roe):
            if roe >= bench["roe"] * 1.2:
                reasons.append(
                    f"<li><strong>ROE {roe:.1f}%</strong> — {roe/bench['roe']:.1f}x above sector "
                    f"avg ({bench['roe']:.0f}%). Management highly efficient at generating returns.</li>"
                )
            elif roe >= bench["roe"]:
                reasons.append(
                    f"<li><strong>ROE {roe:.1f}%</strong> — above sector average ({bench['roe']:.0f}%), "
                    f"solid profitability.</li>"
                )

        ev = row.get("ev_ebitda")
        if not is_financial and ev and not math.isnan(ev) and 0 < ev < 12:
            reasons.append(
                f"<li><strong>EV/EBITDA {ev:.1f}x</strong> — below 12x indicates attractive "
                f"valuation on an enterprise basis, independent of capital structure.</li>"
            )

        fcf = row.get("fcf_yield_pct")
        if fcf and not math.isnan(fcf) and fcf > 3:
            reasons.append(
                f"<li><strong>FCF Yield {fcf:.1f}%</strong> — strong free cash flow generation "
                f"({fcf:.1f}% of market cap). Earnings backed by real cash, not just accounting.</li>"
            )

        rg = row.get("rev_growth_pct")
        if rg and not math.isnan(rg):
            if rg >= 10:
                reasons.append(
                    f"<li><strong>Revenue Growth {rg:.1f}%</strong> — high growth combined with "
                    f"cheap valuation: a classic undervalued growth opportunity.</li>"
                )
            elif rg >= 3:
                reasons.append(
                    f"<li><strong>Revenue Growth {rg:.1f}%</strong> — steady revenue growth "
                    f"confirms this is not a value trap caused by a shrinking business.</li>"
                )

        de = row.get("de_ratio")
        if not is_financial and de is not None and not math.isnan(de) and de < 0.5:
            reasons.append(
                f"<li><strong>D/E {de:.2f}x</strong> — very low debt, providing financial "
                f"flexibility for growth or resilience in downturns.</li>"
            )

        if not reasons:
            reasons.append(
                "<li>Strong composite score across P/E, P/B, ROE, and cash flow metrics "
                "relative to all other S&P 500 constituents.</li>"
            )

        border_color = "#FFD700" if i == 0 else "#C0C0C0" if i == 1 else "#CD7F32"
        cards += f"""
        <div style="border:1px solid #e0e0e0; border-radius:10px; padding:18px 20px;
                    margin-bottom:14px; background:#fff; border-left:5px solid {border_color};">
          <div style="display:flex; align-items:center; margin-bottom:10px;">
            <span style="font-size:22px; margin-right:10px;">{medal[i]}</span>
            <div>
              <strong style="font-size:16px; color:#1a1a2e;">#{i+1} {symbol}</strong>
              <span style="font-size:12px; color:#888; margin-left:8px;">Score: {row['composite_score']:.1f}</span><br>
              <span style="font-size:12px; color:#555;">{company}</span>
              <span style="font-size:11px; color:#888; margin-left:6px;">| {sector}</span>
            </div>
          </div>
          <p style="margin:0 0 8px; font-size:12px; color:#555; font-style:italic;">
            Why is this stock undervalued?
          </p>
          <ul style="margin:0; padding-left:18px; font-size:13px; color:#333; line-height:1.8;">
            {''.join(reasons)}
          </ul>
        </div>"""

    return cards

# ---------------------------------------------------------------------------
# 5. Format email HTML
# ---------------------------------------------------------------------------

def fmt(val, decimals=2, suffix=""):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "-"
    return f"{val:.{decimals}f}{suffix}"

def format_price(val):
    if not val:
        return "-"
    return f"${val:,.2f}"

def format_market_cap(val):
    if not val or val == 0:
        return "-"
    if val >= 1e12:
        return f"${val/1e12:.1f}T"
    if val >= 1e9:
        return f"${val/1e9:.1f}B"
    return f"${val/1e6:.0f}M"


def build_email_html(top10: pd.DataFrame, fetch_date: str) -> str:
    top3_cards = generate_top3_analysis(top10)

    rank_colors = [
        "#FFD700", "#C0C0C0", "#CD7F32",
        "#4CAF50", "#4CAF50", "#4CAF50",
        "#2196F3", "#2196F3", "#2196F3", "#2196F3",
    ]

    rows = ""
    for _, row in top10.iterrows():
        rank         = int(row["rank"])
        badge_color  = rank_colors[rank - 1] if rank <= len(rank_colors) else "#9E9E9E"
        change_color = "#4CAF50" if row["change_pct"] >= 0 else "#F44336"
        change_sign  = "+" if row["change_pct"] >= 0 else ""
        fin_badge    = ' <span style="font-size:10px;background:#E8F5E9;color:#2E7D32;padding:1px 5px;border-radius:3px;">FIN</span>' if row["is_financial"] else ""
        fcf_val      = row.get("fcf_yield_pct")
        fcf_color    = "#4CAF50" if (fcf_val and not math.isnan(fcf_val) and fcf_val > 0) else "#F44336"
        rg_val       = row.get("rev_growth_pct")
        rg_color     = "#4CAF50" if (rg_val and not math.isnan(rg_val) and rg_val >= 0) else "#F44336"

        rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0;">
          <td style="padding:10px 6px; text-align:center;">
            <span style="background:{badge_color}; color:{'#333' if rank <= 3 else '#fff'};
                         padding:3px 8px; border-radius:20px; font-weight:bold; font-size:13px;">
              #{rank}
            </span>
          </td>
          <td style="padding:10px 6px;">
            <strong style="font-size:14px; color:#1a1a2e;">{row['symbol']}</strong>{fin_badge}<br>
            <span style="font-size:11px; color:#666;">{str(row['company'])[:38]}</span>
          </td>
          <td style="padding:10px 6px; font-size:11px; color:#555;">{str(row['sector'])[:22] or '-'}</td>
          <td style="padding:10px 6px; text-align:right;">
            <strong>{format_price(row['price'])}</strong><br>
            <span style="color:{change_color}; font-size:11px;">{change_sign}{fmt(row['change_pct'], 2)}%</span>
          </td>
          <td style="padding:10px 6px; text-align:right; color:#333;">{fmt(row['pe_ratio'], 1)}x</td>
          <td style="padding:10px 6px; text-align:right; color:#333;">{fmt(row['pb_ratio'], 2)}x</td>
          <td style="padding:10px 6px; text-align:right; color:#4CAF50;">{fmt(row['roe_pct'], 1)}%</td>
          <td style="padding:10px 6px; text-align:right; color:#555;">{fmt(row['ev_ebitda'], 1)}x</td>
          <td style="padding:10px 6px; text-align:right; color:{fcf_color};">{fmt(fcf_val, 1)}%</td>
          <td style="padding:10px 6px; text-align:right; color:{rg_color};">{fmt(rg_val, 1)}%</td>
          <td style="padding:10px 6px; text-align:right; font-size:11px; color:#888;">{format_market_cap(row['market_cap'])}</td>
          <td style="padding:10px 6px; text-align:right;">
            <strong style="color:#1a1a2e;">{fmt(row['composite_score'], 1)}</strong>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#f5f7fa; margin:0; padding:20px; }}
    .container {{ max-width:1050px; margin:0 auto; background:#fff; border-radius:12px;
                  box-shadow:0 4px 20px rgba(0,0,0,0.08); overflow:hidden; }}
    .header {{ background:linear-gradient(135deg, #0a192f 0%, #112240 50%, #1d3557 100%);
               padding:28px 36px; color:#fff; }}
    .header h1 {{ margin:0; font-size:22px; letter-spacing:0.5px; }}
    .header p {{ margin:8px 0 0; opacity:0.75; font-size:13px; }}
    .content {{ padding:24px 16px; }}
    .methodology {{ background:#f0f4ff; border-left:4px solid #1d3557;
                    padding:12px 16px; margin-bottom:14px; border-radius:0 8px 8px 0;
                    font-size:12px; color:#444; line-height:1.6; }}
    .filters {{ background:#fff3e0; border-left:4px solid #FF9800;
                padding:10px 16px; margin-bottom:20px; border-radius:0 8px 8px 0;
                font-size:12px; color:#555; }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    thead tr {{ background:#0a192f; color:#fff; }}
    thead th {{ padding:10px 6px; text-align:center; font-weight:600; font-size:11px;
                letter-spacing:0.3px; white-space:nowrap; }}
    tbody tr:hover {{ background:#f0f4ff; }}
    .disclaimer {{ margin-top:18px; padding:12px; background:#fff8e1; border-radius:8px;
                   font-size:11px; color:#795548; }}
    .footer {{ text-align:center; padding:18px; font-size:11px; color:#999;
               border-top:1px solid #f0f0f0; }}
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📈 Top 10 S&amp;P 500 Undervalued Stocks</h1>
    <p>Daily recommendation with 7 fundamental metrics &bull; {fetch_date} &bull; 1 hour before NYSE opens</p>
  </div>
  <div class="content">
    <div class="methodology">
      <strong>Scoring Methodology (US Market Adjusted):</strong><br>
      <strong>Non-Financial:</strong> P/E (20%) + P/B (15%) + ROE (20%) + D/E (10%) + EV/EBITDA (15%) + FCF Yield (10%) + Revenue Growth (10%)<br>
      <strong>Financial Sector:</strong> P/E (20%) + P/B (25%) + ROE (30%) + FCF Yield (10%) + Revenue Growth (15%)
      &mdash; D/E &amp; EV/EBITDA excluded for financials.
    </div>
    <div class="filters">
      <strong>Value Trap Guard:</strong>
      ROE &lt; 8% removed &bull; Current Ratio &lt; 0.8 (non-financial) removed &bull;
      Revenue Growth &lt; &minus;15% removed &bull; P/E &gt; 300x or P/B &gt; 50x removed as outliers.
    </div>
    <table>
      <thead>
        <tr>
          <th>Rank</th>
          <th style="text-align:left;">Stock</th>
          <th style="text-align:left;">Sector</th>
          <th>Price</th>
          <th>P/E</th>
          <th>P/B</th>
          <th>ROE</th>
          <th>EV/EBITDA</th>
          <th>FCF Yield</th>
          <th>Rev Growth</th>
          <th>Mkt Cap</th>
          <th>Score</th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>

    <div style="margin-top:28px;">
      <h2 style="font-size:16px; color:#0a192f; margin-bottom:14px; padding-bottom:8px;
                  border-bottom:2px solid #f0f0f0;">
        🔍 Top 3 Analysis — Why Are These Stocks Undervalued?
      </h2>
      {top3_cards}
    </div>

    <div style="margin-top:28px;">
      <h2 style="font-size:15px; color:#0a192f; margin-bottom:12px; padding-bottom:8px;
                  border-bottom:2px solid #f0f0f0;">
        📚 Metrics Guide
      </h2>
      <table style="width:100%; border-collapse:collapse; font-size:12px;">
        <thead>
          <tr style="background:#f5f7fa;">
            <th style="padding:8px 10px; text-align:left; color:#555; font-weight:600; width:14%;">Metric</th>
            <th style="padding:8px 10px; text-align:left; color:#555; font-weight:600; width:30%;">Description</th>
            <th style="padding:8px 10px; text-align:left; color:#555; font-weight:600; width:20%;">Ideal (S&amp;P 500)</th>
            <th style="padding:8px 10px; text-align:left; color:#555; font-weight:600; width:36%;">Learn More</th>
          </tr>
        </thead>
        <tbody>
          <tr style="border-bottom:1px solid #f0f0f0;">
            <td style="padding:8px 10px;"><strong>P/E Ratio</strong></td>
            <td style="padding:8px 10px; color:#555;">Price divided by earnings per share. Measures how much the market pays for each dollar of profit.</td>
            <td style="padding:8px 10px; color:#2196F3;">&lt; 22x (S&amp;P 500 avg ~22x)</td>
            <td style="padding:8px 10px;">
              <a href="https://www.investopedia.com/terms/p/price-earningsratio.asp" style="color:#1d3557;">Investopedia: P/E Ratio ↗</a><br>
              <a href="https://www.multpl.com/s-p-500-pe-ratio" style="color:#1d3557;">S&amp;P 500 Historical P/E ↗</a>
            </td>
          </tr>
          <tr style="border-bottom:1px solid #f0f0f0; background:#fafafa;">
            <td style="padding:8px 10px;"><strong>P/B Ratio</strong></td>
            <td style="padding:8px 10px; color:#555;">Price divided by book value per share. Below 1x means trading below net asset value.</td>
            <td style="padding:8px 10px; color:#2196F3;">&lt; 3.5x (S&amp;P 500 avg)</td>
            <td style="padding:8px 10px;">
              <a href="https://www.investopedia.com/terms/p/price-to-bookratio.asp" style="color:#1d3557;">Investopedia: P/B Ratio ↗</a>
            </td>
          </tr>
          <tr style="border-bottom:1px solid #f0f0f0;">
            <td style="padding:8px 10px;"><strong>ROE</strong></td>
            <td style="padding:8px 10px; color:#555;">Return on Equity — net income divided by shareholder equity. Measures management efficiency in generating profit.</td>
            <td style="padding:8px 10px; color:#4CAF50;">&gt; 15% (excellent &gt; 20%)</td>
            <td style="padding:8px 10px;">
              <a href="https://www.investopedia.com/terms/r/returnonequity.asp" style="color:#1d3557;">Investopedia: ROE ↗</a>
            </td>
          </tr>
          <tr style="border-bottom:1px solid #f0f0f0; background:#fafafa;">
            <td style="padding:8px 10px;"><strong>D/E Ratio</strong></td>
            <td style="padding:8px 10px; color:#555;">Debt-to-Equity — measures reliance on debt financing. Not applicable for financial sector stocks.</td>
            <td style="padding:8px 10px; color:#2196F3;">&lt; 1.0x</td>
            <td style="padding:8px 10px;">
              <a href="https://www.investopedia.com/terms/d/debtequityratio.asp" style="color:#1d3557;">Investopedia: D/E Ratio ↗</a>
            </td>
          </tr>
          <tr style="border-bottom:1px solid #f0f0f0;">
            <td style="padding:8px 10px;"><strong>EV/EBITDA</strong></td>
            <td style="padding:8px 10px; color:#555;">Enterprise Value / EBITDA. More robust than P/E as it's neutral to capital structure, tax, and depreciation.</td>
            <td style="padding:8px 10px; color:#2196F3;">&lt; 12x (cheap &lt; 8x)</td>
            <td style="padding:8px 10px;">
              <a href="https://www.investopedia.com/ask/answers/072715/what-considered-healthy-evebitda.asp" style="color:#1d3557;">Investopedia: EV/EBITDA ↗</a><br>
              <a href="https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/vebitda.html" style="color:#1d3557;">Damodaran: EV/EBITDA by Sector ↗</a>
            </td>
          </tr>
          <tr style="border-bottom:1px solid #f0f0f0; background:#fafafa;">
            <td style="padding:8px 10px;"><strong>FCF Yield</strong></td>
            <td style="padding:8px 10px; color:#555;">Free Cash Flow / Market Cap. Measures real cash generated relative to price. One of the best long-term return predictors.</td>
            <td style="padding:8px 10px; color:#4CAF50;">&gt; 4% (excellent &gt; 7%)</td>
            <td style="padding:8px 10px;">
              <a href="https://www.investopedia.com/terms/f/freecashflow.asp" style="color:#1d3557;">Investopedia: Free Cash Flow ↗</a><br>
              <a href="https://www.quant-investing.com/blog/top-free-cash-flow-yield-stocks-for-2025" style="color:#1d3557;">40-year FCF Yield study ↗</a>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 10px;"><strong>Revenue Growth</strong></td>
            <td style="padding:8px 10px; color:#555;">Year-over-year revenue growth. Ensures a cheap stock is not cheap because its business is shrinking (value trap).</td>
            <td style="padding:8px 10px; color:#4CAF50;">&gt; 5% YoY</td>
            <td style="padding:8px 10px;">
              <a href="https://www.investopedia.com/terms/r/revenue.asp" style="color:#1d3557;">Investopedia: Revenue ↗</a><br>
              <a href="https://www.investopedia.com/terms/v/valuetrap.asp" style="color:#1d3557;">Investopedia: Value Trap ↗</a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="disclaimer">
      ⚠️ <strong>Disclaimer:</strong> This report is generated automatically from public financial data
      and does not constitute professional investment advice. Always conduct your own research before
      making investment decisions. Data sourced from Yahoo Finance (yfinance).
    </div>
  </div>
  <div class="footer">
    S&amp;P 500 Recommender &bull; {fetch_date} &bull; Data: Yahoo Finance (yfinance)
  </div>
</div>
</body>
</html>"""
    return html

# ---------------------------------------------------------------------------
# 6. Gmail
# ---------------------------------------------------------------------------

def get_gmail_service():
    token_json_str = os.environ.get("GMAIL_TOKEN_JSON")
    if token_json_str:
        creds = Credentials.from_authorized_user_info(json.loads(token_json_str))
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    else:
        raise FileNotFoundError(
            "Credentials not found.\n"
            "Local: copy token.json from saham-recommender project.\n"
            "GitHub Actions: set GMAIL_TOKEN_JSON secret."
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            if not token_json_str and os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
        else:
            raise RuntimeError("OAuth token invalid or expired. Re-run setup or update secret.")

    return build("gmail", "v1", credentials=creds)


def send_email(subject: str, html_body: str) -> bool:
    try:
        service = get_gmail_service()
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = ", ".join(RECIPIENT_LIST)
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info(f"Email sent to: {', '.join(RECIPIENT_LIST)}")
        return True
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        return False

# ---------------------------------------------------------------------------
# 7. Main
# ---------------------------------------------------------------------------

def main():
    today = datetime.now().strftime("%A, %d %B %Y")
    log.info(f"=== S&P 500 Recommender — {today} ===")

    symbols = fetch_sp500_symbols()
    log.info(f"Fetching data for {len(symbols)} S&P 500 stocks (this may take ~10 min)...")

    df = fetch_financial_data(symbols)
    if df.empty:
        log.error("No data fetched.")
        return

    log.info(f"Data fetched: {len(df)} stocks")

    scored = score_stocks(df)
    if scored.empty:
        log.error("Scoring failed — no stocks passed filters.")
        return

    top10 = scored.head(10)
    log.info(
        f"Top 10 stocks:\n"
        f"{top10[['rank','symbol','pe_ratio','pb_ratio','roe_pct','ev_ebitda','fcf_yield_pct','rev_growth_pct','composite_score']].to_string(index=False)}"
    )

    fetch_date = datetime.now().strftime("%d %B %Y, %H:%M WIB")
    html = build_email_html(top10, fetch_date)

    preview_path = os.path.join(os.path.dirname(__file__), "preview.html")
    with open(preview_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Preview saved: {preview_path}")

    subject = f"📈 Top 10 S&P 500 Undervalued Stocks — {datetime.now().strftime('%d %b %Y')}"
    success = send_email(subject, html)

    if success:
        log.info("Done! Recommendation sent.")
    else:
        log.error("Email failed. Check logs above.")


if __name__ == "__main__":
    main()
