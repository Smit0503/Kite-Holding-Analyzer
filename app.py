import json
import os
import re
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
from src.zerodha_trade_importer import (
    add_manual_adjustment,
    append_tradebook_directory,
    ensure_trade_tables,
    list_trade_adjustments,
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "session.db")
TRADEBOOK_FOLDER = os.path.abspath(os.path.join(BASE_DIR, "..", "TradeBook"))

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
        sign = "+" if num > 0 else "-" if num < 0 else ""
        return f"{sign}{abs(num):,.0f}"
    except (TypeError, ValueError):
        return "0"


def xnpv(rate, cashflows):
    if not cashflows:
        return 0.0
    total = 0.0
    today = datetime.utcnow().date()
    for dt, amount in cashflows:
        if dt is None:
            continue
        days = (today - dt.date()).days
        if days < 0:
            days = 0
        total += amount / ((1 + rate) ** (days / 365.0)) if rate != -1 else amount
    return total


def xirr(cashflows):
    if len(cashflows) < 2:
        return 0.0
    has_positive = any(amount > 0 for _, amount in cashflows)
    has_negative = any(amount < 0 for _, amount in cashflows)
    if not (has_positive and has_negative):
        return 0.0

    low = -0.9999
    high = 10.0
    for _ in range(200):
        mid = (low + high) / 2
        value = xnpv(mid, cashflows)
        if value > 0:
            low = mid
        else:
            high = mid
    return ((low + high) / 2) * 100


def calculate_symbol_xirr(symbol, asset_class, current_price, current_quantity, isin=""):
    if not symbol or not current_price or current_quantity <= 0:
        return 0.0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    isin = (isin or "").upper().strip()
    rows = []
    if isin:
        rows = conn.execute(
            "SELECT trade_type, quantity, price, trade_date FROM trade_imports WHERE isin = ? AND asset_class = ? ORDER BY date(trade_date) ASC",
            (isin, asset_class.lower()),
        ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT trade_type, quantity, price, trade_date FROM trade_imports WHERE symbol = ? AND asset_class = ? ORDER BY date(trade_date) ASC",
            (symbol.upper(), asset_class.lower()),
        ).fetchall()
    adjustments = conn.execute(
        "SELECT quantity, amount, purchase_date FROM trade_adjustments WHERE symbol = ? AND asset_class = ? ORDER BY date(purchase_date) ASC",
        (symbol.upper(), asset_class.lower()),
    ).fetchall()
    conn.close()

    day_totals = {}
    for row in rows:
        try:
            trade_date = datetime.fromisoformat(str(row["trade_date"])[:19])
        except ValueError:
            try:
                trade_date = datetime.strptime(str(row["trade_date"]), "%Y-%m-%d")
            except ValueError:
                continue
        day_key = trade_date.date().isoformat()
        day_totals.setdefault(day_key, {"BUY": 0.0, "SELL": 0.0})
        qty = float(row["quantity"] or 0)
        if row["trade_type"] == "BUY":
            day_totals[day_key]["BUY"] += qty
        else:
            day_totals[day_key]["SELL"] += qty

    cashflows = []
    for row in rows:
        trade_date = row["trade_date"]
        try:
            dt = datetime.fromisoformat(trade_date[:19])
        except ValueError:
            try:
                dt = datetime.strptime(trade_date, "%Y-%m-%d")
            except ValueError:
                continue
        day_key = dt.date().isoformat()
        same_day_mis = day_totals.get(day_key, {}).get("BUY", 0.0) > 0 and day_totals.get(day_key, {}).get("SELL", 0.0) > 0
        if same_day_mis:
            continue
        qty = float(row["quantity"] or 0)
        price = float(row["price"] or 0)
        if row["trade_type"] == "BUY":
            cashflows.append((dt, -(qty * price)))
        else:
            cashflows.append((dt, (qty * price)))

    for row in adjustments:
        try:
            dt = datetime.strptime(row["purchase_date"], "%Y-%m-%d")
        except ValueError:
            continue
        cashflows.append((dt, -float(row["amount"] or 0)))

    if not cashflows:
        return 0.0

    cashflows.append((datetime.utcnow(), current_quantity * current_price))
    return xirr(cashflows)


