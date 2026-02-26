"""
Tradovate REST API client.
Handles authentication, token refresh, and all API calls for trading and account data.
"""

import time
import uuid
import threading
import requests
from loguru import logger


class TradovateError(Exception):
    """Raised when the Tradovate API returns an error."""


class TradovateClient:
    LIVE_BASE = "https://live.tradovateapi.com/v1"
    DEMO_BASE = "https://demo.tradovateapi.com/v1"
    MD_BASE   = "https://md.tradovateapi.com/v1"

    def __init__(
        self,
        username: str,
        password: str,
        device_id: str,
        cid: int,
        secret: str,
        live: bool = True,
        account_name: str = "",   # Leave blank to auto-pick first account
    ):
        self.username     = username
        self.password     = password
        self.device_id    = device_id
        self.cid          = cid
        self.secret       = secret
        self.live         = live
        self.account_name = account_name

        self.base_url = self.LIVE_BASE if live else self.DEMO_BASE

        self.access_token    : str  = ""
        self.md_access_token : str  = ""
        self._token_expiry   : float = 0.0
        self._lock = threading.Lock()

        # Populated after auth
        self.account_id   : int = 0
        self.account_spec : str = ""

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> dict:
        """Authenticate and populate access tokens + account info."""
        url = f"{self.base_url}/auth/accesstokenrequest"
        payload = {
            "name":       self.username,
            "password":   self.password,
            "deviceId":   self.device_id,
            "cid":        self.cid,
            "sec":        self.secret,
            "appId":      "TraderJP",
            "appVersion": "1.0.0",
        }
        resp = requests.post(url, json=payload, timeout=15)
        self._check_response(resp, "authenticate")
        data = resp.json()

        self.access_token    = data.get("accessToken", "")
        self.md_access_token = data.get("mdAccessToken", "")
        # Tokens are valid 24 h; refresh 5 min before expiry
        self._token_expiry   = time.time() + 86_400 - 300

        if not self.access_token:
            raise TradovateError(f"Auth failed: {data}")

        self._load_account()
        logger.info(f"Authenticated. Account: {self.account_spec} (id={self.account_id})")
        return data

    def _load_account(self):
        """Fetch account list and pick the configured (or first) account."""
        accounts = self.get_accounts()
        if not accounts:
            raise TradovateError("No accounts found on this Tradovate login.")

        if self.account_name:
            match = [a for a in accounts if a.get("name") == self.account_name]
            if not match:
                names = [a.get("name") for a in accounts]
                raise TradovateError(
                    f"Account '{self.account_name}' not found. Available: {names}"
                )
            account = match[0]
        else:
            account = accounts[0]

        self.account_id   = account["id"]
        self.account_spec = account["name"]

    def ensure_auth(self):
        """Re-authenticate if the token is expired or close to expiry."""
        with self._lock:
            if time.time() >= self._token_expiry:
                logger.info("Access token expired/near expiry — refreshing...")
                self.authenticate()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self, md: bool = False) -> dict:
        self.ensure_auth()
        token = self.md_access_token if md else self.access_token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @staticmethod
    def _check_response(resp: requests.Response, context: str = ""):
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise TradovateError(f"[{context}] HTTP {resp.status_code}: {detail}")

    def _get(self, endpoint: str, params: dict = None, md: bool = False) -> dict | list:
        base = self.MD_BASE if md else self.base_url
        resp = requests.get(f"{base}/{endpoint}", params=params,
                            headers=self._headers(md=md), timeout=15)
        self._check_response(resp, endpoint)
        return resp.json()

    def _post(self, endpoint: str, body: dict = None, md: bool = False) -> dict | list:
        base = self.MD_BASE if md else self.base_url
        resp = requests.post(f"{base}/{endpoint}", json=body or {},
                             headers=self._headers(md=md), timeout=15)
        self._check_response(resp, endpoint)
        return resp.json()

    # ------------------------------------------------------------------
    # Account & Balance
    # ------------------------------------------------------------------

    def get_accounts(self) -> list:
        resp = requests.get(
            f"{self.base_url}/account/list",
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=15,
        )
        self._check_response(resp, "account/list")
        return resp.json()

    def get_cash_balance_snapshot(self) -> dict:
        """Returns realized P&L, open P&L, and net balance for the account."""
        return self._post(
            "cashbalance/getcashbalancesnapshot",
            {"accountId": self.account_id},
        )

    def get_account_risk_status(self) -> dict:
        """Tradovate's built-in risk status (if available on account)."""
        try:
            return self._post(
                "useraccountriskparameter/list",
                {"accountId": self.account_id},
            )
        except TradovateError:
            return {}

    # ------------------------------------------------------------------
    # Positions & Orders
    # ------------------------------------------------------------------

    def get_positions(self) -> list:
        """Return all open positions for the account."""
        all_positions = self._get("position/list")
        return [p for p in all_positions if p.get("accountId") == self.account_id]

    def get_open_orders(self) -> list:
        """Return all working/open orders for the account."""
        all_orders = self._get("order/list")
        return [
            o for o in all_orders
            if o.get("accountId") == self.account_id
            and o.get("ordStatus") in ("Working", "PendingNew", "PendingCancel")
        ]

    # ------------------------------------------------------------------
    # Order Placement
    # ------------------------------------------------------------------

    def place_market_order(
        self,
        action: str,
        symbol: str,
        qty: int,
        comment: str = "TraderJP",
    ) -> dict:
        """
        Place a market order.
        action: 'Buy' or 'Sell'
        """
        payload = {
            "accountSpec":  self.account_spec,
            "accountId":    self.account_id,
            "clOrdId":      str(uuid.uuid4())[:16],
            "action":       action.capitalize(),
            "symbol":       symbol,
            "orderQty":     qty,
            "orderType":    "Market",
            "timeInForce":  "Day",
            "isAutomated":  True,
            "text":         comment,
        }
        result = self._post("order/placeorder", payload)
        logger.info(f"Market order placed: {action} {qty} {symbol} → {result}")
        return result

    def place_limit_order(
        self,
        action: str,
        symbol: str,
        qty: int,
        price: float,
        comment: str = "TraderJP",
    ) -> dict:
        """Place a limit order."""
        payload = {
            "accountSpec":  self.account_spec,
            "accountId":    self.account_id,
            "clOrdId":      str(uuid.uuid4())[:16],
            "action":       action.capitalize(),
            "symbol":       symbol,
            "orderQty":     qty,
            "orderType":    "Limit",
            "price":        price,
            "timeInForce":  "Day",
            "isAutomated":  True,
            "text":         comment,
        }
        result = self._post("order/placeorder", payload)
        logger.info(f"Limit order placed: {action} {qty} {symbol} @ {price} → {result}")
        return result

    def place_stop_order(
        self,
        action: str,
        symbol: str,
        qty: int,
        stop_price: float,
        comment: str = "TraderJP",
    ) -> dict:
        """Place a stop market order (used for stop-loss)."""
        payload = {
            "accountSpec":  self.account_spec,
            "accountId":    self.account_id,
            "clOrdId":      str(uuid.uuid4())[:16],
            "action":       action.capitalize(),
            "symbol":       symbol,
            "orderQty":     qty,
            "orderType":    "Stop",
            "stopPrice":    stop_price,
            "timeInForce":  "Day",
            "isAutomated":  True,
            "text":         comment,
        }
        result = self._post("order/placeorder", payload)
        logger.info(f"Stop order placed: {action} {qty} {symbol} stop@{stop_price} → {result}")
        return result

    def cancel_order(self, order_id: int) -> dict:
        result = self._post("order/cancelorder", {"orderId": order_id})
        logger.info(f"Order cancelled: {order_id}")
        return result

    def cancel_all_orders(self):
        """Cancel all open orders for this account."""
        orders = self.get_open_orders()
        for order in orders:
            try:
                self.cancel_order(order["id"])
            except TradovateError as e:
                logger.warning(f"Could not cancel order {order['id']}: {e}")

    def liquidate_position(self, symbol: str = "") -> dict:
        """
        Flatten / liquidate the position.
        If symbol is blank, Tradovate will close all positions on the account.
        """
        payload = {"accountId": self.account_id, "isAutomated": True}
        if symbol:
            payload["symbol"] = symbol
        result = self._post("order/liquidateposition", payload)
        logger.warning(f"Liquidate position called: symbol={symbol or 'ALL'} → {result}")
        return result

    def flatten_all(self):
        """Cancel all orders then liquidate all positions."""
        logger.warning("FLATTEN ALL — cancelling orders then liquidating positions.")
        self.cancel_all_orders()
        self.liquidate_position()

    # ------------------------------------------------------------------
    # Contract / Quote
    # ------------------------------------------------------------------

    def find_contract(self, name: str) -> dict:
        """Look up a contract by name (e.g. 'ESH5')."""
        return self._get("contract/find", params={"name": name})

    def get_quote(self, symbol: str) -> dict:
        """Fetch current best bid/ask for a symbol via MD endpoint."""
        return self._get("md/getQuote", params={"symbol": symbol}, md=True)
