import json
import os
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request
from flask_cors import CORS
from dotenv import load_dotenv

from src.kite_client import KiteClient
from src.portfolio import (
    calculate_metrics,
    classify_asset,
    fifo_purchase_date_for_holding,
    fifo_realized_pnl,
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "session.db")

app = Flask(__name__)
CORS(app)


def format_int(value):
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "0"


def format_price(value):
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def format_signed(value):
    try:
        num = float(value)
        sign = "+" if num > 0 else ""
        return f"{sign}{num:,.0f}"
    except (TypeError, ValueError):
        return "0"


def numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_holding_row(item):
    quantity = item.get("quantity")
    if quantity in (None, 0):
        quantity = item.get("t1_quantity") or item.get("units") or 0
    qty = numeric(quantity)

    avg_price = (
        item.get("average_price")
        or item.get("avg_price")
        or item.get("average_nav")
        or item.get("avg_nav")
        or item.get("cost_price")
        or item.get("buy_average_price")
        or 0
    )
    current_price = (
        item.get("last_price")
        or item.get("ltp")
        or item.get("close_price")
        or item.get("nav")
        or item.get("last_nav")
        or item.get("current_nav")
        or item.get("price")
        or 0
    )
    close_price = (
        item.get("close_price")
        or item.get("prev_close")
        or item.get("previous_nav")
        or item.get("prev_nav")
        or item.get("close_nav")
        or 0
    )

    avg = numeric(avg_price)
    ltp = numeric(current_price)
    close = numeric(close_price)

    raw_day_change = item.get("day_change")
    if raw_day_change in (None, 0):
        day_change_value = (ltp - close) if close else 0.0
    else:
        day_change_value = raw_day_change

    raw_pnl = item.get("pnl")
    if raw_pnl in (None, 0):
        pnl_value = (ltp - avg) * qty if qty and avg else 0.0
    else:
        pnl_value = raw_pnl

    if raw_pnl in (None, 0) and avg == 0 and qty and ltp:
        pnl_value = 0.0

    return {
        "quantity": qty,
        "average_price": avg,
        "ltp": ltp,
        "day_change": numeric(day_change_value),
        "pnl": numeric(pnl_value),
    }


app.jinja_env.filters["format_int"] = format_int
app.jinja_env.filters["format_price"] = format_price
app.jinja_env.filters["format_signed"] = format_signed

try:
    kite_client = KiteClient()