def calculate_weighted_xirr(rows):
    if not rows:
        return 0.0

    total_value = 0.0
    total_weighted = 0.0
    for row in rows:
        value = float(row.get("quantity") or 0) * float(row.get("ltp") or row.get("average_price") or 0)
        if value <= 0:
            continue
        xirr = calculate_symbol_xirr(
            row.get("tradingsymbol") or row.get("symbol") or "",
            row.get("asset_type") or "equity",
            float(row.get("ltp") or row.get("average_price") or 0),
            float(row.get("quantity") or 0),
            row.get("isin") or "",
        )
        total_value += value
        total_weighted += xirr * value
    if total_value == 0:
        return 0.0
    return total_weighted / total_value


def numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def holding_quantity(item):
    quantity = numeric(item.get("quantity"))
    t1_quantity = numeric(item.get("t1_quantity"))
    units = numeric(item.get("units"))

    if quantity and t1_quantity:
        return quantity + t1_quantity
    if quantity:
        return quantity
    if t1_quantity:
        return t1_quantity
    if units:
        return units
    return 0.0


def normalize_holding_row(item):
    qty = holding_quantity(item)

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


def parse_asset_filters(raw_filters):
    if raw_filters is None:
        return {"all"}
    if isinstance(raw_filters, str):
        candidates = [part.strip().lower() for part in raw_filters.split(",") if part and part.strip()]
    else:
        candidates = [str(value).strip().lower() for value in raw_filters if value and str(value).strip()]
    if not candidates or "all" in candidates or "*" in candidates:
        return {"all"}
    valid = {value for value in candidates if value in {"equity", "debt", "mf", "metal"}}
    return valid if valid else {"all"}


def apply_asset_filter(rows, raw_filters):
    selected = parse_asset_filters(raw_filters)
    if "all" in selected:
        return rows
    return [row for row in rows if str((row.get("asset_type") or "equity")).lower() in selected]


def normalize_symbol_key(value):
    if value is None:
        return ""
    text = str(value).upper().strip().replace("_", " ").replace("-", " ")
    text = re.sub(r"\b(DIRECT|GROWTH|REGULAR|REINVEST|PLAN|FUND|ETF|INDEX)\b", " ", text)
    text = re.sub(r"[^A-Z0-9]+", "", text)
    return text


def extract_isin(item):
    if not isinstance(item, dict):
        return ""
    isin = str(item.get("isin") or item.get("ISIN") or "").upper().strip()
    if isin:
        return isin
    ts = str(item.get("tradingsymbol") or "").upper().strip()
    if len(ts) == 12 and ts.startswith(("INF", "INE", "IN0")):
        return ts
    return ""


def get_symbol_match_value(item):
    if not isinstance(item, dict):
        return "", "", ""
    symbol = str(item.get("tradingsymbol") or item.get("symbol") or "").upper().strip()
    isin = extract_isin(item)
    name = str(item.get("name") or item.get("fund") or item.get("scheme") or item.get("fund_name") or "").upper().strip()
    return symbol, isin, name


def get_all_holdings_entries(holdings=None, mf_holdings=None):
    entries = []
    for payload in (holdings, mf_holdings):
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            entries.extend([item for item in data if isinstance(item, dict)])
        elif isinstance(payload, list):
            entries.extend([item for item in payload if isinstance(item, dict)])
    return entries


def find_holding_match(symbol, holdings_data=None, mf_holdings_data=None):
    target = normalize_symbol_key(symbol)
    if not target:
        return None

    candidate_entries = get_all_holdings_entries(holdings_data, mf_holdings_data)
    if not candidate_entries and isinstance(holdings_data, list):
        candidate_entries = holdings_data

    for item in candidate_entries:
        if not isinstance(item, dict):
            continue
        symbol_value, isin_value, name_value = get_symbol_match_value(item)
        candidates = [
            symbol_value,
            isin_value,
            name_value,
            str(item.get("tradingsymbol") or item.get("symbol") or "").upper().strip(),
            str(item.get("name") or item.get("fund") or item.get("scheme") or item.get("fund_name") or "").upper().strip(),
            str(item.get("fund_name") or item.get("scheme") or "").upper().strip(),
        ]
        for candidate in candidates:
            if normalize_symbol_key(candidate) == target:
                return item
    return None


def get_tradebook_net_quantity(symbol, isin=""):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    symbol = (symbol or "").upper().strip()
    isin = (isin or "").upper().strip()
    rows = []
    if isin:
        rows = conn.execute(
            "SELECT trade_type, quantity FROM trade_imports WHERE isin = ? ORDER BY date(trade_date) ASC",
            (isin,),
        ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT trade_type, quantity FROM trade_imports WHERE symbol = ? ORDER BY date(trade_date) ASC",
            (symbol,),
        ).fetchall()
    net_qty = 0.0
    for row in rows:
        qty = float(row["quantity"] or 0)
        if str(row["trade_type"] or "BUY").upper() == "BUY":
            net_qty += qty
        else:
            net_qty -= qty
    adjustments = conn.execute(
        "SELECT quantity, amount FROM trade_adjustments WHERE symbol = ? ORDER BY date(purchase_date) ASC",
        (symbol,),
    ).fetchall()
    if isin:
        adjustment_rows = conn.execute(
            "SELECT quantity, amount FROM trade_adjustments WHERE symbol IN (SELECT symbol FROM trade_imports WHERE isin = ?) ORDER BY date(purchase_date) ASC",
            (isin,),
        ).fetchall()
        adjustments = adjustments + adjustment_rows
    for row in adjustments:
        net_qty += float(row["quantity"] or 0)
    conn.close()
    return net_qty


def get_breakdown_buy_quantity(symbol, isin=""):
    symbol = (symbol or "").upper().strip()
    isin_value = (isin or "").upper().strip()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if isin_value:
        imports = conn.execute(
            "SELECT symbol, isin, trade_date AS date, trade_type AS type, quantity, price FROM trade_imports WHERE isin = ? ORDER BY date(trade_date) ASC",
            (isin_value,),
        ).fetchall()
    else:
        imports = conn.execute(
            "SELECT symbol, isin, trade_date AS date, trade_type AS type, quantity, price FROM trade_imports WHERE symbol = ? ORDER BY date(trade_date) ASC",
            (symbol,),
        ).fetchall()
    if isin_value:
        adjustments = conn.execute(
            "SELECT symbol, purchase_date AS date, reason AS type, quantity, amount AS price, notes FROM trade_adjustments WHERE isin = ? OR symbol = ? ORDER BY date(purchase_date) ASC",
            (isin_value, symbol),
        ).fetchall()
    else:
        adjustments = conn.execute(
            "SELECT symbol, purchase_date AS date, reason AS type, quantity, amount AS price, notes FROM trade_adjustments WHERE symbol = ? ORDER BY date(purchase_date) ASC",
            (symbol,),
        ).fetchall()
    conn.close()

    lots = []
    for row in imports:
        lots.append({
            "date": str(row["date"] or "").split("T")[0],
            "type": str(row["type"] or "BUY").upper(),
            "quantity": float(row["quantity"] or 0),
            "price": float(row["price"] or 0),
        })
    for row in adjustments:
        lots.append({
            "date": str(row["date"] or "").split("T")[0],
            "type": "BUY",
            "quantity": float(row["quantity"] or 0),
            "price": float(row["price"] or 0),
            "notes": str(row["notes"] or "").strip(),
        })

    available_lots = []
    for day in sorted({lot["date"] for lot in lots}):
        queue = []
        for lot in sorted([item for item in lots if item["date"] == day], key=lambda item: (item["type"] == "SELL", item["price"])):
            if str(lot.get("type") or "BUY").upper() == "BUY":
                queue.append({
                    "date": day,
                    "trade_type": "BUY",
                    "type": "BUY",
                    "quantity": float(lot["quantity"] or 0),
                    "price": float(lot["price"] or 0),
                    "notes": str(lot.get("notes") or "").strip(),
                })
            else:
                sold = float(lot["quantity"] or 0)
                while sold > 0 and queue:
                    first = queue[0]
                    matched = min(float(first["quantity"] or 0), sold)
                    first["quantity"] -= matched
                    sold -= matched
                    if first["quantity"] <= 0:
                        queue.pop(0)
        for row in queue:
            if float(row["quantity"] or 0) > 0:
                available_lots.append({
                    "date": day,
                    "type": "BUY",
                    "quantity": float(row["quantity"] or 0),
                    "price": float(row["price"] or 0),
                    "notes": str(row.get("notes") or "").strip(),
                })

    return sum(float(lot["quantity"] or 0) for lot in available_lots)


