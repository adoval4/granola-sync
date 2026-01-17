"""Tests for Granola API client."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from granola_sync.granola_api import (
    GranolaClient,
    get_granola_token,
    get_token_file_path,
    is_token_expired,
    refresh_access_token,
)


def create_workos_tokens(
    access_token: str = "test-token-123",
    expires_in: int = 3600,
    obtained_at: int | None = None,
) -> dict:
    """Helper to create a valid workos_tokens structure."""
    if obtained_at is None:
        obtained_at = int(time.time() * 1000)
    return {
        "access_token": access_token,
        "expires_in": expires_in,
        "refresh_token": "refresh-token-xyz",
        "token_type": "Bearer",
        "obtained_at": obtained_at,
        "session_id": "session-123",
        "external_id": "external-456",
    }


def create_supabase_json(workos_tokens: dict) -> str:
    """Helper to create the supabase.json content."""
    return json.dumps({"workos_tokens": json.dumps(workos_tokens)})


class TestGetGranolaToken:
    """Tests for get_granola_token function."""

    def test_get_token_from_file(self, tmp_path: Path):
        """Test loading token from supabase.json file."""
        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            # Simulate macOS path
            token_dir = tmp_path / "Library" / "Application Support" / "Granola"
            token_dir.mkdir(parents=True)
            token_file = token_dir / "supabase.json"
            workos_tokens = create_workos_tokens(access_token="test-token-123")
            token_file.write_text(create_supabase_json(workos_tokens))

            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                token = get_granola_token()

            assert token == "test-token-123"

    def test_get_token_file_not_found(self, tmp_path: Path):
        """Test error when token file doesn't exist."""
        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                with pytest.raises(FileNotFoundError, match="Granola credentials not found"):
                    get_granola_token()

    def test_get_token_missing_workos_tokens(self, tmp_path: Path):
        """Test error when workos_tokens key is missing from file."""
        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            token_dir = tmp_path / "Library" / "Application Support" / "Granola"
            token_dir.mkdir(parents=True)
            token_file = token_dir / "supabase.json"
            token_file.write_text(json.dumps({"other_key": "value"}))

            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                with pytest.raises(ValueError, match="Could not find workos_tokens"):
                    get_granola_token()

    def test_get_token_missing_access_token(self, tmp_path: Path):
        """Test error when access_token is missing from workos_tokens."""
        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            token_dir = tmp_path / "Library" / "Application Support" / "Granola"
            token_dir.mkdir(parents=True)
            token_file = token_dir / "supabase.json"
            workos_tokens = {"refresh_token": "refresh-only", "expires_in": 3600}
            token_file.write_text(json.dumps({"workos_tokens": json.dumps(workos_tokens)}))

            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                with pytest.raises(ValueError, match="Could not find access_token"):
                    get_granola_token()


class TestIsTokenExpired:
    """Tests for is_token_expired function."""

    def test_token_not_expired(self):
        """Test that a fresh token is not expired."""
        workos_tokens = create_workos_tokens(
            expires_in=3600,
            obtained_at=int(time.time() * 1000),
        )
        assert not is_token_expired(workos_tokens)

    def test_token_expired(self):
        """Test that an old token is expired."""
        # Token obtained 2 hours ago with 1 hour expiry
        workos_tokens = create_workos_tokens(
            expires_in=3600,
            obtained_at=int((time.time() - 7200) * 1000),
        )
        assert is_token_expired(workos_tokens)

    def test_token_expires_within_buffer(self):
        """Test that a token expiring within 5 minutes is considered expired."""
        # Token will expire in 4 minutes (within 5-minute buffer)
        workos_tokens = create_workos_tokens(
            expires_in=240,  # 4 minutes
            obtained_at=int(time.time() * 1000),
        )
        assert is_token_expired(workos_tokens)


class TestRefreshAccessToken:
    """Tests for refresh_access_token function."""

    @respx.mock
    def test_refresh_token_success(self):
        """Test successful token refresh."""
        workos_tokens = create_workos_tokens(access_token="old-token")

        respx.post("https://api.granola.ai/v1/refresh-access-token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-token-abc",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                },
            )
        )

        updated = refresh_access_token(workos_tokens)

        assert updated["access_token"] == "new-token-abc"
        assert updated["expires_in"] == 7200
        assert updated["refresh_token"] == "refresh-token-xyz"  # Preserved
        assert updated["obtained_at"] > workos_tokens["obtained_at"]

    @respx.mock
    def test_refresh_token_failure(self):
        """Test handling of refresh token failure."""
        workos_tokens = create_workos_tokens()

        respx.post("https://api.granola.ai/v1/refresh-access-token").mock(
            return_value=httpx.Response(401, json={"error": "Invalid refresh token"})
        )

        with pytest.raises(httpx.HTTPStatusError):
            refresh_access_token(workos_tokens)


class TestGranolaClient:
    """Tests for GranolaClient class."""

    @pytest.fixture
    def client(self):
        """Create a client with a test token."""
        return GranolaClient(token="test-token")

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_folders(self, client):
        """Test fetching folders via GET /v2/get-document-lists."""
        mock_folders = [
            {"id": "folder1", "title": "Sales Calls", "documents": []},
            {"id": "folder2", "title": "Standups", "documents": [{"id": "doc1"}]},
        ]

        respx.get("https://api.granola.ai/v2/get-document-lists").mock(
            return_value=httpx.Response(200, json={"lists": mock_folders})
        )

        folders = await client.get_folders()

        assert len(folders) == 2
        assert folders[0]["title"] == "Sales Calls"
        assert folders[1]["title"] == "Standups"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_documents(self, client):
        """Test fetching documents via POST /v2/get-documents."""
        mock_documents = [
            {"id": "doc1", "title": "Meeting 1", "created_at": "2026-01-17T10:00:00Z"},
            {"id": "doc2", "title": "Meeting 2", "created_at": "2026-01-17T11:00:00Z"},
        ]

        respx.post("https://api.granola.ai/v2/get-documents").mock(
            return_value=httpx.Response(200, json={"docs": mock_documents})
        )

        documents = await client.get_documents(limit=100)

        assert len(documents) == 2
        assert documents[0]["title"] == "Meeting 1"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_all_documents(self, client):
        """Test fetching all documents with pagination."""
        page1 = [
            {"id": "doc1", "title": "Meeting 1"},
            {"id": "doc2", "title": "Meeting 2"},
        ]
        page2 = [
            {"id": "doc3", "title": "Meeting 3"},
        ]

        # Mock two pages of results
        route = respx.post("https://api.granola.ai/v2/get-documents")
        route.side_effect = [
            httpx.Response(200, json={"docs": page1}),
            httpx.Response(200, json={"docs": page2}),
            httpx.Response(200, json={"docs": []}),
        ]

        documents = await client.get_all_documents(page_size=2)

        assert len(documents) == 3
        assert documents[0]["title"] == "Meeting 1"
        assert documents[2]["title"] == "Meeting 3"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_transcript(self, client):
        """Test fetching a document transcript via POST /v1/get-document-transcript."""
        mock_transcript = [
            {"source": "microphone", "text": "Hello everyone"},
            {"source": "speaker", "text": "Hi there"},
        ]

        respx.post("https://api.granola.ai/v1/get-document-transcript").mock(
            return_value=httpx.Response(200, json=mock_transcript)
        )

        transcript = await client.get_transcript("doc1")

        assert len(transcript) == 2
        assert transcript[0]["text"] == "Hello everyone"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handling(self, client):
        """Test handling of API errors."""
        respx.post("https://api.granola.ai/v2/get-documents").mock(
            return_value=httpx.Response(401, json={"error": "Unauthorized"})
        )

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_documents()

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_client_reuses_connection(self, client):
        """Test that the client reuses the HTTP connection."""
        respx.post("https://api.granola.ai/v2/get-documents").mock(
            return_value=httpx.Response(200, json={"docs": []})
        )

        # Make two requests
        await client.get_documents()
        await client.get_documents()

        # Should have reused the same client
        assert client._client is not None
        assert not client._client.is_closed

        await client.close()
        assert client._client.is_closed
