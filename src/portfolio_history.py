import calendar
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from src.portfolio import classify_asset, fifo_open_lots


PERIOD_DAYS = {
    "1M": 31,
    "3M": 92,
    "6M": 183,
    "1Y": 366,
    "3Y": 1096,
    "5Y": 1827,
}
PERIOD_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}
ASSET_CLASSES = {"equity", "mf", "debt", "metal"}
DB_PATH = Path(__file__).resolve().parents[1] / "session.db"
MFAPI_BASE_URL = "https://api.mfapi.in"
MAX_PRICE_FETCH_WORKERS = 4
NSE_SYMBOL_ALIASES = {"WBAG": "WABAG"}


def _connect(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def ensure_history_tables(db_path: Optional[str | Path] = None) -> None:
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS historical_prices (
            instrument_key TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            price_date TEXT NOT NULL,
            close REAL NOT NULL,
            provider TEXT NOT NULL,
            cached_at TEXT NOT NULL,
            PRIMARY KEY (instrument_key, price_date, provider)
        );
        CREATE INDEX IF NOT EXISTS idx_historical_prices_date
            ON historical_prices (price_date, instrument_key);
        CREATE TABLE IF NOT EXISTS price_history_sync (
            instrument_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            coverage_start TEXT NOT NULL,
            coverage_end TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (instrument_key, provider)
        );
        CREATE INDEX IF NOT EXISTS idx_trade_imports_history_isin_date
            ON trade_imports (isin, trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_imports_history_symbol_date
            ON trade_imports (symbol, trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_adjustments_history_isin_date
            ON trade_adjustments (isin, purchase_date);
        CREATE INDEX IF NOT EXISTS idx_trade_adjustments_history_symbol_date
            ON trade_adjustments (symbol, purchase_date);
        """
    )
    conn.commit()
    conn.close()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _clean(value).upper())


def _date_value(value: Any) -> Optional[date]:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _period_start(period: str, today: date) -> date:
    months = PERIOD_MONTHS.get(period, PERIOD_MONTHS["1M"])
    month_index = today.year * 12 + today.month - 1 - months
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _holding_items(holdings: Any, mf_holdings: Any) -> List[Dict[str, Any]]:
    def unpack(value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, dict):
            value = value.get("data", [])
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    return unpack(holdings) + unpack(mf_holdings)


def _asset_class(item: Dict[str, Any]) -> str:
    if _clean(item.get("asset_type")).lower() in ASSET_CLASSES:
        return _clean(item.get("asset_type")).lower()
    return classify_asset(item)


def _holding_identity(item: Dict[str, Any], asset_class: str) -> Tuple[str, str, str]:
    symbol = _clean(item.get("tradingsymbol") or item.get("symbol")).upper()
    isin = _clean(item.get("isin") or item.get("ISIN")).upper()
    if not isin and len(symbol) == 12 and symbol.startswith(("INF", "INE", "IN0")):
        isin = symbol
    identity = isin or f"{asset_class}:{_normalized_name(symbol or item.get('fund') or item.get('scheme') or item.get('name'))}"
    return identity, symbol, isin


def _quantity(item: Dict[str, Any]) -> float:
    for key in ("quantity", "units"):
        try:
            value = float(item.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value:
            if key == "quantity":
                try:
                    value += float(item.get("t1_quantity") or 0)
                except (TypeError, ValueError):
                    pass
            return max(value, 0.0)
    return 0.0


def _position_map(
    holdings: Any,
    mf_holdings: Any,
    selected_assets: Iterable[str],
) -> List[Dict[str, Any]]:
    selected = set(selected_assets)
    if not selected or "all" in selected:
        selected = set(ASSET_CLASSES)
    positions: Dict[str, Dict[str, Any]] = {}
    for item in _holding_items(holdings, mf_holdings):
        asset_class = _asset_class(item)
        if asset_class not in selected:
            continue
        quantity = _quantity(item)
        if quantity <= 0:
            continue
        identity, symbol, isin = _holding_identity(item, asset_class)
        position = positions.setdefault(
            identity,
            {
                "instrument_key": identity,
                "asset_class": asset_class,
                "symbol": symbol,
                "isin": isin,
                "exchange": _clean(item.get("exchange") or ("NSE" if asset_class != "mf" else "")).upper(),
                "name": _clean(item.get("fund") or item.get("scheme") or item.get("name") or symbol),
                "quantity": 0.0,
                "cost_basis": 0.0,
                "market_value": 0.0,
            },
        )
        position["quantity"] += quantity
        try:
            average_price = float(
                item.get("average_price")
                or item.get("avg_price")
                or item.get("average_nav")
                or item.get("avg_nav")
                or item.get("cost_price")
                or 0
            )
        except (TypeError, ValueError):
            average_price = 0.0
        try:
            current_price = float(
                item.get("last_price")
                or item.get("ltp")
                or item.get("close_price")
                or item.get("nav")
                or item.get("last_nav")
                or item.get("current_nav")
                or item.get("price")
                or average_price
            )
        except (TypeError, ValueError):
            current_price = average_price
        position["cost_basis"] += quantity * average_price
        position["market_value"] += quantity * current_price
    for position in positions.values():
        if position["quantity"]:
            position["average_price"] = position.pop("cost_basis") / position["quantity"]
            position["current_price"] = position.pop("market_value") / position["quantity"]
    return list(positions.values())


def _row_matches_position(row: sqlite3.Row, position: Dict[str, Any], symbol_key: str) -> bool:
    isin = _clean(row["isin"]).upper()
    symbol_matches = bool(symbol_key) and _normalized_name(row["symbol"]) == symbol_key
    isin_matches = bool(position["isin"] and isin) and isin == position["isin"]
    return symbol_matches or isin_matches


def _open_lots_for_position(
    conn: sqlite3.Connection,
    position: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    symbol_key = _normalized_name(position["symbol"])
    if position["isin"]:
        trade_rows = conn.execute(
            "SELECT * FROM trade_imports WHERE upper(trim(isin)) = ? OR upper(trim(symbol)) = ? ORDER BY date(trade_date), id",
            (position["isin"], position["symbol"]),
        ).fetchall()
        adjustment_rows = conn.execute(
            "SELECT * FROM trade_adjustments WHERE upper(trim(isin)) = ? OR upper(trim(symbol)) = ? ORDER BY date(purchase_date), id",
            (position["isin"], position["symbol"]),
        ).fetchall()
    else:
        trade_rows = conn.execute(
            "SELECT * FROM trade_imports WHERE upper(trim(symbol)) = ? ORDER BY date(trade_date), id",
            (position["symbol"],),
        ).fetchall()
        adjustment_rows = conn.execute(
            "SELECT * FROM trade_adjustments WHERE upper(trim(symbol)) = ? ORDER BY date(purchase_date), id",
            (position["symbol"],),
        ).fetchall()

    trade_series = next(
        (
            _clean(row["series"]).upper()
            for row in reversed(trade_rows)
            if "series" in row.keys() and _clean(row["series"])
            and (
                "exchange" not in row.keys()
                or not _clean(row["exchange"])
                or _clean(row["exchange"]).upper() == "NSE"
            )
        ),
        "",
    )
    if position["asset_class"] == "equity":
        position["nse_series"] = "EQ"
    else:
        position["nse_series"] = trade_series or ("GB" if position["symbol"].startswith("SGB") else "EQ")

    events: List[Dict[str, Any]] = []
    for row in trade_rows:
        if not _row_matches_position(row, position, symbol_key):
            continue
        if _clean(row["asset_class"]).lower() not in ("", position["asset_class"]):
            continue
        trade_date = _date_value(row["trade_date"])
        if not trade_date:
            continue
        trade_type = _clean(row["trade_type"]).upper()
        if trade_type not in {"BUY", "SELL"}:
            continue
        try:
            quantity, price = abs(float(row["quantity"] or 0)), float(row["price"] or 0)
        except (TypeError, ValueError):
            continue
        events.append({"date": trade_date, "type": trade_type, "quantity": quantity, "price": price, "id": row["id"]})

    for row in adjustment_rows:
        if not _row_matches_position(row, position, symbol_key):
            continue
        if _clean(row["asset_class"]).lower() not in ("", position["asset_class"]):
            continue
        trade_date = _date_value(row["purchase_date"])
        if not trade_date:
            continue
        try:
            quantity, price = float(row["quantity"] or 0), float(row["amount"] or 0)
        except (TypeError, ValueError):
            continue
        if quantity == 0:
            continue
        events.append({
            "date": trade_date,
            "type": "BUY",
            "quantity": abs(quantity),
            "price": price,
            "id": row["id"],
            "adjustment": True,
            "reason": _clean(row["reason"]).lower(),
        })

    warnings = []
    adjustment_dates = [
        event["date"].isoformat()
        for event in events
        if event.get("adjustment") and event["type"] == "BUY" and event["quantity"] > 0
    ]
    for event in events:
        if event.get("adjustment") and event["type"] == "BUY" and event["quantity"] > 0 and event["price"] == 0:
            if any(term in event.get("reason", "") for term in ("bonus", "ipo", "demerger")):
                warnings.append(
                    f"{position['symbol'] or position['isin']}: {event['reason']} adjustment has no recorded cost basis; "
                    "the holdings average cost is used to reconcile the chart when available."
                )

    lots = fifo_open_lots([
        {
            "date": event["date"].isoformat(),
            "type": event["type"],
            "quantity": event["quantity"],
            "price": event["price"],
            "adjustment": event.get("adjustment", False),
            "reason": event.get("reason", ""),
        }
        for event in events
    ])

    open_quantity = sum(lot["quantity"] for lot in lots)
    target_quantity = float(position["quantity"])
    if open_quantity > target_quantity + 1e-6:
        to_exclude = open_quantity - target_quantity
        while to_exclude > 1e-9 and lots:
            excluded = min(to_exclude, lots[0]["quantity"])
            lots[0]["quantity"] -= excluded
            to_exclude -= excluded
            if lots[0]["quantity"] <= 1e-9:
                lots.pop(0)
    elif open_quantity + 1e-6 < target_quantity:
        missing_quantity = target_quantity - open_quantity
        if adjustment_dates:
            lots.append({
                "date": min(adjustment_dates),
                "quantity": missing_quantity,
                "price": 0.0,
                "adjustment": True,
            })
        else:
            warnings.append(
                f"{position['symbol'] or position['isin']}: ledger covers {open_quantity:g} of "
                f"{target_quantity:g} current units; missing units use the holding average cost from today."
            )
            lots.append({
                "date": date.today().isoformat(),
                "quantity": missing_quantity,
                "price": float(position.get("average_price") or 0),
                "adjustment": True,
            })

    if lots and float(position.get("average_price") or 0) > 0:
        target_cost = target_quantity * float(position["average_price"])
        actual_cost = sum(lot["quantity"] * lot["price"] for lot in lots)
        adjustment_quantity = sum(lot["quantity"] for lot in lots if lot.get("adjustment"))
        trade_cost = sum(lot["quantity"] * lot["price"] for lot in lots if not lot.get("adjustment"))
        if adjustment_quantity and target_cost >= trade_cost:
            adjustment_price = (target_cost - trade_cost) / adjustment_quantity
            for lot in lots:
                if lot.get("adjustment"):
                    lot["price"] = adjustment_price
        elif actual_cost <= 0 and target_cost > 0:
            for lot in lots:
                lot["price"] = target_cost / target_quantity
        elif actual_cost > 0 and abs(actual_cost - target_cost) > max(0.01, target_cost * 0.0001):
            scale = target_cost / actual_cost
            for lot in lots:
                lot["price"] *= scale

    warning = " ".join(warnings) if warnings else None
    return lots, warning


def _sync_is_fresh(conn: sqlite3.Connection, instrument_key: str, provider: str, today: date, start: date) -> bool:
    row = conn.execute(
        "SELECT coverage_start, coverage_end, synced_at FROM price_history_sync WHERE instrument_key = ? AND provider = ?",
        (instrument_key, provider),
    ).fetchone()
    if not row:
        return False
    coverage_start = _date_value(row["coverage_start"])
    coverage_end = _date_value(row["coverage_end"])
    synced_date = _date_value(row["synced_at"])
    return (
        coverage_start is not None
        and coverage_end is not None
        and coverage_start <= start
        and coverage_end >= today
        and synced_date == today
    )


def _store_prices(
    conn: sqlite3.Connection,
    position: Dict[str, Any],
    provider: str,
    prices: List[Tuple[str, float]],
    start: date,
    end: date,
) -> None:
    cached_at = datetime.now().astimezone().isoformat()
    existing = conn.execute(
        "SELECT coverage_start, coverage_end FROM price_history_sync WHERE instrument_key = ? AND provider = ?",
        (position["instrument_key"], provider),
    ).fetchone()
    if existing:
        existing_start = _date_value(existing["coverage_start"])
        existing_end = _date_value(existing["coverage_end"])
        if (
            existing_start
            and existing_end
            and start <= existing_end + timedelta(days=1)
            and end + timedelta(days=1) >= existing_start
        ):
            start = min(start, existing_start)
            end = max(end, existing_end)
    conn.executemany(
        """
        INSERT INTO historical_prices (instrument_key, asset_class, price_date, close, provider, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(instrument_key, price_date, provider)
        DO UPDATE SET close = excluded.close, cached_at = excluded.cached_at
        """,
        [(position["instrument_key"], position["asset_class"], day, close, provider, cached_at) for day, close in prices],
    )
    conn.execute(
        """
        INSERT INTO price_history_sync (instrument_key, provider, coverage_start, coverage_end, synced_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(instrument_key, provider)
        DO UPDATE SET
            coverage_start = CASE
                WHEN excluded.coverage_start <= date(price_history_sync.coverage_end, '+1 day')
                     AND excluded.coverage_end >= date(price_history_sync.coverage_start, '-1 day')
                THEN min(price_history_sync.coverage_start, excluded.coverage_start)
                ELSE price_history_sync.coverage_start
            END,
            coverage_end = CASE
                WHEN excluded.coverage_start <= date(price_history_sync.coverage_end, '+1 day')
                     AND excluded.coverage_end >= date(price_history_sync.coverage_start, '-1 day')
                THEN max(price_history_sync.coverage_end, excluded.coverage_end)
                ELSE price_history_sync.coverage_end
            END,
            synced_at = excluded.synced_at
        """,
        (position["instrument_key"], provider, start.isoformat(), end.isoformat(), cached_at),
    )


def _cached_prices(conn: sqlite3.Connection, position: Dict[str, Any], provider: str) -> Dict[date, float]:
    rows = conn.execute(
        """
        SELECT price_date, close FROM historical_prices
        WHERE instrument_key = ? AND provider = ? ORDER BY price_date
        """,
        (position["instrument_key"], provider),
    ).fetchall()
    return {
        parsed: float(row["close"])
        for row in rows
        if (parsed := _date_value(row["price_date"])) is not None
    }


def _jugaad_nse_prices(position: Dict[str, Any], start: date, end: date) -> List[Tuple[str, float]]:
    symbol = _clean(position.get("symbol")).upper()
    if not symbol:
        raise RuntimeError("No NSE trading symbol is available for this holding.")
    nse_symbol = NSE_SYMBOL_ALIASES.get(symbol, symbol)

    try:
        from jugaad_data.nse import NSEHistory

        records = NSEHistory().stock_raw(
            nse_symbol,
            start,
            end,
            series=(
                "EQ"
                if position.get("asset_class") == "equity"
                else _clean(position.get("nse_series")) or "EQ"
            ),
        )
    except (ImportError, KeyError, requests.RequestException, TypeError, ValueError) as exc:
        raise RuntimeError(f"NSE history lookup failed for {symbol}: {exc}") from exc

    if not isinstance(records, list):
        raise RuntimeError(f"NSE returned an invalid historical price response for {symbol}.")

    prices = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if (
            position.get("asset_class") == "equity"
            and _clean(record.get("CH_SERIES")).upper() != "EQ"
        ):
            continue
        day = _date_value(record.get("mTIMESTAMP"))
        if not day:
            timestamp = _clean(record.get("CH_TIMESTAMP"))
            try:
                day = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(
                    timezone(timedelta(hours=5, minutes=30))
                ).date()
            except ValueError:
                continue
        try:
            close = float(record.get("CH_CLOSING_PRICE") or 0)
        except (TypeError, ValueError):
            continue
        if start <= day <= end and close > 0:
            prices.append((day.isoformat(), close))
    if not prices:
        raise RuntimeError(f"NSE returned no historical prices for {symbol} (queried {nse_symbol}).")
    return prices


def _mf_scheme_data(position: Dict[str, Any]) -> List[Tuple[str, float]]:
    name = position["name"]
    expected_isin = _clean(position.get("isin")).upper()
    lookup = expected_isin or _clean(position.get("symbol")) or name
    if not lookup:
        raise RuntimeError("Fund name is missing; the scheme cannot be resolved.")

    def find_scheme(candidates):
        for candidate in candidates[:20]:
            if not isinstance(candidate, dict) or not candidate.get("schemeCode"):
                continue
            detail = requests.get(f"{MFAPI_BASE_URL}/mf/{candidate['schemeCode']}", timeout=15)
            detail.raise_for_status()
            scheme_data = detail.json()
            metadata = scheme_data.get("meta", {}) if isinstance(scheme_data, dict) else {}
            scheme_isins = {
                _clean(metadata.get("isin_growth")).upper(),
                _clean(metadata.get("isin_div_reinvestment")).upper(),
            }
            if expected_isin and expected_isin in scheme_isins:
                return candidate["schemeCode"], scheme_data
            if not expected_isin and name and _normalized_name(candidate.get("schemeName")) == _normalized_name(name):
                return candidate["schemeCode"], scheme_data
        return None, None

    search = requests.get(f"{MFAPI_BASE_URL}/mf/search", params={"q": lookup}, timeout=15)
    search.raise_for_status()
    candidates = search.json()
    if not isinstance(candidates, list):
        raise RuntimeError("MFAPI returned an invalid scheme search response.")

    selected_code, payload = find_scheme(candidates)
    if not selected_code and name and lookup != name:
        search = requests.get(f"{MFAPI_BASE_URL}/mf/search", params={"q": name}, timeout=15)
        search.raise_for_status()
        candidates = search.json()
        if not isinstance(candidates, list):
            raise RuntimeError("MFAPI returned an invalid scheme search response.")
        selected_code, payload = find_scheme(candidates)
    if not selected_code:
        raise RuntimeError("MFAPI could not match a scheme to the holding's ISIN.")

    records = payload.get("data", []) if isinstance(payload, dict) else []
    prices = []
    for record in records:
        if not isinstance(record, dict):
            continue
        day = _date_value(record.get("date"))
        try:
            nav = float(record.get("nav") or 0)
        except (TypeError, ValueError):
            continue
        if day and nav > 0:
            prices.append((day.isoformat(), nav))
    if not prices:
        raise RuntimeError("MFAPI returned no historical NAV values for this scheme.")
    return prices


def _price_provider(position: Dict[str, Any]) -> str:
    if position["asset_class"] == "mf":
        return "mfapi"
    if position["asset_class"] == "equity":
        return "jugaad-nse-eq-only"
    return "jugaad-nse"


def _get_or_fetch_prices(
    conn: sqlite3.Connection,
    position: Dict[str, Any],
    start: date,
    end: date,
) -> Tuple[Dict[date, float], str]:
    provider = _price_provider(position)
    cached = _cached_prices(conn, position, provider)
    sync_row = conn.execute(
        "SELECT coverage_start, coverage_end, synced_at FROM price_history_sync WHERE instrument_key = ? AND provider = ?",
        (position["instrument_key"], provider),
    ).fetchone()
    coverage_end = _date_value(sync_row["coverage_end"]) if sync_row else None
    synced_date = _date_value(sync_row["synced_at"]) if sync_row else None
    coverage_updated_today = synced_date == end and coverage_end is not None and coverage_end >= end
    earliest_cached_date = min(cached) if cached else None
    latest_cached_date = max(cached) if cached else None

    if _sync_is_fresh(conn, position["instrument_key"], provider, end, start):
        if cached:
            return cached, provider

    if provider == "mfapi":
        fetch_ranges = [(_period_start("5Y", end), end)]
    else:
        history_start = _period_start("5Y", end)
        fetch_ranges = []
        if not cached or earliest_cached_date is None or earliest_cached_date > history_start:
            fetch_ranges.append((history_start, end))
        elif not coverage_updated_today:
            refresh_start = (
                max(history_start, latest_cached_date - timedelta(days=7))
                if latest_cached_date
                else history_start
            )
            fetch_ranges.append((refresh_start, end))

    if not fetch_ranges:
        return cached, provider

    errors = []
    for fetch_start, fetch_end in fetch_ranges:
        try:
            raw_prices = (
                _mf_scheme_data(position)
                if provider == "mfapi"
                else _jugaad_nse_prices(position, fetch_start, fetch_end)
            )
            prices = [
                (day, close)
                for day, close in raw_prices
                if (parsed_day := _date_value(day)) is not None and fetch_start <= parsed_day <= fetch_end
            ]
            if not prices:
                raise RuntimeError(f"No prices were returned within {fetch_start} to {fetch_end}.")
            _store_prices(conn, position, provider, prices, fetch_start, fetch_end)
            conn.commit()
        except (ImportError, requests.RequestException, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
        cached = _cached_prices(conn, position, provider)

    if not cached:
        raise RuntimeError("; ".join(errors) or f"No prices available for {position['symbol']}.")
    return cached, provider


def _fetch_position_prices(
    position: Dict[str, Any],
    start: date,
    end: date,
    db_path: Optional[str | Path],
) -> Tuple[Dict[date, float], str]:
    conn = _connect(db_path)
    try:
        return _get_or_fetch_prices(conn, position, start, end)
    finally:
        conn.close()


def _points_for_lots(
    lots_by_key: Dict[str, List[Dict[str, Any]]],
    positions: List[Dict[str, Any]],
    prices_by_key: Dict[str, Dict[date, float]],
    start: date,
    end: date,
) -> List[Dict[str, Any]]:
    calendar_days = sorted(
        {
            day
            for price_map in prices_by_key.values()
            for day in price_map
            if start <= day <= end
        }
    )
    if end not in calendar_days and calendar_days:
        calendar_days.append(end)
    output = []
    sorted_prices = {
        key: (sorted(price_map), price_map)
        for key, price_map in prices_by_key.items()
    }
    for day in calendar_days:
        pnl = 0.0
        cost_basis = 0.0
        market_value = 0.0
        has_eligible_lot = False
        for position in positions:
            key = position["instrument_key"]
            price_entry = sorted_prices.get(key)
            if not price_entry:
                continue
            price_dates, price_map = price_entry
            price_index = bisect_right(price_dates, day) - 1
            if price_index < 0:
                continue
            price_date = price_dates[price_index]
            if day - price_date > timedelta(days=7):
                continue
            market_price = price_map[price_date]
            for lot in lots_by_key.get(key, []):
                purchase_date = date.fromisoformat(lot["date"])
                if purchase_date > day:
                    continue
                has_eligible_lot = True
                cost_basis += lot["quantity"] * lot["price"]
                market_value += lot["quantity"] * market_price
                pnl += lot["quantity"] * (market_price - lot["price"])
        if has_eligible_lot:
            output.append({
                "date": day.isoformat(),
                "pnl": round(pnl, 2),
                "pnl_pct": round((pnl / cost_basis) * 100, 4) if cost_basis else 0.0,
                "invested": round(cost_basis, 2),
                "current_value": round(market_value, 2),
            })
    return output


def _modified_dietz_return(
    points: List[Dict[str, Any]],
    lots_by_key: Dict[str, List[Dict[str, Any]]],
    positions: List[Dict[str, Any]],
) -> Optional[float]:
    if len(points) < 2:
        return None
    try:
        start_day = date.fromisoformat(points[0]["date"])
        end_day = date.fromisoformat(points[-1]["date"])
    except (KeyError, TypeError, ValueError):
        return None
    period_days = (end_day - start_day).days
    if period_days <= 0:
        return None

    net_contributions = 0.0
    weighted_contributions = 0.0
    for position in positions:
        for lot in lots_by_key.get(position["instrument_key"], []):
            try:
                purchase_day = date.fromisoformat(lot["date"])
                contribution = float(lot["quantity"]) * float(lot["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if start_day < purchase_day <= end_day and contribution > 0:
                net_contributions += contribution
                weighted_contributions += contribution * ((end_day - purchase_day).days / period_days)

    opening_value = float(points[0].get("current_value") or 0)
    ending_value = float(points[-1].get("current_value") or 0)
    denominator = opening_value + weighted_contributions
    if denominator <= 0:
        return None
    return round(
        ((ending_value - opening_value - net_contributions) / denominator) * 100,
        4,
    )


def _xirr(cashflows: List[Tuple[date, float]]) -> Optional[float]:
    if len(cashflows) < 2:
        return None
    has_positive = any(amount > 0 for _, amount in cashflows)
    has_negative = any(amount < 0 for _, amount in cashflows)
    if not has_positive or not has_negative:
        return None

    start_day = min(day for day, _ in cashflows)

    def xnpv(rate: float) -> float:
        return sum(
            amount / ((1 + rate) ** ((day - start_day).days / 365.0))
            for day, amount in cashflows
        )

    low = -0.9999
    high = 1.0
    while xnpv(high) > 0 and high < 10000.0:
        high = high * 2 + 1
    if xnpv(low) < 0 or xnpv(high) > 0:
        return None

    for _ in range(200):
        middle = (low + high) / 2
        if xnpv(middle) > 0:
            low = middle
        else:
            high = middle
    return round(((low + high) / 2) * 100, 4)


def _period_xirr(
    points: List[Dict[str, Any]],
    lots_by_key: Dict[str, List[Dict[str, Any]]],
    positions: List[Dict[str, Any]],
) -> Optional[float]:
    if len(points) < 2:
        return None
    try:
        start_day = date.fromisoformat(points[0]["date"])
        end_day = date.fromisoformat(points[-1]["date"])
    except (KeyError, TypeError, ValueError):
        return None
    if end_day <= start_day:
        return None

    opening_value = float(points[0].get("current_value") or 0)
    ending_value = float(points[-1].get("current_value") or 0)
    cashflows = []
    if opening_value > 0:
        cashflows.append((start_day, -opening_value))
    for position in positions:
        for lot in lots_by_key.get(position["instrument_key"], []):
            try:
                purchase_day = date.fromisoformat(lot["date"])
                contribution = float(lot["quantity"]) * float(lot["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if start_day < purchase_day <= end_day and contribution > 0:
                cashflows.append((purchase_day, -contribution))
    if ending_value > 0:
        cashflows.append((end_day, ending_value))
    return _xirr(cashflows)


def build_portfolio_history_breakdown(
    holdings: Any,
    mf_holdings: Any,
    selected_assets: Iterable[str],
    selected_date: str,
    db_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    day = _date_value(selected_date)
    if not day:
        raise ValueError(f"Invalid history date: {selected_date}.")
    today = date.today()
    if day > today:
        raise ValueError(f"History date cannot be after today: {selected_date}.")

    selected = {value.lower() for value in selected_assets if value}
    if not selected or "all" in selected:
        selected = set(ASSET_CLASSES)
    invalid = selected - ASSET_CLASSES
    if invalid:
        raise ValueError(f"Unsupported asset filters: {', '.join(sorted(invalid))}")

    positions = _position_map(holdings, mf_holdings, selected)
    if not positions:
        return {"date": day.isoformat(), "rows": [], "warnings": []}

    ensure_history_tables(db_path)
    conn = _connect(db_path)
    rows = []
    warnings = []
    try:
        for position in positions:
            lots, warning = _open_lots_for_position(conn, position)
            if warning:
                warnings.append(warning)
            if not lots:
                continue

            prices = _cached_prices(conn, position, _price_provider(position))
            if not prices:
                continue
            latest_price_date = max(prices)
            if today - latest_price_date > timedelta(days=7):
                continue

            if day == today:
                quantity = float(position["quantity"])
                invested = quantity * float(position.get("average_price", 0))
                price = float(position.get("current_price", 0))
                price_date = day
            else:
                price_dates = sorted(prices)
                price_index = bisect_right(price_dates, day) - 1
                if price_index < 0:
                    continue
                price_date = price_dates[price_index]
                if day - price_date > timedelta(days=7):
                    continue
                price = prices[price_date]
                active_lots = [
                    lot for lot in lots
                    if date.fromisoformat(lot["date"]) <= day
                ]
                quantity = sum(float(lot["quantity"]) for lot in active_lots)
                invested = sum(
                    float(lot["quantity"]) * float(lot["price"])
                    for lot in active_lots
                )
                if quantity <= 0:
                    continue

            current_value = quantity * price
            rows.append({
                "symbol": (
                    position["name"]
                    if position["asset_class"] == "mf"
                    else position["symbol"] or position["name"]
                ),
                "name": position["name"],
                "asset_class": position["asset_class"],
                "quantity": round(quantity, 6),
                "price": round(price, 4),
                "price_date": price_date.isoformat(),
                "invested": round(invested, 2),
                "current_value": round(current_value, 2),
                "pnl": round(current_value - invested, 2),
            })
    finally:
        conn.close()

    rows.sort(key=lambda row: row["symbol"].upper())
    return {"date": day.isoformat(), "rows": rows, "warnings": warnings}


def build_portfolio_history(
    holdings: Any,
    mf_holdings: Any,
    selected_assets: Iterable[str],
    period: str = "1M",
    db_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    normalized_period = period.upper()
    if normalized_period not in PERIOD_DAYS:
        raise ValueError(f"Unsupported period: {period}")
    selected = {value.lower() for value in selected_assets if value}
    if not selected or "all" in selected:
        selected = set(ASSET_CLASSES)
    invalid = selected - ASSET_CLASSES
    if invalid:
        raise ValueError(f"Unsupported asset filters: {', '.join(sorted(invalid))}")

    today = date.today()
    start = _period_start(normalized_period, today)
    positions = _position_map(holdings, mf_holdings, selected)
    if not positions:
        return {
            "period": normalized_period,
            "selected_assets": sorted(selected),
            "points": [],
            "period_return_pct": None,
            "period_xirr_pct": None,
            "warnings": [],
            "sources": [],
            "available_holdings": [],
        }

    ensure_history_tables(db_path)
    conn = _connect(db_path)
    lots_by_key: Dict[str, List[Dict[str, Any]]] = {}
    warnings: List[str] = []
    for position in positions:
        lots, warning = _open_lots_for_position(conn, position)
        lots_by_key[position["instrument_key"]] = lots
        if warning:
            warnings.append(warning)
        if not lots:
            warnings.append(
                f"{position['symbol']}: no current open purchase lots were found in the imported trade ledger; excluded from historical reconstruction."
            )
    chart_positions = [position for position in positions if lots_by_key.get(position["instrument_key"])]
    conn.close()

    prices_by_key: Dict[str, Dict[date, float]] = {}
    sources = set()
    if chart_positions:
        with ThreadPoolExecutor(max_workers=min(MAX_PRICE_FETCH_WORKERS, len(chart_positions))) as executor:
            futures = {
                executor.submit(_fetch_position_prices, position, start, today, db_path): position
                for position in chart_positions
            }
            for future in as_completed(futures):
                position = futures[future]
                provider = _price_provider(position)
                try:
                    prices, provider = future.result()
                    if not prices:
                        raise RuntimeError("No historical prices are cached for this holding.")
                    latest_price_date = max(prices)
                    if today - latest_price_date > timedelta(days=7):
                        warnings.append(
                            f"{position['symbol'] or position['isin']}: latest cached price is from {latest_price_date.isoformat()}; stale history was excluded."
                        )
                        continue
                    prices_by_key[position["instrument_key"]] = prices
                    source_labels = {
                        "mfapi": "MFAPI NAV",
                        "jugaad-nse": "NSE historical prices (jugaad-data)",
                        "jugaad-nse-eq-only": "NSE historical prices (jugaad-data)",
                    }
                    sources.add(source_labels[provider])
                except (ImportError, requests.RequestException, ValueError, RuntimeError) as exc:
                    warnings.append(f"{position['symbol'] or position['isin']}: historical price unavailable ({exc})")

    priced_positions = [position for position in chart_positions if position["instrument_key"] in prices_by_key]
    points = _points_for_lots(lots_by_key, priced_positions, prices_by_key, start, today)
    if priced_positions:
        current_cost = sum(
            position["quantity"] * position.get("average_price", 0)
            for position in priced_positions
        )
        current_pnl = sum(
            position["quantity"] * (position.get("current_price", 0) - position.get("average_price", 0))
            for position in priced_positions
        )
        latest_point = {
            "date": today.isoformat(),
            "pnl": round(current_pnl, 2),
            "pnl_pct": round((current_pnl / current_cost) * 100, 4) if current_cost else 0.0,
            "invested": round(current_cost, 2),
            "current_value": round(current_cost + current_pnl, 2),
        }
        if points and points[-1]["date"] == latest_point["date"]:
            points[-1] = latest_point
        else:
            points.append(latest_point)

    period_return_pct = _modified_dietz_return(points, lots_by_key, priced_positions)
    period_xirr_pct = _period_xirr(points, lots_by_key, priced_positions)
    return {
        "period": normalized_period,
        "selected_assets": sorted(selected),
        "points": points,
        "period_return_pct": period_return_pct,
        "period_xirr_pct": period_xirr_pct,
        "warnings": warnings,
        "sources": sorted(sources),
        "available_holdings": [
            {
                "symbol": position["symbol"],
                "name": position["name"],
                "asset_class": position["asset_class"],
            }
            for position in priced_positions
        ],
    }
