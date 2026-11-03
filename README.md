# Kite Holdings Analyzer

This is a Python-based web application designed to analyze and display investment holdings using the Kite Connect API.

## 🚀 Features
- Fetches real-time holdings data.
- Provides tools for calculating metrics like Profit & Loss (PNL).
- Persistent session management using a local SQLite database.

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