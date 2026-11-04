import math
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List

import requests


def calculate_cagr(start_value: float, end_value: float, years: float) -> float:
    """Calculate compound annual growth rate."""
    if start_value <= 0 or years <= 0:
        return 0.0
    return ((end_value / start_value) ** (1 / years) - 1) * 100


def _period_to_years(period: str) -> float:
    normalized = (period or "").upper().strip()
    if normalized in {"1M", "1MONTH", "1MTH"}:
        return 1 / 12
    if normalized in {"3M", "3MONTH", "3MTH"}:
        return 3 / 12
    if normalized in {"6M", "6MONTH", "6MTH"}:
        return 6 / 12
    if normalized in {"1Y", "12M", "12MONTH", "1YEAR"}:
        return 1.0
    if normalized in {"2Y", "24M"}:
        return 2.0
    if normalized in {"3Y", "36M"}:
        return 3.0
    return 1.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _extract_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
    return []


def _symbol_key(item: Dict[str, Any]) -> str:
    return str(item.get("tradingsymbol") or item.get("symbol") or "").upper().strip()


def _looks_like_debt_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").upper().strip()
    if not normalized:
        return False

    if re.search(r"(?:^|[^A-Z])(GILT|SDL|NCD|BOND|DEBT|TBILL|T-BILL|TREASURY|NOTE|DEBENTURE)", normalized):
        return True

    compact = re.sub(r"[^A-Z0-9]", "", normalized)
    return bool(re.match(r"^\d+[A-Z]{3,}\d{2,}[A-Z0-9-]*$", compact))


def _looks_like_metal_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").upper().strip()
    if not normalized:
        return False
    compact = re.sub(r"[^A-Z0-9]", "", normalized)
    metal_tokens = ["SGB", "GOLD", "SILVER", "METAL", "BULLION", "PRECIOUS", "ETF"]
    if any(token in normalized for token in metal_tokens):
        return True
    return bool(re.match(r"^(?:SGB|GOLD|SILVER|METAL)[A-Z0-9-]*$", compact))


def classify_asset(item: Dict[str, Any]) -> str:
    text = " ".join(
        [
            str(item.get("tradingsymbol") or ""),
            str(item.get("product") or ""),
            str(item.get("exchange") or ""),
            str(item.get("instrument_type") or ""),
            str(item.get("segment") or ""),
            str(item.get("symbol") or ""),
            str(item.get("fund_name") or ""),
            str(item.get("name") or ""),
            str(item.get("isin") or ""),
        ]
    ).upper()

    raw = str(item.get("instrument_type") or item.get("exchange") or item.get("segment") or "").upper()
    symbol = str(item.get("tradingsymbol") or item.get("symbol") or "")

    if item.get("is_mf") is True or "MF" in raw or "MUTUALFUND" in raw or "MUTFUND" in raw:
        return "mf"

    if any(token in text for token in ["SGB", "GOLD", "SILVER", "METAL", "PRECIOUS", "BULLION", "GOLD ETF", "SILVER ETF"]) or _looks_like_metal_symbol(symbol):
        return "metal"
    if any(token in text for token in ["MUTUAL", "MF", "MUTFUND", "FUND", "SCHEME", "NFO"]) or "MUTUALFUND" in text:
        return "mf"
    if any(token in text for token in ["DEBT", "BOND", "NCD", "GILT", "SDL", "DEBENTURE", "TREASURY", "T-BILL", "TBILL", "NOTE"]) or _looks_like_debt_symbol(symbol):
        return "debt"
    if raw in {"MF", "MUTUALFUND", "MUTFUND", "NFO"}:
        return "mf"
    return "equity"


def _portfolio_value_from_holdings(holdings: Dict[str, Any]) -> float:
    items = _extract_items(holdings)
    total = 0.0
    for item in items:
        qty = _safe_float(item.get("quantity"))
        if qty <= 0:
            qty = _safe_float(item.get("t1_quantity"))
        ltp = _safe_float(item.get("last_price")) or _safe_float(item.get("ltp")) or _safe_float(item.get("close_price"))
        if qty and ltp:
            total += qty * ltp
        else:
            avg = _safe_float(item.get("average_price"))
            total += qty * avg
    return total


def _coerce_trade_date(value: Any) -> datetime:
    if not value:
        return datetime.min
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min
    return datetime.min


