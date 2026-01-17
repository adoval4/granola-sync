"""Granola API client."""

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

GRANOLA_API_BASE = "https://api.granola.ai"
TOKEN_REFRESH_BUFFER_SECONDS = 300  # Refresh 5 minutes before expiration


def get_token_file_path() -> Path:
    """Get the path to the Granola credentials file.

    Returns:
        Path to the supabase.json file
    """
    if os.name == "nt":
        # Windows
        app_data = os.environ.get("APPDATA", "")
        return Path(app_data) / "Granola" / "supabase.json"
    elif os.uname().sysname == "Darwin":
        # macOS
        return Path.home() / "Library" / "Application Support" / "Granola" / "supabase.json"
    else:
        # Linux
        return Path.home() / ".config" / "Granola" / "supabase.json"


def is_token_expired(workos_tokens: dict[str, Any]) -> bool:
    """Check if the access token has expired or will expire soon.

    Args:
        workos_tokens: The parsed workos_tokens object

    Returns:
        True if the token has expired or will expire within the buffer time
    """
    current_time = time.time() * 1000  # Convert to milliseconds
    token_obtained_at = workos_tokens.get("obtained_at", 0)
    expires_in_ms = workos_tokens.get("expires_in", 0) * 1000
    expiration_time = token_obtained_at + expires_in_ms
    buffer_time = TOKEN_REFRESH_BUFFER_SECONDS * 1000

    return current_time >= (expiration_time - buffer_time)


def refresh_access_token(workos_tokens: dict[str, Any]) -> dict[str, Any]:
    """Refresh the access token using the refresh token.

    Args:
        workos_tokens: The current workos_tokens object

    Returns:
        Updated workos_tokens with new access token

    Raises:
        httpx.HTTPStatusError: If the refresh request fails
    """
    logger.debug("refreshing_access_token")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            "https://api.granola.ai/v1/refresh-access-token",
            headers={
                "Authorization": f"Bearer {workos_tokens['access_token']}",
                "Content-Type": "application/json",
            },
            json={
                "refresh_token": workos_tokens["refresh_token"],
                "provider": "workos",
            },
        )
        response.raise_for_status()
        refresh_response = response.json()

    updated_tokens = {
        **workos_tokens,
        "access_token": refresh_response["access_token"],
        "expires_in": refresh_response["expires_in"],
        "token_type": refresh_response["token_type"],
        "obtained_at": int(time.time() * 1000),
        "refresh_token": refresh_response.get("refresh_token", workos_tokens["refresh_token"]),
    }

    logger.debug("access_token_refreshed")
    return updated_tokens


def get_granola_token() -> str:
    """Get the Granola authentication token from the local storage.

    The token is stored by the Granola desktop app in supabase.json.
    If the token has expired, it will be refreshed automatically.

    Returns:
        The authentication token

    Raises:
        FileNotFoundError: If the token file doesn't exist
        ValueError: If the token cannot be found in the file
    """
    token_path = get_token_file_path()

    if not token_path.exists():
        raise FileNotFoundError(
            f"Granola credentials not found at {token_path}. "
            "Make sure the Granola app is installed and you are logged in."
        )

    with open(token_path) as f:
        token_data = json.load(f)

    workos_tokens_str = token_data.get("workos_tokens")
    if not workos_tokens_str:
        raise ValueError("Could not find workos_tokens in Granola credentials file")

    workos_tokens = json.loads(workos_tokens_str)

    token = workos_tokens.get("access_token")
    if not token:
        raise ValueError("Could not find access_token in Granola credentials")

    if is_token_expired(workos_tokens):
        logger.debug("token_expired_refreshing")
        try:
            workos_tokens = refresh_access_token(workos_tokens)
            token = workos_tokens["access_token"]
        except Exception as e:
            raise ValueError(
                f"Access token has expired and refresh failed: {e}. "
                "Please re-authenticate in the Granola app."
            ) from e

    return token


class GranolaClient:
    """Client for interacting with the Granola API."""

    def __init__(self, token: Optional[str] = None, base_url: str = GRANOLA_API_BASE):
        """Initialize the Granola API client.

        Args:
            token: Authentication token. If not provided, will be loaded from local storage.
            base_url: Base URL for the API.
        """
        self._token = token
        self.base_url = base_url
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def token(self) -> str:
        """Get the authentication token, loading it if necessary."""
        if self._token is None:
            self._token = get_granola_token()
        return self._token

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_folders(self) -> list[dict[str, Any]]:
        """Get all folders (document lists) from Granola.

        Returns:
            List of folder objects with id, title, and documents
        """
        client = await self._get_client()
        logger.debug("fetching_folders")

        response = await client.get("/v2/get-document-lists")
        response.raise_for_status()

        data = response.json()
        folders = data.get("lists", [])
        logger.debug("folders_fetched", count=len(folders))
        return folders

    async def get_documents(
        self, limit: int = 100, offset: int = 0, include_last_viewed_panel: bool = True
    ) -> list[dict[str, Any]]:
        """Get recent documents from Granola.

        Args:
            limit: Maximum number of documents to fetch per page
            offset: Number of documents to skip (for pagination)
            include_last_viewed_panel: Whether to include document content

        Returns:
            List of document objects
        """
        client = await self._get_client()
        logger.debug("fetching_documents", limit=limit, offset=offset)

        response = await client.post(
            "/v2/get-documents",
            json={
                "limit": limit,
                "offset": offset,
                "include_last_viewed_panel": include_last_viewed_panel,
            },
        )
        response.raise_for_status()

        data = response.json()
        documents = data.get("docs", data) if isinstance(data, dict) else data
        logger.debug("documents_fetched", count=len(documents))
        return documents

    async def get_all_documents(
        self, page_size: int = 100, include_last_viewed_panel: bool = True
    ) -> list[dict[str, Any]]:
        """Get all documents from Granola with pagination.

        Args:
            page_size: Number of documents to fetch per page
            include_last_viewed_panel: Whether to include document content

        Returns:
            List of all document objects
        """
        documents: list[dict[str, Any]] = []
        offset = 0

        while True:
            page = await self.get_documents(
                limit=page_size,
                offset=offset,
                include_last_viewed_panel=include_last_viewed_panel,
            )
            if not page:
                break

            documents.extend(page)

            if len(page) < page_size:
                break

            offset += page_size

        return documents

    async def get_transcript(self, doc_id: str) -> list[dict[str, Any]]:
        """Get the transcript for a document.

        Args:
            doc_id: The document ID

        Returns:
            List of transcript entries with speaker, text, and timestamps
        """
        client = await self._get_client()
        logger.debug("fetching_transcript", doc_id=doc_id)

        response = await client.post(
            "/v1/get-document-transcript",
            json={"document_id": doc_id},
        )
        response.raise_for_status()

        data = response.json()
        # The API returns the transcript array directly
        transcript = data if isinstance(data, list) else data.get("transcript", [])
        logger.debug("transcript_fetched", doc_id=doc_id, segments=len(transcript))
        return transcript
