import argparse
import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "session.db"


def connect_db(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_trade_tables(db_path: Optional[str | Path] = None) -> None:
    conn = connect_db(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT,
            asset_class TEXT,
            symbol TEXT,
            isin TEXT,
            trade_date TEXT,
            exchange TEXT,
            segment TEXT,
            series TEXT,
            trade_type TEXT,
            auction TEXT,
            quantity REAL,
            price REAL,
            trade_id TEXT,
            order_id TEXT,
            order_execution_time TEXT,
            imported_at TEXT
        )
        """
    )
    adjustment_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(trade_adjustments)").fetchall()
    }
    if "isin" not in adjustment_columns:
        conn.execute("ALTER TABLE trade_adjustments ADD COLUMN isin TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            asset_class TEXT,
            reason TEXT,
            quantity REAL,
            amount REAL,
            purchase_date TEXT,
            notes TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _read_csv_rows(csv_path: str | Path) -> List[Dict[str, Any]]:
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader if row]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return default


def _asset_class_from_file(csv_path: str | Path, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit.lower()

    lower_name = str(csv_path).lower()
    if "mf" in lower_name:
        return "mf"
    if "debt" in lower_name or "bond" in lower_name:
        return "debt"
    if "metal" in lower_name or "gold" in lower_name or "silver" in lower_name:
        return "metal"
    return "equity"


def _normalize_trade_type(value: Any) -> str:
    normal = _clean(value).lower()
    if normal in {"buy", "b", "purchase"}:
        return "BUY"
    if normal in {"sell", "s", "sale"}:
        return "SELL"
    return "BUY" if normal else "BUY"


def append_trade_rows_from_csv(csv_path: str | Path, db_path: Optional[str | Path] = None, asset_class: Optional[str] = None) -> int:
    ensure_trade_tables(db_path)
    conn = connect_db(db_path)
    imported_count = 0
    resolved_asset_class = asset_class or _asset_class_from_file(csv_path)

    for row in _read_csv_rows(csv_path):
        symbol = _clean(row.get("symbol") or row.get("tradingsymbol") or row.get("instrument_name") or row.get("name"))
        if not symbol:
            continue

        trade_type = _normalize_trade_type(row.get("trade_type") or row.get("transaction_type") or row.get("type"))
        quantity = _parse_float(row.get("quantity") or row.get("qty") or row.get("filled_quantity") or row.get("pending_quantity"))
        if quantity == 0:
            continue

        price = _parse_float(row.get("price") or row.get("average_price") or row.get("avg_price") or row.get("buy_average_price") or row.get("sell_average_price"))
        trade_date = _clean(row.get("trade_date") or row.get("filled_at") or row.get("timestamp") or row.get("order_timestamp"))
        if not trade_date:
            continue

        conn.execute(
            """
            INSERT INTO trade_imports (
                source_file,
                asset_class,
                symbol,
                isin,
                trade_date,
                exchange,
                segment,
                series,
                trade_type,
                auction,
                quantity,
                price,
                trade_id,
                order_id,
                order_execution_time,
                imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(csv_path),
                resolved_asset_class,
                symbol.upper(),
                _clean(row.get("isin")),
                trade_date,
                _clean(row.get("exchange") or row.get("segment") or row.get("exchange_name")),
                _clean(row.get("segment") or row.get("segment_name")),
                _clean(row.get("series")),
                trade_type,
                _clean(row.get("auction")),
                abs(quantity),
                price,
                _clean(row.get("trade_id") or row.get("tradeid") or row.get("id")),
                _clean(row.get("order_id") or row.get("orderid")),
                _clean(row.get("order_execution_time") or row.get("order_executed_time") or row.get("execution_time")),
                datetime.utcnow().isoformat(),
            ),
        )
        imported_count += 1

    conn.commit()
    conn.close()
    return imported_count


def append_tradebook_directory(directory: str | Path, db_path: Optional[str | Path] = None) -> Dict[str, int]:
    folder = Path(directory)
    result: Dict[str, int] = {}

    if not folder.exists():
        return result

    for csv_file in sorted(folder.glob("*.csv")):
        asset_class = _asset_class_from_file(csv_file)
        result[str(csv_file.name)] = append_trade_rows_from_csv(csv_file, db_path=db_path, asset_class=asset_class)

    return result


def add_manual_adjustment(
    symbol: str,
    asset_class: str,
    quantity: float,
    amount: float,
    purchase_date: str,
    reason: str = "",
    notes: str = "",
    isin: str = "",
    db_path: Optional[str | Path] = None,
) -> int:
    ensure_trade_tables(db_path)
    conn = connect_db(db_path)
    cursor = conn.execute(
        """
        INSERT INTO trade_adjustments (symbol, asset_class, reason, quantity, amount, purchase_date, notes, isin, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(symbol).upper().strip(),
            str(asset_class).lower().strip() or "equity",
            reason,
            float(quantity or 0),
            float(amount or 0),
            purchase_date,
            notes,
            str(isin or "").upper().strip(),
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return int(cursor.lastrowid)


def list_trade_adjustments(db_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    conn = connect_db(db_path)
    rows = conn.execute(
        "SELECT * FROM trade_adjustments ORDER BY purchase_date DESC, created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def list_trade_imports(db_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    conn = connect_db(db_path)
    rows = conn.execute(
        "SELECT * FROM trade_imports ORDER BY trade_date ASC, id ASC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Import Zerodha tradebook CSV exports into the app database.")
    parser.add_argument("--file", type=str, help="Single CSV file to import")
    parser.add_argument("--folder", type=str, help="Folder containing tradebook CSV files to import")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH), help="SQLite database path")
    args = parser.parse_args()

    ensure_trade_tables(args.db)

    if args.file:
        count = append_trade_rows_from_csv(args.file, db_path=args.db)
        print(f"Imported {count} rows from {args.file}")
        return

    if args.folder:
        result = append_tradebook_directory(args.folder, db_path=args.db)
        total = sum(result.values())
        print(f"Imported {total} rows across {len(result)} files")
        return

    raise SystemExit("Provide either --file or --folder.")


if __name__ == "__main__":
    _cli()
