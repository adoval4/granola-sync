"""Tests for webhook sender."""

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from granola_sync.webhook import WebhookSender, sign_payload, verify_signature


class TestSignPayload:
    """Tests for sign_payload function."""

    def test_sign_payload_basic(self):
        """Test basic payload signing."""
        payload = {"key": "value", "number": 123}
        secret = "test-secret"

        signature = sign_payload(payload, secret)

        assert signature.startswith("sha256=")
        # Verify the signature manually
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        assert signature == f"sha256={expected}"

    def test_sign_payload_consistent(self):
        """Test that signing is consistent for the same input."""
        payload = {"a": 1, "b": "test"}
        secret = "my-secret"

        sig1 = sign_payload(payload, secret)
        sig2 = sign_payload(payload, secret)

        assert sig1 == sig2

    def test_sign_payload_different_secrets(self):
        """Test that different secrets produce different signatures."""
        payload = {"data": "test"}

        sig1 = sign_payload(payload, "secret1")
        sig2 = sign_payload(payload, "secret2")

        assert sig1 != sig2

    def test_sign_payload_complex_payload(self):
        """Test signing a complex payload."""
        payload = {
            "source": "Granola",
            "folder_name": "SQP",
            "note_id": "abc123",
            "title": "Sprint Planning",
            "participants": ["John", "Jane"],
            "note_text": "## Summary\n- Item 1\n- Item 2",
        }
        secret = "webhook-secret"

        signature = sign_payload(payload, secret)

        assert signature.startswith("sha256=")
        assert len(signature) == 7 + 64  # "sha256=" + 64 hex chars


class TestVerifySignature:
    """Tests for verify_signature function."""

    def test_verify_valid_signature(self):
        """Test verifying a valid signature."""
        payload = {"test": "data"}
        secret = "test-secret"
        signature = sign_payload(payload, secret)

        assert verify_signature(payload, secret, signature) is True

    def test_verify_invalid_signature(self):
        """Test verifying an invalid signature."""
        payload = {"test": "data"}
        secret = "test-secret"

        assert verify_signature(payload, secret, "sha256=invalid") is False

    def test_verify_wrong_secret(self):
        """Test verifying with wrong secret."""
        payload = {"test": "data"}
        signature = sign_payload(payload, "secret1")

        assert verify_signature(payload, "secret2", signature) is False


class TestWebhookSender:
    """Tests for WebhookSender class."""

    @pytest.fixture
    def sender(self):
        """Create a webhook sender for testing."""
        return WebhookSender(
            url="https://example.com/webhooks/granola/",
            secret="test-secret",
            retry_attempts=2,
            retry_delay=0,  # No delay for tests
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_success(self, sender):
        """Test successful webhook send."""
        respx.post("https://example.com/webhooks/granola/").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )

        payload = {"note_id": "doc1", "title": "Test Meeting"}
        response = await sender.send(payload)

        assert response.status_code == 200
        await sender.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_includes_signature(self, sender):
        """Test that webhook includes HMAC signature."""
        route = respx.post("https://example.com/webhooks/granola/").mock(
            return_value=httpx.Response(200)
        )

        payload = {"note_id": "doc1"}
        await sender.send(payload)

        request = route.calls[0].request
        assert "X-Granola-Signature" in request.headers
        signature = request.headers["X-Granola-Signature"]
        assert signature.startswith("sha256=")

        # Verify the signature is correct
        assert verify_signature(payload, "test-secret", signature)

        await sender.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_includes_user_agent(self, sender):
        """Test that webhook includes User-Agent header."""
        route = respx.post("https://example.com/webhooks/granola/").mock(
            return_value=httpx.Response(200)
        )

        await sender.send({"note_id": "doc1"})

        request = route.calls[0].request
        assert "User-Agent" in request.headers
        assert "granola-sync" in request.headers["User-Agent"]

        await sender.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retry_on_server_error(self, sender):
        """Test retry behavior on server errors."""
        # First call fails, second succeeds
        route = respx.post("https://example.com/webhooks/granola/").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(200),
            ]
        )

        payload = {"note_id": "doc1"}
        response = await sender.send(payload)

        assert response.status_code == 200
        assert route.call_count == 2

        await sender.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_retry_on_client_error(self, sender):
        """Test no retry on client errors (except 429)."""
        route = respx.post("https://example.com/webhooks/granola/").mock(
            return_value=httpx.Response(400, json={"error": "Bad request"})
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await sender.send({"note_id": "doc1"})

        assert exc_info.value.response.status_code == 400
        assert route.call_count == 1  # No retries

        await sender.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_retry_on_rate_limit(self, sender):
        """Test retry behavior on rate limiting (429)."""
        route = respx.post("https://example.com/webhooks/granola/").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200),
            ]
        )

        response = await sender.send({"note_id": "doc1"})

        assert response.status_code == 200
        assert route.call_count == 2

        await sender.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_exhausted_retries(self, sender):
        """Test error when all retries are exhausted."""
        respx.post("https://example.com/webhooks/granola/").mock(
            return_value=httpx.Response(500)
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await sender.send({"note_id": "doc1"})

        assert exc_info.value.response.status_code == 500

        await sender.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_auth_error(self, sender):
        """Test handling of authentication errors."""
        respx.post("https://example.com/webhooks/granola/").mock(
            return_value=httpx.Response(401, json={"error": "Invalid signature"})
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await sender.send({"note_id": "doc1"})

        assert exc_info.value.response.status_code == 401

        await sender.close()