def _trade_type(trade: Dict[str, Any]) -> str:
    trade_type = str(
        trade.get("trade_type")
        or trade.get("transaction_type")
        or trade.get("type")
        or trade.get("order_type")
        or ""
    ).upper()
    if not trade_type:
        qty = _trade_quantity(trade)
        return "BUY" if qty > 0 else "SELL"
    if trade_type in {"BUY", "SELL"}:
        return trade_type
    if trade_type in {"BUY_ORDER", "LONG", "DEBIT"}:
        return "BUY"
    if trade_type in {"SELL_ORDER", "SHORT", "CREDIT"}:
        return "SELL"
    return trade_type


def _trade_symbol(trade: Dict[str, Any]) -> str:
    symbol = trade.get("tradingsymbol") or trade.get("symbol") or trade.get("instrument_name") or trade.get("name") or ""
    return str(symbol).upper().strip()


def _trade_quantity(trade: Dict[str, Any]) -> float:
    qty = _safe_float(trade.get("quantity"))
    if qty == 0:
        qty = _safe_float(trade.get("filled_quantity"))
    if qty == 0:
        qty = _safe_float(trade.get("pending_quantity"))
    return abs(qty)


def _trade_price(trade: Dict[str, Any]) -> float:
    return (
        _safe_float(trade.get("average_price"))
        or _safe_float(trade.get("price"))
        or _safe_float(trade.get("avg_price"))
        or _safe_float(trade.get("last_price"))
        or _safe_float(trade.get("buy_average_price"))
        or _safe_float(trade.get("sell_average_price"))
    )


def fifo_purchase_date_for_holding(holding: Dict[str, Any], trades: Iterable[Dict[str, Any]]) -> str:
    symbol = _symbol_key(holding)
    current_qty = _safe_float(holding.get("quantity"))
    if current_qty <= 0:
        current_qty = _safe_float(holding.get("t1_quantity"))
    if current_qty <= 0 or not symbol:
        return "N/A"

    buy_lots = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        if _trade_symbol(trade) != symbol:
            continue
        if _trade_type(trade) != "BUY":
            continue
        qty = _trade_quantity(trade)
        if qty <= 0:
            continue
        buy_lots.append({
            "date": _coerce_trade_date(trade.get("order_timestamp") or trade.get("timestamp") or trade.get("filled_at") or trade.get("trade_date")),
            "quantity": qty,
            "price": _trade_price(trade),
        })

    if not buy_lots:
        return "N/A"

    buy_lots.sort(key=lambda item: item["date"])
    remaining = current_qty
    for lot in buy_lots:
        if remaining <= 0:
            break
        if lot["quantity"] >= remaining:
            return lot["date"].strftime("%Y-%m-%d")
        remaining -= lot["quantity"]
    return buy_lots[0]["date"].strftime("%Y-%m-%d")


def fifo_realized_pnl(trades: Iterable[Dict[str, Any]], from_date: str = "", to_date: str = "") -> Dict[str, Any]:
    trade_rows = []
    lots_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    total_pnl = 0.0

    filtered_trades = []
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        trade_date = _coerce_trade_date(trade.get("order_timestamp") or trade.get("timestamp") or trade.get("filled_at") or trade.get("trade_date"))
        if trade_date == datetime.min:
            continue
        if from_date and trade_date.date() < datetime.strptime(from_date, "%Y-%m-%d").date():
            continue
        if to_date and trade_date.date() > datetime.strptime(to_date, "%Y-%m-%d").date():
            continue
        filtered_trades.append(trade)

    for trade in sorted(filtered_trades, key=lambda item: _coerce_trade_date(item.get("order_timestamp") or item.get("timestamp") or item.get("filled_at") or item.get("trade_date"))):
        symbol = _trade_symbol(trade)
        if not symbol:
            continue

        trade_type = _trade_type(trade)
        qty = _trade_quantity(trade)
        if qty <= 0:
            continue

        if trade_type == "BUY":
            lots_by_symbol[symbol].append({
                "quantity": qty,
                "price": _trade_price(trade),
                "date": _coerce_trade_date(trade.get("order_timestamp") or trade.get("timestamp") or trade.get("filled_at") or trade.get("trade_date")),
            })
            continue

        if trade_type != "SELL":
            continue

        remaining = qty
        while remaining > 0 and lots_by_symbol.get(symbol):
            lot = lots_by_symbol[symbol][0]
            matched = min(remaining, lot["quantity"])
            pnl = ( _trade_price(trade) - lot["price"] ) * matched
            total_pnl += pnl
            trade_rows.append({
                "symbol": symbol,
                "date": _coerce_trade_date(trade.get("order_timestamp") or trade.get("timestamp") or trade.get("filled_at") or trade.get("trade_date")).strftime("%Y-%m-%d"),
                "purchase_date": lot["date"].strftime("%Y-%m-%d"),
                "quantity": matched,
                "sell_price": _trade_price(trade),
                "buy_price": lot["price"],
                "pnl": pnl,
            })
            remaining -= matched
            lot["quantity"] -= matched
            if lot["quantity"] <= 0:
                lots_by_symbol[symbol].pop(0)

    return {
        "rows": trade_rows,
        "total_pnl": total_pnl,
    }