def get_discrepancy_status_for_symbol(symbol, holdings=None, mf_holdings=None):
    if not symbol:
        return {"has_discrepancy": False, "quantity": 0.0}
    symbol = str(symbol).upper().strip()
    holdings_data = holdings or SESSION.get("holdings") or {}
    mf_data = mf_holdings if mf_holdings is not None else SESSION.get("mf_holdings") or []

    matched = find_holding_match(symbol, holdings_data, mf_data)
    if not isinstance(matched, dict):
        return {"has_discrepancy": False, "quantity": 0.0}

    current_qty = holding_quantity(matched)
    matched_isin = extract_isin(matched)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    import_rows = conn.execute(
        "SELECT COUNT(*) AS row_count FROM trade_imports WHERE (? != '' AND isin = ?) OR symbol = ?",
        (matched_isin, matched_isin, symbol),
    ).fetchone()
    adjustment_rows = conn.execute(
        "SELECT COUNT(*) AS row_count FROM trade_adjustments WHERE symbol = ? OR (? != '' AND symbol IN (SELECT symbol FROM trade_imports WHERE isin = ?))",
        (symbol, matched_isin, matched_isin),
    ).fetchone()
    conn.close()

    total_trade_rows = int((import_rows["row_count"] if import_rows else 0) or 0) + int((adjustment_rows["row_count"] if adjustment_rows else 0) or 0)
    if total_trade_rows == 0:
        if current_qty > 0:
            return {"has_discrepancy": True, "quantity": current_qty, "holding_qty": current_qty, "tradebook_qty": 0.0, "breakdown_qty": 0.0, "asset_class": classify_asset(matched)}
        return {"has_discrepancy": False, "quantity": 0.0, "holding_qty": current_qty, "tradebook_qty": 0.0, "breakdown_qty": 0.0, "asset_class": classify_asset(matched)}

    trade_qty = get_tradebook_net_quantity(symbol, matched_isin or "")
    breakdown_buy_qty = get_breakdown_buy_quantity(symbol, matched_isin or "")
    difference = current_qty - trade_qty

    if abs(difference) <= 0.5:
        return {"has_discrepancy": False, "quantity": 0.0, "holding_qty": current_qty, "tradebook_qty": trade_qty, "breakdown_qty": breakdown_buy_qty, "asset_class": classify_asset(matched)}

    if trade_qty == current_qty:
        return {"has_discrepancy": False, "quantity": 0.0, "holding_qty": current_qty, "tradebook_qty": trade_qty, "breakdown_qty": breakdown_buy_qty, "asset_class": classify_asset(matched)}

    if abs(current_qty - breakdown_buy_qty) <= 0.5:
        return {"has_discrepancy": False, "quantity": 0.0, "holding_qty": current_qty, "tradebook_qty": trade_qty, "breakdown_qty": breakdown_buy_qty, "asset_class": classify_asset(matched)}

    if trade_qty > current_qty and current_qty > 0:
        return {"has_discrepancy": False, "quantity": 0.0, "holding_qty": current_qty, "tradebook_qty": trade_qty, "breakdown_qty": breakdown_buy_qty, "asset_class": classify_asset(matched)}

    discrepancy = abs(difference)
    return {
        "has_discrepancy": discrepancy > 0.5001,
        "quantity": discrepancy,
        "holding_qty": current_qty,
        "tradebook_qty": trade_qty,
        "breakdown_qty": breakdown_buy_qty,
        "asset_class": classify_asset(matched),
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
    ensure_trade_tables(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    adjustment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trade_adjustments)").fetchall()}
    if "isin" not in adjustment_columns:
        conn.execute("ALTER TABLE trade_adjustments ADD COLUMN isin TEXT")

    session_row = conn.execute(
        "SELECT holdings_json, mf_holdings_json FROM session WHERE id = 1"
    ).fetchone()
    isin_by_symbol = {}
    if session_row:
        for payload_json in (session_row["holdings_json"], session_row["mf_holdings_json"]):
            if not payload_json:
                continue
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payload = payload.get("data") or []
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("tradingsymbol") or item.get("symbol") or item.get("fund") or item.get("scheme") or item.get("name") or "").upper().strip()
                isin_value = extract_isin(item)
                if symbol and isin_value:
                    isin_by_symbol[symbol] = isin_value

        for symbol, isin_value in isin_by_symbol.items():
            conn.execute(
                "UPDATE trade_adjustments SET isin = ? WHERE upper(symbol) = ? AND (isin IS NULL OR trim(isin) = '')",
                (isin_value, symbol),
            )
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


