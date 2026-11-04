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
- Persistent session management using a local SQLite database.

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

## 🔑 Security Note
**Crucially, never commit your local `.env` file to GitHub, as it contains confidential API keys.** The `.gitignore` file is configured to prevent this.

## 📚 Built with
- Flask
- Python
- Kite Connect API