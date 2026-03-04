# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python CLI tool that syncs Granola meeting notes to a webhook. Runs as a background service (launchd on macOS, systemd on Linux). Uses an API-first approach with local cache fallback.

## Commands

```bash
# Install in dev mode
pip install -e ".[dev]"

# Run all tests (skip CLI tests that may hang)
.venv/bin/python -m pytest tests/ -v --ignore=tests/test_cli.py

# Run specific test file or test
.venv/bin/python -m pytest tests/test_sync.py -v
.venv/bin/python -m pytest tests/test_sync.py::TestSyncService::test_sync_once_new_documents -v

# Lint
ruff check src tests

# Format
ruff format src tests

# CLI entry point
granola-sync --help
```

## Architecture

**Data flow:** CLI → SyncService → GranolaClient (API) / GranolaCacheReader (fallback) → WebhookSender

### Key components

- **`sync.py` — SyncService**: Orchestrates sync cycles. Resolves folders, fetches documents (API-first, cache fallback), gates on content availability, sends webhooks. Documents without notes are marked `pending_content` and re-checked next cycle.
- **`granola_api.py` — GranolaClient + GranolaCacheReader**: API client with auto token refresh. Cache reader handles `cache-v4.json` (fallback to v3) for folder ID resolution and document fallback.
- **`state.py` — StateManager**: JSON-based persistent state tracking synced/failed/pending documents and folder ID mappings. Auto-migrates v1 → v2 schema.
- **`webhook.py` — WebhookSender**: HMAC-SHA256 signed payloads with configurable retry/backoff.
- **`config.py`**: Pydantic models validated from `~/.granola-sync/config.yaml`.
- **`cli.py`**: Typer-based CLI with commands: `config`, `run`, `sync-once`, `status`, `start`, `stop`.

### Folder resolution priority
1. Explicit `folder_ids` in config (highest)
2. Persisted `folder_map` in state.json
3. Cache file lookup (updates state for next time)

### Granola API status
- `POST /v2/get-documents` — works, primary data source
- `POST /v1/get-document-transcript` — works
- `POST /v1/refresh-access-token` — works
- `GET /v2/get-document-lists` — **broken (HTTP 500)**, folder IDs resolved via cache + state

## Testing patterns

- Async tests use `@pytest.mark.asyncio` with `pytest-asyncio`
- HTTP mocking via `respx`
- Temp files via `tmp_path` fixture
- `_make_doc()` helper for creating test document dicts
- `test_cli.py::test_run_no_config` hangs — always skip CLI tests with `--ignore=tests/test_cli.py`

## Runtime paths

- Config: `~/.granola-sync/config.yaml` (mode 0600)
- State: `~/.granola-sync/state.json`
- Granola data (macOS): `~/Library/Application Support/Granola/` — `supabase.json` (auth), `cache-v4.json` (cache)
