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
The selected-period return uses Modified Dietz and only includes holdings with
usable historical prices. Its money-weighted XIRR is shown in brackets, using
the portfolio value at the period start, purchases of currently held lots
during the period, and the portfolio value at the period end.
Click a chart date to see its holdings, applicable price dates, quantities,
invested value, current value, and P&L in a sortable breakdown table.

The chart only includes lots that remain open in the current portfolio. Fully
sold positions and realized gains are excluded. Historical quantities and
purchase costs come from the locally imported Zerodha tradebook and manual
adjustments; make sure the tradebook has been refreshed. Ledger gaps and
unavailable prices are called out rather than filled with estimated values.

NSE-listed equity, debt, and metal prices are retrieved with `jugaad-data`,
using the holding's trading symbol and the tradebook's NSE series when
available. Mutual-fund NAV history comes from MFAPI; when a scheme code must be
discovered from the fund name, the selected scheme is validated against the
holding's ISIN. These sources require no additional API keys. NSE does not
provide a batch-symbol request through `jugaad-data`, so the app fetches missing
instrument histories concurrently with a small worker limit and caches each
instrument's daily closes in SQLite. On a cold cache, it initially fetches up
to five years so every chart period can use the same history; partial caches
backfill older gaps and refresh recent prices once per day. `jugaad-data` may
split long date ranges into monthly NSE requests, so the first history load can
take longer; later chart loads reuse the cache. If a provider has no prices for
a holding, that holding is omitted from every chart point and reported in the
chart's data notes. The latest point uses the current prices of holdings with
usable history, so it can differ from total Holdings P&L when some holdings
lack prices. NSE MCP is not used as a direct data API: this Flask app has no
configured MCP server connection.

Manual trade adjustments, including bonus, IPO, or demerger entries, participate
in FIFO reconstruction. When trade and adjustment costs do not match the
current holdings average cost, adjustment lots are reconciled to that average
so their chart P&L uses the holdings average cost. When every holding has usable
history, the latest chart P&L matches Holdings; unavailable holdings are excluded.
Zero-cost adjustments are flagged.
Corporate actions or incomplete trade records can still affect reconstructed
lots. The imported tradebook files do not provide CNC/MIS product labels;
same-day buys are processed before sells, matching the Holdings breakdown FIFO
behavior. Holdings XIRR is calculated only for open lots up to the current
quantity; a date with exactly offsetting buy and sell quantities is excluded as
a likely MIS round trip. This is a heuristic and can also exclude legitimate
same-day CNC activity because product labels are unavailable.

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
   No separate price-history API keys are needed: `jugaad-data` retrieves NSE
   prices, and MFAPI provides mutual-fund NAV history.
5. **Start the app:**
   `python app.py`

## 🔑 Security Note
**Crucially, never commit your local `.env` file to GitHub, as it contains confidential API keys.** The `.gitignore` file is configured to prevent this.

## 📚 Built with
- Flask
- Python
- Kite Connect API