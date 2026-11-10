# Kite Holdings Analyzer

This is a Python-based web application designed to analyze and display investment holdings using the Kite Connect API.

## 🚀 Features
- Fetches real-time holdings data.
- Provides tools for calculating metrics like Profit & Loss (PNL).
- Reconciles holdings against local Zerodha tradebook imports.
- Shows holding, tradebook, breakdown, and discrepancy quantities.
- Supports ISIN-based matching for Mutual Fund holdings.
- Provides separate controls to refresh holdings and import tradebook CSVs.
- Supports manual trade adjustments for quantities missing from the tradebook.
- Includes a portfolio history chart for historical P&L of currently held lots.
- Persistent session management using a local SQLite database.

## Portfolio history

The Portfolio history page is available from Holdings. It offers multi-select
Equity, MF, Debt, and Metal filters, plus separate 1M/3M/6M/1Y/3Y/5Y chart
periods. Rupee P&L and return on cost are plotted on separate axes.

The chart only includes lots that remain open in the current portfolio. Fully
sold positions and realized gains are excluded. Historical quantities and
purchase costs come from the locally imported Zerodha tradebook and manual
adjustments; make sure the tradebook has been refreshed. Ledger gaps and
unavailable prices are called out rather than filled with estimated values.

Equity history first uses IndianAPI's historical data endpoint, queried by the
holding's trading symbol (or ISIN when no symbol is present). The endpoint
accepts one symbol per request; the app requests five years once per holding
and reuses its local SQLite cache when shorter chart ranges are selected.
Configure `INDIAN_API_KEY` in `.env`. Debt and metal holdings prefer NSE history
via `jugaad-data`; it is also used as a fallback for other NSE holdings. The
instrument series is taken from the tradebook when available. NSE history
requests are chunked monthly by the library, so a long initial backfill can
take longer; fetched prices are cached locally and coverage expands if a
longer range is selected. Historical MF NAVs are obtained from MFAPI; where a
scheme code must be discovered from its fund name, the final scheme is
validated against the holding's ISIN. Price data is refreshed at most once per
day for the required coverage. If a provider has no prices for a holding, that
holding is omitted from historical points and reported in the chart's data
notes; the latest point uses the current holdings snapshot. NSE MCP is not used
as a direct data API: this Flask app has no configured MCP server connection.

Manual trade adjustments, including bonus, IPO, or demerger entries, participate
in FIFO reconstruction. When trade and adjustment costs do not match the
current holdings average cost, adjustment lots are reconciled to that average
so the latest chart P&L matches holdings. Zero-cost adjustments are flagged.
Corporate actions or incomplete trade records can still affect reconstructed
lots. The imported tradebook files do not provide CNC/MIS product labels;
same-day buys are processed before sells, matching the Holdings breakdown FIFO
behavior.

## Tradebook and discrepancy workflow

The application stores tradebook data locally in `session.db` in the `trade_imports`
table. The breakdown uses the holdings ISIN where available and falls back to the
holding/trade symbol for securities without an ISIN.

1. Export the Zerodha TradeBook CSV files on the local machine.
2. Place the CSV files in the sibling folder named `TradeBook`, next to this project:
   `Kite Holdings/TradeBook`.
3. Open the Holdings page and click the dedicated Tradebook refresh button.
4. The application imports the files only when that button is clicked; ordinary
   Holdings refreshes do not change `trade_imports`.
5. Use the breakdown menu on a holding to compare the holding quantity with BUY,
   SELL, CNC/MIS, and trade adjustment rows.
6. Use the discrepancy form for quantities that are not present in the tradebook.
   The adjustment date, notes, quantity, and ISIN are stored locally in SQLite.

Tradebook CSV files, database files, and local session data are sensitive
portfolio information and are not part of the Git repository. The application is
configured to ignore:

- `TradeBook/`
- sibling `../TradeBook/`
- `*.csv`
- `session.db`, `*.db`, and `*.sqlite3`

## ⚙️ Setup and Installation
Follow these steps to set up the local development environment:
1. **Clone the repository:**
   `git clone https://github.com/Smit0503/Kite-Holding-Analyzer.git`
2. **Create and activate a virtual environment:**
   `python -m venv .venv`
   `.\.venv\Scripts\Activate.ps1`
3. **Install dependencies:**
   `pip install -r requirements.txt`
4. **Configure API keys:**
   Copy the example file to create your local configuration:
   `copy .env.example .env`
   Edit the `.env` file and fill in your `KITE_API_KEY` and `KITE_API_SECRET`.
   Set `INDIAN_API_KEY` as well for IndianAPI stock-price history. Mutual-fund
   NAV history uses MFAPI and does not require an API key. NSE history is
   retrieved through `jugaad-data`.
5. **Start the app:**
   `python app.py`

## 🔑 Security Note
**Crucially, never commit your local `.env` file to GitHub, as it contains confidential API keys.** The `.gitignore` file is configured to prevent this.

## 📚 Built with
- Flask
- Python
- Kite Connect API