# Implementation Strategy: API-First Sync with Cache Fallback

**Date:** 2026-03-03
**Status:** Approved
**Problem:** `cache-v4.json` is not reliably updated by the Granola desktop app — it can lag behind by 2+ days, causing recent meetings to be missed entirely.

---

## Context & Findings

### Current State
- `cache-v4.json` last modified: **March 1** (2 days stale)
- Most recent meeting in API: **March 3** (today, real-time)
- `GET /v2/get-document-lists` API: **broken** (returns HTTP 500)
- `POST /v2/get-documents` API: **works** — returns real-time data, supports `list_id` filtering
- `POST /v1/get-document-transcript` API: **works** — returns full transcript immediately

### API Document Schema (from /v2/get-documents)
Documents returned by the API include:
- `id`, `title`, `created_at`, `updated_at`
- `notes_markdown` — pre-rendered markdown (empty for recent/in-progress meetings)
- `notes_plain` — plain text version (empty for recent/in-progress meetings)
- `notes` — ProseMirror dict (always present, but may have empty content for very recent meetings)
- `people` — attendees with `name`, `email`, and `details`
- `google_calendar_event` — calendar event metadata
- `status`, `type`, `workspace_id`
- Does **not** include folder/list membership info

### Key Insight
The API's `POST /v2/get-documents` endpoint accepts a `list_id` parameter to filter by folder. The folder ID mapping can be resolved from `cache-v4.json` and persisted in state.

---

## Decisions

| Decision | Choice |
|---|---|
| **Primary data source** | API-first (`/v2/get-documents`) with cache fallback |
| **Folder ID resolution** | Cache-based folder map with persistent state + config override |
| **Empty content handling** | Skip docs with no content, mark as `pending_content`, re-check later |
| **Re-sync on update** | Yes — track `updated_at` and re-sync when changed |

---

## Architecture Changes

### 1. New Data Flow

```
┌─────────────────────────────────────────────────────┐
│                    sync_once()                       │
├─────────────────────────────────────────────────────┤
│                                                      │
│  1. Resolve folder IDs                               │
│     ├─ Check config.yaml for explicit folder_ids     │
│     ├─ Check state.json folder_map                   │
│     └─ Read cache-v4.json → update folder_map        │
│                                                      │
│  2. For each folder:                                 │
│     └─ API: POST /v2/get-documents {list_id}         │
│        ├─ On success: use API documents              │
│        └─ On failure: fallback to cache documents    │
│                                                      │
│  3. Filter documents                                 │
│     ├─ New docs (not in state.seen_documents)        │
│     ├─ Updated docs (updated_at changed)             │
│     └─ Pending docs (previously had no content)      │
│                                                      │
│  4. Content check                                    │
│     ├─ Has notes_markdown or ProseMirror text?       │
│     │   → Process and send webhook                   │
│     └─ No content yet?                               │
│         → Mark as pending_content, skip for now      │
│                                                      │
│  5. For each processable document:                   │
│     ├─ Fetch transcript (optional)                   │
│     ├─ Build payload                                 │
│     ├─ Send webhook                                  │
│     └─ Update state                                  │
└─────────────────────────────────────────────────────┘
```

### 2. Changes to `granola_api.py`

#### `GranolaClient.get_folders()` → Refactored
- **Remove** the current "cache-first, API-fallback" approach for getting folders
- **New**: Resolve folder IDs from multiple sources (config, state, cache)
- **New**: Add `get_documents_for_folder(list_id)` method that calls the API directly

#### `GranolaClient.get_documents_by_folder(list_id, limit, offset)` → New method
```python
async def get_documents_by_folder(self, list_id: str, limit: int = 100, offset: int = 0):
    """Fetch documents for a specific folder via API."""
    response = await client.post("/v2/get-documents", json={
        "list_id": list_id,
        "limit": limit,
        "offset": offset,
    })
    return response.json().get("docs", [])
```

#### `GranolaCacheReader` → Kept as fallback
- Add `get_folder_map()` method that returns `{folder_title: folder_id}` mapping
- Used only for folder ID discovery and as document content fallback

### 3. Changes to `sync.py`

#### `SyncService.sync_once()` → Refactored
```python
async def sync_once(self, dry_run=False):
    # 1. Resolve folder name → ID mapping
    folder_map = self._resolve_folder_map()

    # 2. For each configured folder
    for folder_name in self.config.granola.folders:
        folder_id = folder_map.get(folder_name)
        if not folder_id:
            logger.warning("folder_id_not_found", name=folder_name)
            continue

        # 3. Fetch documents from API (with cache fallback)
        documents = await self._fetch_folder_documents(folder_name, folder_id)

        # 4. Filter + process
        await self._sync_documents(folder_name, documents, dry_run)

    # 5. Re-check pending_content documents
    await self._recheck_pending_documents(dry_run)
```

