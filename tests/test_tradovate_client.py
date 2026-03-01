"""Tests for tradovate_client — API client and retry logic."""

import time
from unittest.mock import MagicMock, patch

import pytest
import requests
from bot.tradovate_client import TradovateClient, TradovateError, _build_session


def _make_client(**kwargs) -> TradovateClient:
    """Create a TradovateClient with test defaults."""
    defaults = dict(
        username="test_user",
        password="test_pass",
        device_id="test-device",
        cid=0,
        secret="",
        live=False,
    )
    defaults.update(kwargs)
    return TradovateClient(**defaults)


class TestTradovateClientInit:
    def test_demo_base_url(self):
        client = _make_client(live=False)
        assert "demo" in client.base_url

    def test_live_base_url(self):
        client = _make_client(live=True)
        assert "live" in client.base_url

    def test_sessions_created(self):
        client = _make_client()
        assert client._session is not None
        assert client._session_no_retry is not None


class TestCheckResponse:
    def test_success(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        TradovateClient._check_response(resp, "test")  # should not raise

    def test_http_error_with_json(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError()
        resp.status_code = 400
        resp.json.return_value = {"message": "Bad request"}
        with pytest.raises(TradovateError) as exc_info:
            TradovateClient._check_response(resp, "test")
        assert "400" in str(exc_info.value)

    def test_http_error_with_text(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError()
        resp.status_code = 500
        resp.json.side_effect = ValueError("no json")
        resp.text = "Internal Server Error"
        with pytest.raises(TradovateError):
            TradovateClient._check_response(resp, "test")


class TestEnsureAuth:
    def test_no_reauth_when_token_valid(self):
        client = _make_client()
        client.access_token = "valid-token"
        client._token_expiry = time.time() + 3600
        with patch.object(client, "authenticate") as mock_auth:
            client.ensure_auth()
            mock_auth.assert_not_called()

    def test_reauth_when_expired(self):
        client = _make_client()
        client.access_token = "expired"
        client._token_expiry = time.time() - 1
        with patch.object(client, "authenticate") as mock_auth:
            client.ensure_auth()
            mock_auth.assert_called_once()


class TestFlattenAllRetry:
    def test_flatten_succeeds_first_try(self):
        client = _make_client()
        client.access_token = "token"
        client._token_expiry = time.time() + 3600
        client.account_id = 1
        with patch.object(client, "get_open_orders", return_value=[]), \
             patch.object(client, "liquidate_position") as mock_liq:
            client.flatten_all()
            mock_liq.assert_called_once()

    def test_flatten_retries_on_failure(self):
        client = _make_client()
        client.access_token = "token"
        client._token_expiry = time.time() + 3600
        client.account_id = 1
        with patch.object(client, "get_open_orders", return_value=[]), \
             patch.object(client, "liquidate_position") as mock_liq, \
             patch("bot.tradovate_client.time.sleep"):  # speed up test
            mock_liq.side_effect = [
                TradovateError("timeout"),
                TradovateError("timeout"),
                None,  # succeeds on 3rd try
            ]
            client.flatten_all()
            assert mock_liq.call_count == 3

    def test_flatten_raises_after_max_retries(self):
        client = _make_client()
        client.access_token = "token"
        client._token_expiry = time.time() + 3600
        client.account_id = 1
        with patch.object(client, "get_open_orders", return_value=[]), \
             patch.object(client, "liquidate_position") as mock_liq, \
             patch("bot.tradovate_client.time.sleep"):
            mock_liq.side_effect = TradovateError("always fails")
            with pytest.raises(TradovateError, match="FLATTEN FAILED"):
                client.flatten_all()
            assert mock_liq.call_count == 3


class TestNoRetryEndpoints:
    def test_order_placement_uses_no_retry_session(self):
        client = _make_client()
        client.access_token = "token"
        client._token_expiry = time.time() + 3600
        client.account_id = 1

        # Verify the no-retry session is used for order endpoints
        assert "order/placeorder" in client._NO_RETRY_ENDPOINTS
        assert "order/placeOSO" in client._NO_RETRY_ENDPOINTS
        assert "order/cancelorder" in client._NO_RETRY_ENDPOINTS


class TestBuildSession:
    def test_session_has_retry_adapter(self):
        session = _build_session()
        # Check that https adapter has retry config
        adapter = session.get_adapter("https://example.com")
        assert adapter.max_retries.total == 3