def _historical_price_for_instrument(instrument_token: Any, access_token: str, api_key: str, start_date: datetime) -> float:
    token = str(instrument_token)
    end_date = datetime.utcnow()
    url = "https://api.kite.trade/instruments/historical/{token}/day"
    params = {
        "from": start_date.strftime("%Y-%m-%d"),
        "to": end_date.strftime("%Y-%m-%d"),
        "continuous": 0,
    }
    headers = {"Authorization": f"token {api_key}:{access_token}"}
    try:
        response = requests.get(url.format(token=token), headers=headers, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        candles = payload.get("data", {}).get("candles") if isinstance(payload, dict) else []
        if not candles:
            return 0.0
        for candle in candles:
            if len(candle) >= 6:
                return float(candle[4])
        return float(candles[0][4])
    except Exception:
        return 0.0


def calculate_metrics(holdings: Dict[str, Any], period: str, access_token: str = "", api_key: str = "") -> Dict[str, Any]:
    """Realistic portfolio CAGR using historical price history for each held instrument."""
    items = _extract_items(holdings)
    if not items:
        return {"period": period, "current_value": 0.0, "cagr_percentage": 0.0, "total_return_percentage": 0.0, "volatility_annualized": 0.0}

    years = _period_to_years(period)
    current_value = 0.0
    start_value = 0.0
    start_date = datetime.utcnow() - timedelta(days=365 * max(1, int(math.ceil(years * 12 / 1.0))))

    if period.upper() in {"1M"}:
        start_date = datetime.utcnow() - timedelta(days=30)
    elif period.upper() in {"3M"}:
        start_date = datetime.utcnow() - timedelta(days=90)
    elif period.upper() in {"6M"}:
        start_date = datetime.utcnow() - timedelta(days=180)
    elif period.upper() in {"12M", "1Y"}:
        start_date = datetime.utcnow() - timedelta(days=365)

    for item in items:
        qty = _safe_float(item.get("quantity"))
        if qty <= 0:
            qty = _safe_float(item.get("t1_quantity"))
        if qty <= 0:
            continue

        ltp = _safe_float(item.get("last_price")) or _safe_float(item.get("ltp")) or _safe_float(item.get("close_price"))
        avg = _safe_float(item.get("average_price"))
        current_value += qty * (ltp or avg or 0)

        base_price = _safe_float(item.get("average_price"), 0.0)
        instrument_token = item.get("instrument_token")
        if access_token and api_key and instrument_token:
            historical_price = _historical_price_for_instrument(instrument_token, access_token, api_key, start_date)
            if historical_price > 0:
                base_price = historical_price
        start_value += qty * base_price

    if start_value <= 0 or current_value <= 0:
        return {
            "period": period,
            "current_value": round(current_value, 2),
            "cagr_percentage": 0.0,
            "total_return_percentage": 0.0,
            "volatility_annualized": 0.0,
        }

    cagr = calculate_cagr(start_value, current_value, years)
    total_return = ((current_value / start_value) - 1) * 100
    return {
        "period": period,
        "current_value": round(current_value, 2),
        "start_value": round(start_value, 2),
        "cagr_percentage": round(cagr, 2),
        "total_return_percentage": round(total_return, 2),
        "volatility_annualized": 0.0,
    }

