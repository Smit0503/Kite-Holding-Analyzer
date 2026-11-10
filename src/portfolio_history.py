import calendar
from bisect import bisect_right
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
INDIAN_API_BASE_URL = "https://stock.indianapi.in"
MFAPI_BASE_URL = "https://api.mfapi.in"
INDIAN_API_PERIODS = {
    "5Y": "5yr",
}


def _connect(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
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
        ),
        "",
    )
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
        DO UPDATE SET coverage_start = excluded.coverage_start,
                      coverage_end = excluded.coverage_end,
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


def _indian_api_prices(position: Dict[str, Any], period: str, api_key: str) -> List[Tuple[str, float]]:
    identifier = _clean(position.get("symbol") or position.get("isin")).upper()
    if not identifier:
        raise RuntimeError("No trading symbol or ISIN is available for this holding.")
    if not api_key:
        raise RuntimeError("INDIAN_API_KEY is not configured.")

    response = requests.get(
        f"{INDIAN_API_BASE_URL}/historical_data",
        headers={"X-Api-Key": api_key},
        params={"stock_name": identifier, "period": INDIAN_API_PERIODS[period], "filter": "price"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    datasets = payload.get("datasets", []) if isinstance(payload, dict) else []
    if not isinstance(datasets, list):
        raise RuntimeError(f"IndianAPI returned an invalid price response for {identifier}.")
    price_dataset = next(
        (
            dataset
            for dataset in datasets
            if isinstance(dataset, dict) and _clean(dataset.get("metric")).lower() == "price"
        ),
        None,
    )
    if not price_dataset:
        raise RuntimeError(f"IndianAPI returned no price series for {identifier}.")
    values = price_dataset.get("values", [])
    if not isinstance(values, list):
        raise RuntimeError(f"IndianAPI returned an invalid price series for {identifier}.")

    prices = []
    for point in values:
        if not isinstance(point, list) or len(point) < 2:
            continue
        day = _date_value(point[0])
        try:
            close = float(point[1])
        except (TypeError, ValueError):
            continue
        if day and close > 0:
            prices.append((day.isoformat(), close))
    if not prices:
        raise RuntimeError(f"IndianAPI returned no daily prices for {identifier}.")
    return prices


def _jugaad_nse_prices(position: Dict[str, Any], start: date, end: date) -> List[Tuple[str, float]]:
    symbol = _clean(position.get("symbol")).upper()
    if not symbol:
        raise RuntimeError("No NSE trading symbol is available for this holding.")
    if _clean(position.get("exchange") or "NSE").upper() != "NSE":
        raise RuntimeError("NSE historical data is only available for NSE holdings.")

    try:
        from jugaad_data.nse import stock_raw

        records = stock_raw(
            symbol,
            start,
            end,
            series=_clean(position.get("nse_series")) or "EQ",
        )
    except (ImportError, KeyError, requests.RequestException, TypeError, ValueError) as exc:
        raise RuntimeError(f"NSE history lookup failed for {symbol}: {exc}") from exc

    if not isinstance(records, list):
        raise RuntimeError(f"NSE returned an invalid historical price response for {symbol}.")

    prices = []
    for record in records:
        if not isinstance(record, dict):
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
        raise RuntimeError(f"NSE returned no historical prices for {symbol}.")
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
    return "mfapi" if position["asset_class"] == "mf" else "indianapi"


def _get_or_fetch_prices(
    conn: sqlite3.Connection,
    position: Dict[str, Any],
    period: str,
    start: date,
    end: date,
    indian_api_key: str,
) -> Tuple[Dict[date, float], str]:
    primary_provider = _price_provider(position)
    history_start = _period_start("5Y", end)
    providers = [(primary_provider, history_start)]
    if primary_provider != "mfapi":
        nse_provider = ("jugaad-nse", start)
        if position["asset_class"] in {"debt", "metal"} or _cached_prices(
            conn, position, "jugaad-nse"
        ):
            providers.insert(0, nse_provider)
        else:
            providers.append(nse_provider)

    errors = []
    for provider, fetch_start in providers:
        if _sync_is_fresh(conn, position["instrument_key"], provider, end, fetch_start):
            cached = _cached_prices(conn, position, provider)
            if cached:
                return cached, provider
            errors.append(f"{provider} returned no prices on its latest lookup")
            continue

        try:
            if provider == "mfapi":
                raw_prices = _mf_scheme_data(position)
            elif provider == "indianapi":
                raw_prices = _indian_api_prices(position, "5Y", indian_api_key)
            else:
                raw_prices = _jugaad_nse_prices(position, fetch_start, end)
            prices = [
                (day, close)
                for day, close in raw_prices
                if (parsed_day := _date_value(day)) is not None and fetch_start <= parsed_day <= end
            ]
            if not prices:
                raise RuntimeError(f"No prices were returned within the requested history.")
            _store_prices(conn, position, provider, prices, fetch_start, end)
            conn.commit()
            return _cached_prices(conn, position, provider), provider
        except (ImportError, requests.RequestException, ValueError, RuntimeError) as exc:
            _store_prices(conn, position, provider, [], fetch_start, end)
            conn.commit()
            errors.append(f"{provider}: {exc}")

    raise RuntimeError("; ".join(errors) or f"No prices available for {position['symbol']}.")


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
                pnl += lot["quantity"] * (market_price - lot["price"])
        if has_eligible_lot:
            output.append({
                "date": day.isoformat(),
                "pnl": round(pnl, 2),
                "pnl_pct": round((pnl / cost_basis) * 100, 4) if cost_basis else 0.0,
            })
    return output


def build_portfolio_history(
    holdings: Any,
    mf_holdings: Any,
    selected_assets: Iterable[str],
    period: str = "1M",
    indian_api_key: str = "",
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

    prices_by_key: Dict[str, Dict[date, float]] = {}
    sources = set()
    for position in chart_positions:
        provider = _price_provider(position)
        try:
            prices, provider = _get_or_fetch_prices(
                conn, position, normalized_period, start, today, indian_api_key,
            )
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
                "indianapi": "IndianAPI historical prices",
                "jugaad-nse": "NSE historical prices (jugaad-data)",
            }
            sources.add(source_labels[provider])
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            warnings.append(f"{position['symbol'] or position['isin']}: historical price unavailable ({exc})")
    conn.close()

    priced_positions = [position for position in chart_positions if position["instrument_key"] in prices_by_key]
    points = _points_for_lots(lots_by_key, priced_positions, prices_by_key, start, today)
    if positions:
        current_cost = sum(
            position["quantity"] * position.get("average_price", 0)
            for position in positions
        )
        current_pnl = sum(
            position["quantity"] * (position.get("current_price", 0) - position.get("average_price", 0))
            for position in positions
        )
        latest_point = {
            "date": today.isoformat(),
            "pnl": round(current_pnl, 2),
            "pnl_pct": round((current_pnl / current_cost) * 100, 4) if current_cost else 0.0,
        }
        if points and points[-1]["date"] == latest_point["date"]:
            points[-1] = latest_point
        else:
            points.append(latest_point)

    return {
        "period": normalized_period,
        "selected_assets": sorted(selected),
        "points": points,
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