except ValueError as exc:
    print(f"Startup error: {exc}")
    kite_client = None


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT,
            user_json TEXT,
            holdings_json TEXT,
            tradebook_json TEXT,
            mf_holdings_json TEXT,
            updated_at TEXT
        )
        """
    )
    columns = [row[1] for row in conn.execute("PRAGMA table_info(session)").fetchall()]
    if "tradebook_json" not in columns:
        conn.execute("ALTER TABLE session ADD COLUMN tradebook_json TEXT")
    if "mf_holdings_json" not in columns:
        conn.execute("ALTER TABLE session ADD COLUMN mf_holdings_json TEXT")
    conn.commit()
    conn.close()


def load_session_from_db():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT access_token, user_json, holdings_json, tradebook_json, mf_holdings_json FROM session WHERE id = 1"
    ).fetchone()
    conn.close()
    if not row:
        return {"access_token": None, "user": None, "holdings": None, "tradebook": None, "mf_holdings": None}
    access_token, user_json, holdings_json, tradebook_json, mf_holdings_json = row
    return {
        "access_token": access_token,
        "user": json.loads(user_json) if user_json else None,
        "holdings": json.loads(holdings_json) if holdings_json else None,
        "tradebook": json.loads(tradebook_json) if tradebook_json else None,
        "mf_holdings": json.loads(mf_holdings_json) if mf_holdings_json else None,
    }


def save_session_to_db(access_token=None, user=None, holdings=None, tradebook=None, mf_holdings=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO session (id, access_token, user_json, holdings_json, tradebook_json, mf_holdings_json, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token, user_json=excluded.user_json, holdings_json=excluded.holdings_json, tradebook_json=excluded.tradebook_json, mf_holdings_json=excluded.mf_holdings_json, updated_at=excluded.updated_at",
        (
            access_token,
            json.dumps(user) if user is not None else None,
            json.dumps(holdings) if holdings is not None else None,
            json.dumps(tradebook) if tradebook is not None else None,
            json.dumps(mf_holdings) if mf_holdings is not None else None,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_default_date_window(days=90):
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


init_db()
SESSION = load_session_from_db()


@app.route('/')
def index():
    if SESSION.get("access_token"):
        return redirect('/holdings')
    return redirect('/login')


@app.route('/login', methods=['GET'])
def login():
    if not kite_client:
        return jsonify({"error": "Kite client not configured. Check .env."}), 500
    if SESSION.get("access_token"):
        return redirect('/holdings')
    return redirect(kite_client.generate_login_url())


@app.route('/oauth/callback', methods=['GET'])
def oauth_callback():
    if not kite_client:
        return jsonify({"error": "Kite client not configured."}), 500

    request_token = request.args.get("request_token")
    status = request.args.get("status")
    if status != "success" or not request_token:
        return jsonify({"error": "Kite did not return a valid request_token", "args": request.args.to_dict()}), 400

    try:
        token_data = kite_client.exchange_request_token(request_token)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    access_token = token_data.get("access_token")
    if not access_token:
        return jsonify({"error": "Token exchange did not return access_token.", "raw": token_data}), 500

    SESSION["access_token"] = access_token
    try:
        user = kite_client.get_profile(access_token)
        SESSION["user"] = user
        save_session_to_db(access_token=access_token, user=user, holdings=None)
    except Exception:
        SESSION["user"] = {"message": "Profile fetch failed but access token was obtained."}
        save_session_to_db(access_token=access_token, user=SESSION["user"], holdings=None)

    return redirect('/holdings')


@app.route('/holdings', methods=['GET'])
def holdings_page():
    if not SESSION.get("access_token"):
        return redirect('/login')

    try:
        holdings = SESSION.get("holdings")
        if not holdings:
            holdings = kite_client.get_holdings(SESSION["access_token"])
            SESSION["holdings"] = holdings
        mf_holdings = SESSION.get("mf_holdings")
        if mf_holdings is None:
            try:
                mf_holdings = kite_client.get_mf_holdings(SESSION["access_token"])
                SESSION["mf_holdings"] = mf_holdings
            except Exception:
                mf_holdings = []
        save_session_to_db(
            access_token=SESSION["access_token"],
            user=SESSION.get("user"),
            holdings=SESSION.get("holdings"),
            tradebook=SESSION.get("tradebook"),
            mf_holdings=SESSION.get("mf_holdings"),
        )
    except Exception as exc:
        return jsonify({"error": f"Holdings fetch failed: {exc}"}), 500

    items = holdings.get("data") if isinstance(holdings, dict) else holdings
    if not isinstance(items, list):
        items = []

    mf_items = []
    if isinstance(mf_holdings, list):
        mf_items = mf_holdings
    elif isinstance(mf_holdings, dict):
        data = mf_holdings.get("data")
        if isinstance(data, list):
            mf_items = data

    all_items = items + [
        {
            **item,
            "tradingsymbol": item.get("fund") or item.get("scheme") or item.get("name") or "MF",
            "product": item.get("product") or "MF",
            "exchange": item.get("exchange") or "MF",
            "is_mf": True,
        }
        for item in mf_items
        if isinstance(item, dict)
    ]

    asset_filter = request.args.get("filter", "all").lower()
    rows = []
    tradebook = SESSION.get("tradebook") or []
    for item in all_items:
        if not isinstance(item, dict):
            continue
        quantity = item.get("quantity")
        if quantity in (None, 0):
            quantity = item.get("t1_quantity") or item.get("units") or 0
        asset_type = classify_asset(item)
        if asset_filter != "all" and asset_type != asset_filter:
            continue
        metrics = normalize_holding_row(item)
        rows.append(
            {
                "tradingsymbol": item.get("tradingsymbol") or item.get("scheme") or item.get("name") or "MF",
                "quantity": int(float(quantity or metrics["quantity"] or 0)),
                "average_price": metrics["average_price"],
                "ltp": metrics["ltp"],
                "day_change": metrics["day_change"],
                "pnl": metrics["pnl"],
                "asset_type": asset_type,
                "purchase_date": fifo_purchase_date_for_holding(item, tradebook),
            }
        )

    portfolio_value = sum(float(row["quantity"]) * float(row["ltp"] or row["average_price"] or 0) for row in rows)
    invested_value = sum(float(row["quantity"]) * float(row["average_price"] or 0) for row in rows)
    total_pnl = sum(float(row["pnl"]) for row in rows)
    total_profit_pct = ((portfolio_value - invested_value) / invested_value * 100) if invested_value else 0.0

    aggregate_by_asset = {}
    for row in rows:
        asset_type = row["asset_type"]
        aggregate_by_asset.setdefault(asset_type, {"invested": 0.0, "current": 0.0, "pnl": 0.0})
        aggregate_by_asset[asset_type]["invested"] += float(row["quantity"]) * float(row["average_price"] or 0)
        aggregate_by_asset[asset_type]["current"] += float(row["quantity"]) * float(row["ltp"] or row["average_price"] or 0)
        aggregate_by_asset[asset_type]["pnl"] += float(row["pnl"])

    return render_template(
        "holdings.html",
        rows=rows,
        user_name=(SESSION.get("user") or {}).get("user_name") or (SESSION.get("user") or {}).get("name") or "User",
        holdings_count=len(rows),
        portfolio_value=portfolio_value,
        invested_value=invested_value,
        total_pnl=total_pnl,
        total_profit_pct=total_profit_pct,
        aggregate_by_asset=aggregate_by_asset,
        asset_filter=asset_filter,
    )


@app.route('/tradebook', methods=['GET'])
def tradebook_page():
    if not SESSION.get("access_token"):
        return redirect('/login')

    today = datetime.utcnow().strftime("%Y-%m-%d")
    from_date = request.args.get("from_date") or today
    to_date = request.args.get("to_date") or today
    range_note = None
    if from_date != to_date:
        range_note = "Kite order/trade APIs are day-scoped. They return daily data only, not a complete historical ledger. For older buy dates, import a Zerodha statement/CSV or use a manual trade log."

    try:
        tradebook = kite_client.get_tradebook(SESSION["access_token"], from_date=from_date, to_date=to_date)
        SESSION["tradebook"] = tradebook
        save_session_to_db(
            access_token=SESSION["access_token"],
            user=SESSION.get("user"),
            holdings=SESSION.get("holdings"),
            tradebook=tradebook,
            mf_holdings=SESSION.get("mf_holdings"),
        )
    except Exception as exc:
        tradebook = SESSION.get("tradebook") or []
        if not tradebook:
            return jsonify({"error": f"Tradebook fetch failed: {exc}"}), 500

    summary = fifo_realized_pnl(tradebook, from_date, to_date)
    trade_rows = summary.get("rows", [])
    total_pnl = summary.get("total_pnl", 0.0)

    return render_template(
        "tradebook.html",
        from_date=from_date,
        to_date=to_date,
        trade_rows=trade_rows,
        total_pnl=total_pnl,
        range_note=range_note,
    )


@app.route('/api/holdings', methods=['GET'])
def api_holdings():
    if not SESSION.get("access_token"):
        return jsonify({"error": "Not authenticated. Visit /login first."}), 401
    try:
        holdings = kite_client.get_holdings(SESSION["access_token"])
        SESSION["holdings"] = holdings
        mf_holdings = []
        try:
            mf_holdings = kite_client.get_mf_holdings(SESSION["access_token"])
            SESSION["mf_holdings"] = mf_holdings
        except Exception:
            mf_holdings = []
        save_session_to_db(access_token=SESSION["access_token"], user=SESSION.get("user"), holdings=holdings, mf_holdings=mf_holdings)
        return jsonify({"holdings": holdings, "mf_holdings": mf_holdings}), 200
    except Exception as exc:
        return jsonify({"error": f"Holdings fetch failed: {exc}"}), 500


@app.route('/mf/holdings', methods=['GET'])
def api_mf_holdings():
    if not SESSION.get("access_token"):
        return jsonify({"error": "Not authenticated. Visit /login first."}), 401
    try:
        mf_holdings = kite_client.get_mf_holdings(SESSION["access_token"])
        SESSION["mf_holdings"] = mf_holdings
        save_session_to_db(access_token=SESSION["access_token"], user=SESSION.get("user"), holdings=SESSION.get("holdings"), mf_holdings=mf_holdings)
        return jsonify({"mf_holdings": mf_holdings}), 200
    except Exception as exc:
        return jsonify({"error": f"MF holdings fetch failed: {exc}"}), 500


@app.route('/api/tradebook', methods=['GET'])
def api_tradebook():
    if not SESSION.get("access_token"):
        return jsonify({"error": "Not authenticated. Visit /login first."}), 401
    today = datetime.utcnow().strftime("%Y-%m-%d")
    from_date = request.args.get("from_date") or today
    to_date = request.args.get("to_date") or today
    try:
        tradebook = kite_client.get_tradebook(SESSION["access_token"], from_date=from_date, to_date=to_date)
        SESSION["tradebook"] = tradebook
        save_session_to_db(access_token=SESSION["access_token"], user=SESSION.get("user"), holdings=SESSION.get("holdings"), tradebook=tradebook)
        summary = fifo_realized_pnl(tradebook, from_date, to_date)
        return jsonify({"from_date": from_date, "to_date": to_date, "tradebook": tradebook, "summary": summary}), 200
    except Exception as exc:
        return jsonify({"error": f"Tradebook fetch failed: {exc}"}), 500


@app.route('/api/refresh')
def api_refresh():
    if not SESSION.get("access_token") or not kite_client:
        return jsonify({"error": "Authentication or client not configured."}), 401

    try:
        raw_holdings = kite_client.get_holdings(SESSION["access_token"])
        mf_holdings = []
        try:
            mf_holdings = kite_client.get_mf_holdings(SESSION["access_token"])
        except Exception:
            mf_holdings = []

        all_items = []
        if isinstance(raw_holdings, dict):
            raw_holdings = raw_holdings.get("data") or []
        if isinstance(raw_holdings, list):
            all_items.extend(raw_holdings)
        if isinstance(mf_holdings, list):
            all_items.extend([
                {
                    **item,
                    "tradingsymbol": item.get("fund") or item.get("scheme") or item.get("name") or "MF",
                    "asset_type": "mf",
                }
                for item in mf_holdings
                if isinstance(item, dict)
            ])

        rows = []
        for item in all_items:
            if not isinstance(item, dict):
                continue
            normalized = normalize_holding_row(item)
            asset_type = classify_asset(item)
            if asset_type not in {"equity", "debt", "mf", "metal"}:
                asset_type = "equity"
            rows.append({
                "tradingsymbol": item.get("tradingsymbol") or item.get("fund") or item.get("scheme") or item.get("name") or "N/A",
                "asset_type": asset_type,
                "quantity": normalized["quantity"],
                "average_price": normalized["average_price"],
                "ltp": normalized["ltp"],
                "day_change": normalized["day_change"],
                "pnl": normalized["pnl"],
                "purchase_date": item.get("purchase_date") or "N/A",
            })

        aggregate = {
            'equity': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
            'debt': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
            'mf': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
            'metal': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
        }

        total_invested = 0.0
        total_current = 0.0
        total_pnl = 0.0

        for row in rows:
            qty = float(row['quantity'] or 0)
            avg = float(row['average_price'] or 0)
            ltp = float(row['ltp'] or row['average_price'] or 0)
            pnl = float(row['pnl'] or 0)
            asset_type = str(row.get('asset_type') or 'equity').strip().lower()
            if asset_type == 'n/a' or asset_type == 'na':
                asset_type = 'equity'
            if asset_type not in aggregate:
                asset_type = 'equity'
            aggregate[asset_type]['invested'] += qty * avg
            aggregate[asset_type]['current'] += qty * ltp
            aggregate[asset_type]['pnl'] += pnl

            total_invested += qty * avg
            total_current += qty * ltp
            total_pnl += pnl

        SESSION["holdings"] = raw_holdings
        SESSION["mf_holdings"] = mf_holdings
        save_session_to_db(
            access_token=SESSION.get("access_token"),
            user=SESSION.get("user"),
            holdings=raw_holdings,
            mf_holdings=mf_holdings,
        )

        return jsonify({
            "rows": rows,
            "invested_value": total_invested,
            "portfolio_value": total_current,
            "total_pnl": total_pnl,
            "total_profit_pct": ((total_pnl / total_invested) * 100) if total_invested else 0.0,
            "aggregate_by_asset": aggregate,
            "user_name": (SESSION.get("user") or {}).get("user_name") or (SESSION.get("user") or {}).get("name") or "User",
            "asset_filter": "all",
        })
    
    except Exception as e:
        print(f"Error refreshing data: {e}")
        return jsonify({"error": f"Failed to fetch data: {str(e)}"}), 500


@app.route('/logout')
def logout():
    SESSION.clear()
    save_session_to_db(access_token=None, user=None, holdings=None, tradebook=None, mf_holdings=None)
    return redirect('/login')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "authenticated": bool(SESSION.get("access_token"))})


if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
