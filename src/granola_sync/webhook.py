"""Webhook sender with HMAC signing."""

import hashlib
import hmac
import json
from typing import Any, Optional

import httpx
import structlog

from . import __version__

logger = structlog.get_logger()


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    """Generate HMAC-SHA256 signature for webhook payload.

    Args:
        payload: The payload to sign
        secret: The HMAC secret

    Returns:
        Signature in the format "sha256=<hexdigest>"
    """
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={signature}"


def verify_signature(payload: dict[str, Any], secret: str, signature: str) -> bool:
    """Verify an HMAC-SHA256 signature.

    Args:
        payload: The payload that was signed
        secret: The HMAC secret
        signature: The signature to verify

    Returns:
        True if the signature is valid, False otherwise
    """
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature)


class WebhookSender:
    """Sends webhooks to the backend with HMAC signing."""

    def __init__(
        self,
        url: str,
        secret: str,
        retry_attempts: int = 3,
        retry_delay: int = 30,
    ):
        """Initialize the webhook sender.

        Args:
            url: The webhook endpoint URL
            secret: The HMAC signing secret
            retry_attempts: Number of retry attempts for failed requests
            retry_delay: Delay in seconds between retries
        """
        self.url = url
        self.secret = secret
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def send(self, payload: dict[str, Any]) -> httpx.Response:
        """Send a webhook with HMAC signature.

        Args:
            payload: The payload to send

        Returns:
            The HTTP response

        Raises:
            httpx.HTTPStatusError: If the request fails after all retries
        """
        client = await self._get_client()

        signature = sign_payload(payload, self.secret)
        headers = {
            "Content-Type": "application/json",
            "X-Granola-Signature": signature,
            "User-Agent": f"granola-sync/{__version__}",
        }

        last_error: Optional[Exception] = None

        for attempt in range(self.retry_attempts):
            try:
                logger.debug(
                    "sending_webhook",
                    url=self.url,
                    attempt=attempt + 1,
                    note_id=payload.get("note_id"),
                )

                response = await client.post(
                    self.url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()

                logger.info(
                    "webhook_sent",
                    url=self.url,
                    status=response.status_code,
                    note_id=payload.get("note_id"),
                )
                return response

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(
                    "webhook_failed",
                    url=self.url,
                    status=e.response.status_code,
                    attempt=attempt + 1,
                    max_attempts=self.retry_attempts,
                    note_id=payload.get("note_id"),
                )

                # Don't retry on client errors (4xx) except rate limiting
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    raise

            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    "webhook_request_error",
                    url=self.url,
                    error=str(e),
                    attempt=attempt + 1,
                    max_attempts=self.retry_attempts,
                    note_id=payload.get("note_id"),
                )

            # Wait before retrying (unless this was the last attempt)
            if attempt < self.retry_attempts - 1:
                import asyncio
                await asyncio.sleep(self.retry_delay)

        # All retries exhausted
        if last_error:
            raise last_error
        raise RuntimeError("Webhook send failed with unknown error")