#### `SyncService._resolve_folder_map()` → New method
```python
def _resolve_folder_map(self) -> dict[str, str]:
    """Resolve folder names to IDs from multiple sources."""
    folder_map = {}

    # Priority 1: Explicit IDs from config.yaml
    for entry in self.config.granola.folder_ids or []:
        folder_map[entry["name"]] = entry["id"]

    # Priority 2: Cached mapping from state.json
    folder_map.update(self.state.get_folder_map())

    # Priority 3: Read from cache-v4.json (if available)
    try:
        cache = GranolaCacheReader()
        cache_map = cache.get_folder_map()
        folder_map.update(cache_map)
        # Persist for future use
        self.state.update_folder_map(cache_map)
    except Exception:
        pass

    return folder_map
```

#### `SyncService._fetch_folder_documents()` → New method
```python
async def _fetch_folder_documents(self, folder_name, folder_id):
    """Fetch documents for a folder, API-first with cache fallback."""
    try:
        return await self.granola.get_documents_by_folder(folder_id)
    except Exception as e:
        logger.warning("api_fetch_failed_using_cache", error=str(e))
        cache = GranolaCacheReader()
        return cache.get_documents_for_folder(folder_name)
```

#### `SyncService._has_content()` → New method
```python
def _has_content(self, doc: dict) -> bool:
    """Check if a document has meaningful note content."""
    if doc.get("notes_markdown"):
        return True
    if doc.get("notes_plain"):
        return True
    notes = doc.get("notes", {})
    if isinstance(notes, dict):
        # Check ProseMirror for actual text content
        return self._prosemirror_has_text(notes)
    return False
```

#### `SyncService._recheck_pending_documents()` → New method
```python
async def _recheck_pending_documents(self, dry_run=False):
    """Re-check documents that were previously skipped due to missing content."""
    pending = self.state.get_pending_documents()
    for doc_info in pending:
        # Re-fetch from API
        docs = await self.granola.get_documents(limit=1, doc_id=doc_info["id"])
        if docs and self._has_content(docs[0]):
            await self._process_document(docs[0], doc_info["folder_name"])
            self.state.clear_pending(doc_info["id"])
```

### 4. Changes to `state.py`

Add new state fields:

```python
{
    "version": 2,  # Bump version
    "folder_map": {
        "SQP": "25168602-d16e-478c-8345-fd308ad5959c",
        "CAL": "43158371-119d-4add-aa8a-867fb6263fc8"
    },
    "pending_documents": {
        "doc_id": {
            "title": "Meeting Title",
            "folder_name": "SQP",
            "first_seen": "2026-03-03T10:00:00Z",
            "last_checked": "2026-03-03T10:02:00Z",
            "check_count": 3
        }
    },
    "seen_documents": { ... },  // existing
    "failed_documents": { ... }  // existing
}
```

### 5. Changes to `config.py`

Add optional `folder_ids` config:

```yaml
granola:
  folders: ["SQP", "CAL"]
  # Optional: explicit folder ID overrides (bypasses cache-based resolution)
  folder_ids:
    SQP: "25168602-d16e-478c-8345-fd308ad5959c"
    CAL: "43158371-119d-4add-aa8a-867fb6263fc8"
  include_transcript: true
```

---

## Implementation Order

1. **`granola_api.py`** — Add `get_documents_by_folder()` method and `get_folder_map()` to cache reader
2. **`state.py`** — Add `folder_map` and `pending_documents` to state schema
3. **`config.py`** — Add optional `folder_ids` config field
4. **`sync.py`** — Refactor `sync_once()` to use API-first flow with content checking and pending re-check
5. **Tests** — Update existing tests and add tests for new behavior
6. **README** — Document the new `folder_ids` config option

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| API rate limiting | Keep batch_size config; add backoff on 429 responses |
| API goes down entirely | Cache fallback preserves existing behavior |
| Folder IDs change (Granola update) | Cache auto-refreshes the mapping; config override available |
| Pending docs never get content | Add `max_pending_age` config (default 7 days) to expire stale pending docs |
| Token expiration during long sync | Existing token refresh logic handles this |

---

## What Stays the Same

- Webhook payload format (no breaking changes for consumers)
- Transcript fetching via `/v1/get-document-transcript`
- HMAC-SHA256 webhook signing
- CLI commands and service management
- State file location and general structure (additive changes only)
- ProseMirror → text conversion logic