@app.route('/trade-adjustments', methods=['GET', 'POST'])
def trade_adjustments_page():
    if request.method == 'POST':
        symbol = request.form.get('symbol', '').strip()
        asset_class = request.form.get('asset_class', 'equity').strip().lower()
        reason = request.form.get('reason', '').strip()
        quantity = request.form.get('quantity', '0')
        amount = request.form.get('amount', '0')
        purchase_date = request.form.get('purchase_date', '').strip()
        notes = request.form.get('notes', '').strip()

        if not symbol or not purchase_date or not amount:
            return render_template('trade_adjustments.html', adjustments=list_trade_adjustments(DB_PATH), error='Symbol, date, and amount are required.'), 400

        add_manual_adjustment(
            symbol=symbol,
            asset_class=asset_class,
            quantity=quantity,
            amount=amount,
            purchase_date=purchase_date,
            reason=reason,
            notes=notes,
            isin=extract_isin(find_holding_match(symbol, SESSION.get("holdings"), SESSION.get("mf_holdings")) or {}),
            db_path=DB_PATH,
        )
        return redirect('/trade-adjustments')

    return render_template('trade_adjustments.html', adjustments=list_trade_adjustments(DB_PATH), error=None)


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
            "isin": extract_isin(item),
            "tradingsymbol": item.get("fund") or item.get("scheme") or item.get("name") or "MF",
            "product": item.get("product") or "MF",
            "exchange": item.get("exchange") or "MF",
            "is_mf": True,
        }
        for item in mf_items
        if isinstance(item, dict)
    ]

    asset_filter_values = request.args.getlist("filter") or ["all"]
    asset_filter = ",".join(asset_filter_values) if len(asset_filter_values) > 1 else asset_filter_values[0]
    selected_assets = parse_asset_filters(asset_filter_values)
    rows = []
    tradebook = SESSION.get("tradebook") or []
    for item in all_items:
        if not isinstance(item, dict):
            continue
        quantity = holding_quantity(item)
        asset_type = classify_asset(item)
        if "all" not in selected_assets and asset_type not in selected_assets:
            continue
        metrics = normalize_holding_row(item)
        rows.append(
            {
                "tradingsymbol": item.get("tradingsymbol") or item.get("scheme") or item.get("name") or "MF",
                "isin": extract_isin(item),
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
    day_pnl = sum(float(row["quantity"]) * float(row["day_change"] or 0) for row in rows)

    aggregate_by_asset = {}
    for row in rows:
        asset_type = row["asset_type"]
        aggregate_by_asset.setdefault(asset_type, {"invested": 0.0, "current": 0.0, "pnl": 0.0})
        aggregate_by_asset[asset_type]["invested"] += float(row["quantity"]) * float(row["average_price"] or 0)
        aggregate_by_asset[asset_type]["current"] += float(row["quantity"]) * float(row["ltp"] or row["average_price"] or 0)
        aggregate_by_asset[asset_type]["pnl"] += float(row["pnl"])

    asset_xirr = {}
    for asset_key in ['equity', 'debt', 'mf', 'metal']:
        asset_rows = [row for row in rows if row['asset_type'] == asset_key]
        asset_xirr[asset_key] = calculate_weighted_xirr(asset_rows)

    return render_template(
        "holdings.html",
        rows=rows,
        user_name=(SESSION.get("user") or {}).get("user_name") or (SESSION.get("user") or {}).get("name") or "User",
        holdings_count=len(rows),
        portfolio_value=portfolio_value,
        invested_value=invested_value,
        total_pnl=total_pnl,
        total_profit_pct=total_profit_pct,
        day_pnl=day_pnl,
        portfolio_xirr=calculate_weighted_xirr(rows),
        asset_xirr=asset_xirr,
        aggregate_by_asset=aggregate_by_asset,
        asset_filter=asset_filter,
        asset_filter_selected=selected_assets,
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


@app.route('/api/refresh-tradebook', methods=['POST'])
def api_refresh_tradebook():
    if not os.path.isdir(TRADEBOOK_FOLDER):
        return jsonify({"error": f"Tradebook folder not found at {TRADEBOOK_FOLDER}."}), 404

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM trade_imports")
        conn.commit()
        conn.close()

        result = append_tradebook_directory(TRADEBOOK_FOLDER, DB_PATH)
        total_rows = sum(result.values()) if isinstance(result, dict) else 0
        return jsonify({"success": True, "message": "Tradebook refreshed from CSV files.", "files": result, "total_rows": total_rows}), 200
    except Exception as exc:
        return jsonify({"error": f"Failed to refresh tradebook: {exc}"}), 500


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
                    "isin": extract_isin(item),
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
                "isin": extract_isin(item),
                "asset_type": asset_type,
                "quantity": normalized["quantity"],
                "average_price": normalized["average_price"],
                "ltp": normalized["ltp"],
                "day_change": normalized["day_change"],
                "pnl": normalized["pnl"],
                "purchase_date": item.get("purchase_date") or "N/A",
            })

        selected_assets = parse_asset_filters(request.args.getlist("filter") or ["all"])
        filtered_rows = apply_asset_filter(rows, selected_assets)

        aggregate = {
            'equity': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
            'debt': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
            'mf': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
            'metal': {'invested': 0.0, 'current': 0.0, 'pnl': 0.0},
        }

        total_invested = 0.0
        total_current = 0.0
        total_pnl = 0.0
        day_pnl = 0.0

        for row in filtered_rows:
            qty = float(row['quantity'] or 0)
            avg = float(row['average_price'] or 0)
            ltp = float(row['ltp'] or row['average_price'] or 0)
            pnl = float(row['pnl'] or 0)
            day_change = float(row['day_change'] or 0)
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
            day_pnl += qty * day_change

        SESSION["holdings"] = raw_holdings
        SESSION["mf_holdings"] = mf_holdings
        save_session_to_db(
            access_token=SESSION.get("access_token"),
            user=SESSION.get("user"),
            holdings=raw_holdings,
            mf_holdings=mf_holdings,
        )

        asset_xirr = {}
        for asset_key in ['equity', 'debt', 'mf', 'metal']:
            asset_xirr[asset_key] = calculate_weighted_xirr([row for row in filtered_rows if row.get('asset_type') == asset_key])

        return jsonify({
            "rows": filtered_rows,
            "invested_value": total_invested,
            "portfolio_value": total_current,
            "total_pnl": total_pnl,
            "total_profit_pct": ((total_pnl / total_invested) * 100) if total_invested else 0.0,
            "day_pnl": day_pnl,
            "aggregate_by_asset": aggregate,
            "asset_xirr": asset_xirr,
            "portfolio_xirr": calculate_weighted_xirr(filtered_rows),
            "user_name": (SESSION.get("user") or {}).get("user_name") or (SESSION.get("user") or {}).get("name") or "User",
            "asset_filter": ",".join(sorted(selected_assets)) if "all" not in selected_assets else "all",
        })
    
    except Exception as e:
        print(f"Error refreshing data: {e}")
        return jsonify({"error": f"Failed to fetch data: {str(e)}"}), 500


@app.route('/api/symbol-breakdown/<symbol>')
def api_symbol_breakdown(symbol):
    symbol = symbol.strip().upper()
    if not symbol:
        return jsonify({"error": "Missing symbol."}), 400

    holdings = SESSION.get("holdings") or {}
    mf_holdings = SESSION.get("mf_holdings") or []
    holdings_data = holdings.get("data") if isinstance(holdings, dict) else (holdings if isinstance(holdings, list) else [])
    matched = find_holding_match(symbol, holdings_data, mf_holdings)
    if not isinstance(matched, dict):
        matched = None

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    isin_value = extract_isin(matched)
    if isin_value:
        imports = conn.execute(
            "SELECT symbol, isin, trade_date AS date, trade_type AS type, quantity, price FROM trade_imports WHERE isin = ? ORDER BY date(trade_date) ASC",
            (isin_value,),
        ).fetchall()
    else:
        imports = conn.execute(
            "SELECT symbol, isin, trade_date AS date, trade_type AS type, quantity, price FROM trade_imports WHERE symbol = ? ORDER BY date(trade_date) ASC",
            (symbol,),
        ).fetchall()
    if isin_value:
        adjustments = conn.execute(
            "SELECT symbol, purchase_date AS date, reason AS type, quantity, amount AS price, notes FROM trade_adjustments WHERE isin = ? OR symbol = ? ORDER BY date(purchase_date) ASC",
            (isin_value, symbol),
        ).fetchall()
    else:
        adjustments = conn.execute(
            "SELECT symbol, purchase_date AS date, reason AS type, quantity, amount AS price, notes FROM trade_adjustments WHERE symbol = ? ORDER BY date(purchase_date) ASC",
            (symbol,),
        ).fetchall()
    conn.close()

    lots = []
    for row in imports:
        lots.append({
            "date": str(row["date"] or "").split("T")[0],
            "type": str(row["type"] or "BUY").upper(),
            "quantity": float(row["quantity"] or 0),
            "price": float(row["price"] or 0),
        })
    for row in adjustments:
        lots.append({
            "date": str(row["date"] or "").split("T")[0],
            "type": "BUY",
            "quantity": float(row["quantity"] or 0),
            "price": float(row["price"] or 0),
            "notes": str(row["notes"] or "").strip(),
        })

    if not lots and isinstance(matched, dict):
        qty = holding_quantity(matched)
        avg = numeric(matched.get("average_price") or matched.get("avg_price") or matched.get("average_nav") or matched.get("avg_nav") or matched.get("cost_price") or 0)
        ltp = numeric(matched.get("last_price") or matched.get("ltp") or matched.get("close_price") or matched.get("nav") or matched.get("last_nav") or matched.get("current_nav") or matched.get("price") or 0)
        if qty > 0:
            lots.append({
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "type": "BUY",
                "quantity": qty,
                "price": avg or ltp or 0,
            })

    available_lots = []
    for day in sorted({lot["date"] for lot in lots}):
        queue = []
        for lot in sorted([item for item in lots if item["date"] == day], key=lambda item: (item["type"] == "SELL", item["price"])):
            if str(lot.get("type") or "BUY").upper() == "BUY":
                queue.append({
                    "date": day,
                    "trade_type": "BUY",
                    "type": "BUY",
                    "quantity": float(lot["quantity"] or 0),
                    "price": float(lot["price"] or 0),
                    "notes": str(lot.get("notes") or "").strip(),
                })
            else:
                sold = float(lot["quantity"] or 0)
                while sold > 0 and queue:
                    first = queue[0]
                    matched = min(float(first["quantity"] or 0), sold)
                    first["quantity"] -= matched
                    sold -= matched
                    if first["quantity"] <= 0:
                        queue.pop(0)
        for row in queue:
            if float(row["quantity"] or 0) > 0:
                available_lots.append({
                    "date": day,
                    "type": "BUY",
                    "quantity": float(row["quantity"] or 0),
                    "price": float(row["price"] or 0),
                    "notes": str(row.get("notes") or "").strip(),
                })

    remaining_lots = []
    for lot in sorted(available_lots, key=lambda item: (item["date"], float(item["price"] or 0))):
        remaining_lots.append({
            "date": lot["date"],
            "type": str(lot.get("type") or "BUY").upper(),
            "quantity": float(lot["quantity"] or 0),
            "price": float(lot["price"] or 0),
            "notes": str(lot.get("notes") or "").strip(),
        })

    ltp = 0.0
    avg = 0.0
    qty = 0.0
    if isinstance(matched, dict):
        qty = holding_quantity(matched)
        avg = numeric(matched.get("average_price") or matched.get("avg_price") or matched.get("average_nav") or matched.get("avg_nav") or matched.get("cost_price") or 0)
        ltp = numeric(
            matched.get("last_price")
            or matched.get("ltp")
            or matched.get("close_price")
            or matched.get("nav")
            or matched.get("last_nav")
            or matched.get("current_nav")
            or matched.get("price")
            or matched.get("average_nav")
            or matched.get("avg_nav")
            or 0
        )

    for lot in remaining_lots:
        ev = float(lot["price"] or 0)
        lot["ltp"] = ltp
        lot["pnl"] = (ltp - ev) * float(lot["quantity"] or 0)
        lot["chg_pct"] = ((ltp - ev) / ev * 100) if ev else 0.0
        lot["xirr"] = 0.0
        lot["cnc_status"] = "CNC"

    return jsonify({
        "symbol": symbol,
        "ltp": ltp,
        "pnl": (ltp - avg) * qty if qty and avg else 0.0,
        "chg_pct": ((ltp - avg) / avg * 100) if avg else 0.0,
        "xirr": 0.0,
        "lots": remaining_lots,
    })


@app.route('/api/symbol-discrepancy/<symbol>')
def api_symbol_discrepancy(symbol):
    clean_symbol = (symbol or '').strip()
    if not clean_symbol:
        return jsonify({"error": "Missing symbol."}), 400
    return jsonify(get_discrepancy_status_for_symbol(clean_symbol, SESSION.get("holdings"), SESSION.get("mf_holdings")))


@app.route('/api/discrepancy', methods=['POST'])
def api_discrepancy():
    symbol = (request.form.get('symbol') or '').strip()
    if not symbol and request.is_json:
        symbol = str((request.json or {}).get('symbol') or '').strip()
    asset_class = (request.form.get('asset_class') or 'equity').strip().lower()
    quantity = request.form.get('quantity', '0')
    amount = request.form.get('amount', '0')
    purchase_date = request.form.get('purchase_date', '').strip()
    notes = request.form.get('notes', '').strip()
    reason = request.form.get('reason', 'DISCREPANCY').strip() or 'DISCREPANCY'

    if not symbol or not purchase_date or not amount:
        return jsonify({"error": "Symbol, date, and amount are required."}), 400

    add_manual_adjustment(
        symbol=symbol,
        asset_class='equity',
        quantity=quantity,
        amount=amount,
        purchase_date=purchase_date,
        reason=reason,
        notes=notes,
        isin=extract_isin(find_holding_match(symbol, SESSION.get("holdings"), SESSION.get("mf_holdings")) or {}),
        db_path=DB_PATH,
    )
    return jsonify({"success": True, "message": "Discrepancy saved."})


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
