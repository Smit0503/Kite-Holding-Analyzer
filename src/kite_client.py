import hashlib
import os
from typing import Any, Dict
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()


class KiteClient:
    """Client for Kite Connect v3 authentication and portfolio APIs."""

    def __init__(self):
        # Prefer the official Kite names, but keep compatibility for existing local env names.
        self.api_key = os.getenv("KITE_API_KEY") or os.getenv("ZERODHIA_API_KEY")
        self.api_secret = os.getenv("KITE_API_SECRET") or os.getenv("ZERODHIA_SECRET")
        self.callback_url = os.getenv("REDIRECT_URI", "http://127.0.0.1:5000/oauth/callback")

        self.base_url = "https://api.kite.trade"
        self.login_url = "https://kite.zerodha.com/connect/login"

        if not self.api_key or not self.api_secret:
            raise ValueError(
                "Missing Kite keys. Set KITE_API_KEY and KITE_API_SECRET in .env. "
                "Legacy ZERODHIA_* names are also supported for compatibility."
            )

    def generate_login_url(self) -> str:
        """Build the Kite Connect v3 login URL."""
        params = {
            "v": 3,
            "api_key": self.api_key,
        }
        return f"{self.login_url}?{urlencode(params)}"

    def compute_checksum(self, request_token: str) -> str:
        """Compute the Kite checksum for the request token."""
        raw = self.api_key + request_token + self.api_secret
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def exchange_request_token(self, request_token: str) -> Dict[str, Any]:
        """Exchange request_token + checksum for access_token."""
        checksum = self.compute_checksum(request_token)
        payload = {
            "api_key": self.api_key,
            "request_token": request_token,
            "checksum": checksum,
        }

        try:
            response = requests.post(f"{self.base_url}/session/token", data=payload, timeout=20)
            response.raise_for_status()
            json_body = response.json()
            if isinstance(json_body, dict) and "data" in json_body and isinstance(json_body["data"], dict):
                return json_body["data"]
            return json_body
        except requests.RequestException as exc:
            raise RuntimeError(f"Kite token exchange failed: {exc}") from exc

    def get_profile(self, access_token: str) -> Dict[str, Any]:
        """Fetch user profile for the authenticated session."""
        headers = {"Authorization": f"token {self.api_key}:{access_token}"}
        response = requests.get(f"{self.base_url}/user/profile", headers=headers, timeout=20)
        response.raise_for_status()
        json_body = response.json()
        if isinstance(json_body, dict) and "data" in json_body:
            return json_body["data"]
        return json_body

    def get_holdings(self, access_token: str) -> Dict[str, Any]:
        """Fetch current equity holdings."""
        headers = {"Authorization": f"token {self.api_key}:{access_token}"}
        response = requests.get(f"{self.base_url}/portfolio/holdings", headers=headers, timeout=20)
        response.raise_for_status()
        json_body = response.json()
        if isinstance(json_body, dict) and "data" in json_body:
            return json_body["data"]
        return json_body

    def get_mf_holdings(self, access_token: str) -> list:
        """Fetch mutual fund holdings via the MF holdings endpoint."""
        headers = {"Authorization": f"token {self.api_key}:{access_token}"}
        response = requests.get(f"{self.base_url}/mf/holdings", headers=headers, timeout=20)
        response.raise_for_status()
        json_body = response.json()
        if isinstance(json_body, dict):
            data = json_body.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("holdings", []) or data.get("mf_holdings", []) or []
        if isinstance(json_body, list):
            return json_body
        return []

    def refresh_mf_nav(self, holdings: list) -> list:
        """Refresh MF NAV values from the published NAV data using ISIN matching."""
        if not isinstance(holdings, list):
            return holdings

        refreshed = []
        for holding in holdings:
            item = dict(holding) if isinstance(holding, dict) else holding
            if not isinstance(item, dict):
                refreshed.append(item)
                continue

            isin = str(item.get("tradingsymbol") or item.get("isin") or "").strip().upper()
            fund_name = str(item.get("fund") or item.get("scheme") or item.get("name") or "").strip()
            try:
                search = requests.get(
                    "https://api.mfapi.in/mf/search",
                    params={"q": fund_name},
                    timeout=8,
                )
                search.raise_for_status()
                candidates = search.json()
                if not isinstance(candidates, list):
                    candidates = []

                for candidate in candidates:
                    scheme_code = candidate.get("schemeCode") if isinstance(candidate, dict) else None
                    if not scheme_code:
                        continue
                    scheme = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=8)
                    scheme.raise_for_status()
                    scheme_json = scheme.json()
                    meta = scheme_json.get("meta", {}) if isinstance(scheme_json, dict) else {}
                    scheme_isins = {
                        str(meta.get(key) or "").upper()
                        for key in ("isin_growth", "isin_div_reinvestment")
                    }
                    if isin and isin not in scheme_isins:
                        continue
                    data = scheme_json.get("data", []) if isinstance(scheme_json, dict) else []
                    if data:
                        item["last_price"] = float(data[0]["nav"])
                        item["last_price_date"] = str(data[0]["date"])
                    break
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                print(f"MF NAV refresh failed for {fund_name}: {exc}")

            refreshed.append(item)
        return refreshed

    def get_orders(self, access_token: str, from_date: str = "", to_date: str = "") -> list:
        """Fetch order history as a fallback for MF and non-stock trades.

        Kite's order/trade APIs are daily endpoints. They return the current day
        of order activity and do not provide a full multi-year trade ledger.
        """
        headers = {"Authorization": f"token {self.api_key}:{access_token}"}
        params = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        response = requests.get(f"{self.base_url}/orders", headers=headers, params=params, timeout=20)
        response.raise_for_status()
        json_body = response.json()
        if isinstance(json_body, dict):
            data = json_body.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("orders", []) or data.get("trades", []) or []
        if isinstance(json_body, list):
            return json_body
        return []

    def get_tradebook(self, access_token: str, from_date: str = "", to_date: str = "") -> list:
        """Fetch executed trades and order history for FIFO-based purchase date and P&L logic.

        This is bounded by Kite's daily order/trade API and will only include the
        day(s) exposed by Kite, not a complete long-term historical ledger.
        """
        headers = {"Authorization": f"token {self.api_key}:{access_token}"}
        params = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        trade_rows = []
        for endpoint in ("/trades", "/orders"):
            response = requests.get(f"{self.base_url}{endpoint}", headers=headers, params=params, timeout=20)
            if response.status_code >= 400:
                continue
            json_body = response.json()
            data = []
            if isinstance(json_body, dict):
                data = json_body.get("data") if isinstance(json_body.get("data"), list) else []
                if not data and isinstance(json_body.get("data"), dict):
                    nested = json_body["data"]
                    data = nested.get("orders", []) or nested.get("trades", []) or []
            elif isinstance(json_body, list):
                data = json_body
            trade_rows.extend(data)

        deduped = []
        seen = set()
        for item in trade_rows:
            if not isinstance(item, dict):
                continue
            key_parts = [
                str(item.get("trade_id") or item.get("order_id") or item.get("order_id") or item.get("transaction_id") or ""),
                str(item.get("tradingsymbol") or item.get("symbol") or item.get("instrument_name") or ""),
                str(item.get("timestamp") or item.get("order_timestamp") or item.get("filled_at") or item.get("trade_date") or ""),
            ]
            key = "|".join(key_parts)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped
