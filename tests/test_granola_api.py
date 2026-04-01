"""Tests for Granola API client."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from granola_sync.granola_api import (
    GranolaCacheReader,
    GranolaClient,
    _get_granola_app_dir,
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


def _make_cache_state(doc_id="doc1", folder_title="Sales Calls"):
    """Helper to create a valid cache state dict."""
    folder_id = "folder1"
    return {
        "documentListsMetadata": {
            folder_id: {"id": folder_id, "title": folder_title},
        },
        "documentLists": {
            folder_id: [doc_id],
        },
        "documents": {
            doc_id: {"id": doc_id, "title": "Meeting notes"},
        },
    }


def _write_cache(cache_dir: Path, state: dict, version: int, *, json_string_wrap: bool = False) -> Path:
    """Write a cache-v{version}.json file.

    Args:
        json_string_wrap: If True, encode the cache field as a JSON string (v3-style).
    """
    path = cache_dir / f"cache-v{version}.json"
    inner = {"state": state, "version": version}
    if json_string_wrap:
        path.write_text(json.dumps({"cache": json.dumps(inner)}))
    else:
        path.write_text(json.dumps({"cache": inner}))
    return path


class TestGranolaCacheReader:
    """Tests for GranolaCacheReader with auto-discovered cache files."""

    @pytest.fixture
    def cache_dir(self, tmp_path: Path):
        """Create a fake Granola app directory."""
        granola_dir = tmp_path / "Library" / "Application Support" / "Granola"
        granola_dir.mkdir(parents=True)
        return granola_dir

    def test_read_v3_cache_json_string_wrapped(self, cache_dir: Path):
        """Test reading v3 cache (JSON-string-wrapped)."""
        state = _make_cache_state()
        _write_cache(cache_dir, state, 3, json_string_wrap=True)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            result = reader.read_cache()

        assert result["documents"]["doc1"]["title"] == "Meeting notes"

    def test_read_v4_cache(self, cache_dir: Path):
        """Test reading v4 cache (dict-style)."""
        state = _make_cache_state()
        _write_cache(cache_dir, state, 4)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            result = reader.read_cache()

        assert result["documents"]["doc1"]["title"] == "Meeting notes"

    def test_discovers_v6_cache(self, cache_dir: Path):
        """Test that v6 cache is auto-discovered and read."""
        state = _make_cache_state(folder_title="V6 Folder")
        _write_cache(cache_dir, state, 6)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            folders = reader.get_folders()

        assert folders[0]["title"] == "V6 Folder"

    def test_prefers_highest_version(self, cache_dir: Path):
        """When multiple cache versions exist, the highest version is used."""
        _write_cache(cache_dir, _make_cache_state(folder_title="Old V3"), 3, json_string_wrap=True)
        _write_cache(cache_dir, _make_cache_state(folder_title="Mid V4"), 4)
        _write_cache(cache_dir, _make_cache_state(folder_title="New V6"), 6)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            folders = reader.get_folders()

        assert folders[0]["title"] == "New V6"

    def test_falls_back_to_older_version(self, cache_dir: Path):
        """When only an older version exists, it is used."""
        state = _make_cache_state(folder_title="V3 Folder")
        _write_cache(cache_dir, state, 3, json_string_wrap=True)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            folders = reader.get_folders()

        assert folders[0]["title"] == "V3 Folder"

    def test_skips_invalid_structure(self, cache_dir: Path):
        """Cache files missing required keys are skipped, falls back to next."""
        # v6 has no documents/documentListsMetadata — invalid
        invalid_state = {"somethingElse": {}}
        _write_cache(cache_dir, invalid_state, 6)
        # v4 is valid
        _write_cache(cache_dir, _make_cache_state(folder_title="Valid V4"), 4)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            folders = reader.get_folders()

        assert folders[0]["title"] == "Valid V4"

    def test_no_cache_files_raises(self, cache_dir: Path):
        """When no cache-v*.json files exist, raises FileNotFoundError."""
        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            with pytest.raises(FileNotFoundError, match="No Granola cache files"):
                reader.read_cache()

    def test_get_folders_end_to_end(self, cache_dir: Path):
        """End-to-end: get_folders works with auto-discovered cache."""
        state = _make_cache_state(doc_id="d1", folder_title="Standups")
        _write_cache(cache_dir, state, 6)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            folders = reader.get_folders()

        assert len(folders) == 1
        assert folders[0]["title"] == "Standups"
        assert folders[0]["documents"][0]["id"] == "d1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_folders_both_fail(self, cache_dir: Path):
        """GranolaClient.get_folders raises RuntimeError when cache and API both fail."""
        respx.get("https://api.granola.ai/v2/get-document-lists").mock(
            return_value=httpx.Response(500, json={"error": "Internal Server Error"})
        )

        client = GranolaClient(token="test-token")
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            with pytest.raises(RuntimeError, match="Failed to load folders"):
                await client.get_folders()

        await client.close()


class TestGranolaCacheReaderFolderMap:
    """Tests for GranolaCacheReader.get_folder_map."""

    @pytest.fixture
    def cache_dir(self, tmp_path: Path):
        granola_dir = tmp_path / "Library" / "Application Support" / "Granola"
        granola_dir.mkdir(parents=True)
        return granola_dir

    def test_get_folder_map(self, cache_dir: Path):
        """Test get_folder_map returns title→id mapping."""
        state = _make_cache_state(folder_title="Sales Calls")
        _write_cache(cache_dir, state, 6)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            folder_map = reader.get_folder_map()

        assert folder_map == {"Sales Calls": "folder1"}

    def test_get_folder_map_multiple_folders(self, cache_dir: Path):
        """Test get_folder_map with multiple folders."""
        state = {
            "documentListsMetadata": {
                "f1": {"id": "f1", "title": "SQP"},
                "f2": {"id": "f2", "title": "CLIENT-A"},
            },
            "documentLists": {"f1": [], "f2": []},
            "documents": {},
        }
        _write_cache(cache_dir, state, 6)

        reader = GranolaCacheReader()
        with patch("granola_sync.granola_api._get_granola_app_dir", return_value=cache_dir):
            folder_map = reader.get_folder_map()

        assert folder_map == {"SQP": "f1", "CLIENT-A": "f2"}


class TestGranolaClient:
    """Tests for GranolaClient class."""

    @pytest.fixture
    def client(self):
        """Create a client with a test token."""
        return GranolaClient(token="test-token")

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_folders(self, client):
        """Test fetching folders via API fallback when cache is unavailable."""
        mock_folders = [
            {"id": "folder1", "title": "Sales Calls", "documents": []},
            {"id": "folder2", "title": "Standups", "documents": [{"id": "doc1"}]},
        ]

        respx.get("https://api.granola.ai/v2/get-document-lists").mock(
            return_value=httpx.Response(200, json={"lists": mock_folders})
        )

        with patch(
            "granola_sync.granola_api.GranolaCacheReader.get_folders",
            side_effect=FileNotFoundError("no cache"),
        ):
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
    async def test_get_documents_by_folder(self, client):
        """Test fetching documents filtered by folder/list ID."""
        mock_docs = [
            {"id": "doc1", "title": "Meeting in Folder", "created_at": "2026-01-17T10:00:00Z"},
        ]

        route = respx.post("https://api.granola.ai/v2/get-documents")
        route.mock(return_value=httpx.Response(200, json={"docs": mock_docs}))

        documents = await client.get_documents_by_folder("folder-123", limit=50)

        assert len(documents) == 1
        assert documents[0]["title"] == "Meeting in Folder"

        # Verify the request included list_id
        request = route.calls[0].request
        body = json.loads(request.content)
        assert body["list_id"] == "folder-123"
        assert body["limit"] == 50

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
