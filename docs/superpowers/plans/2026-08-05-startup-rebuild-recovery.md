# Startup Rebuild Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent one unreadable `ready` asset from aborting application startup while preserving a valid index for every other readable asset, then launch the local FastAPI UI inside a Tauri desktop window.

**Architecture:** `KnowledgeBase.rebuild()` will convert exceptions raised while resolving or parsing a specific asset into a typed error that carries only the asset ID. `rebuild_ready_assets()` will catch only that typed error, remove the failed asset's old chunks, publish its existing safe failure state, and retry with the remaining `ready` assets; infrastructure errors outside an asset boundary will still abort. Tauri development will use a command-line config overlay that points both `devUrl` and the main window at the local FastAPI server without editing production configuration.

**Tech Stack:** Python 3.13, FastAPI, Pytest 8, Tauri 2.11, Rust 1.97, React 19, Vite 8, macOS system WebView.

## Global Constraints

- Preserve all readable assets and their retrieval chunks.
- Do not delete asset records or S3 objects.
- Publish the safe existing error text `文件解析或预览生成失败`; do not expose paths, object keys, or parser errors.
- Catch only asset-scoped rebuild failures; unrelated index persistence and program errors must still propagate.
- Keep the entire recovery loop inside `KnowledgeBase.rebuild_transaction()`.
- Do not change `frontend/src-tauri/tauri.conf.json`, its remote production URL, bundle targets, CSP, signing, or icons.
- Follow TDD: observe the regression test fail for the startup exception before changing production code.

---

### Task 1: Isolate an unreadable ready asset during startup rebuild

**Files:**
- Modify: `tests/test_status_runtime.py:79-136`
- Modify: `app/knowledge_base.py:25-40,157-190`
- Modify: `app/main.py:15,160-170`

**Interfaces:**
- Consumes: `KnowledgeBase.rebuild(assets, path_for)`, `KnowledgeBase.remove_asset(asset_id)`, `AssetStore.publish_failed(asset_id, chunk_counts, error)`, and `AssetStore.ready_current_assets()`.
- Produces: `AssetIndexingError.asset_id: str` and a recovery loop in `rebuild_ready_assets() -> Dict[str, int]`.

- [ ] **Step 1: Write the failing startup regression test**

Add this test after the existing startup rebuild tests in `tests/test_status_runtime.py`:

```python
def test_startup_quarantines_unreadable_ready_asset_and_rebuilds_remaining(
    monkeypatch,
    tmp_path,
):
    settings, asset_store, knowledge_base, processor = build_startup_runtime(tmp_path)
    readable_source = settings.knowledge_dir / "readable.md"
    missing_source = settings.knowledge_dir / "missing.md"
    readable_source.write_text("仍然可检索的制度规定住宿上限五百元。", encoding="utf-8")
    missing_source.write_text("即将丢失的资料。", encoding="utf-8")
    readable = asset_store.create("可读制度.md", readable_source.name)
    missing = asset_store.create("缺失制度.md", missing_source.name)
    processor.process(readable.id)
    processor.process(missing.id)
    missing_source.unlink()
    knowledge_base.chunks = [
        chunk for chunk in knowledge_base.chunks if chunk.asset_id == readable.id
    ][:-1]
    patch_startup_services(monkeypatch, asset_store, knowledge_base, processor)
    assert main.index_state_requires_rebuild() is True

    main.startup()

    failed = asset_store.get(missing.id)
    preserved = asset_store.get(readable.id)
    assert failed.status == "failed"
    assert failed.error == "文件解析或预览生成失败"
    assert "missing.md" not in failed.error
    assert failed.chunk_count == 0
    assert preserved.status == "ready"
    assert preserved.chunk_count == knowledge_base.count_for_asset(readable.id) > 0
    assert knowledge_base.search("住宿上限", allowed_asset_ids={readable.id})
    assert knowledge_base.count_for_asset(missing.id) == 0
```

The explicit `index_state_requires_rebuild()` assertion protects the setup from accidentally bypassing the production startup decision; the removed readable chunk ensures the rebuild path is entered even though the missing asset's old chunk was previously valid.

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_status_runtime.py::test_startup_quarantines_unreadable_ready_asset_and_rebuilds_remaining
```

Expected: FAIL with `FileNotFoundError: 资产文件不存在且未配置 S3`, proving the test reaches the same failing boundary as the real S3 startup.

- [ ] **Step 3: Add the typed asset-scoped rebuild error**

In `app/knowledge_base.py`, add this class beside the existing data types:

```python
class AssetIndexingError(RuntimeError):
    """Identify an asset that could not participate in an atomic index rebuild."""

    def __init__(self, asset_id: str):
        super().__init__("资产索引重建失败")
        self.asset_id = asset_id
```

Then wrap only the per-asset path and segment construction in `KnowledgeBase.rebuild()`:

```python
for asset in assets:
    try:
        source_path = path_for(asset)
        segments = list(
            self._asset_segments(
                asset,
                source_path,
                pipeline["pdf_chunk_scope"],
                pipeline["image_index_mode"],
            )
        )
    except Exception as exc:
        raise AssetIndexingError(asset.id) from exc
    chunk_no = 0
    for page, source_text in segments:
        for chunk_text in split_text(
            source_text,
            pipeline["chunk_size"],
            pipeline["chunk_overlap"],
            pipeline["boundary_mode"],
        ):
            chunk_no += 1
            digest = sha256(
                f"{asset.id}:{page}:{chunk_no}:{chunk_text}".encode("utf-8")
            ).hexdigest()[:16]
            rebuilt.append(
                Chunk(
                    digest,
                    asset.id,
                    asset.original_name,
                    chunk_no,
                    page,
                    chunk_text,
                )
            )
```

Do not wrap the final index write or MySQL synchronization; those errors are not attributable to one asset and must remain fatal.

- [ ] **Step 4: Recover only the identified asset and retry**

Import the new exception in `app/main.py`:

```python
from app.knowledge_base import AssetIndexingError, KnowledgeBase
```

Replace `rebuild_ready_assets()` with:

```python
def rebuild_ready_assets() -> Dict[str, int]:
    with knowledge_base.rebuild_transaction():
        while True:
            ready_assets = asset_store.ready_current_assets()
            try:
                rebuilt = knowledge_base.rebuild(ready_assets, asset_store.path_for)
            except AssetIndexingError as exc:
                failed_asset = asset_store.get(exc.asset_id)
                if failed_asset is None or failed_asset.status != "ready":
                    raise
                knowledge_base.remove_asset(exc.asset_id)
                asset_store.publish_failed(
                    exc.asset_id,
                    knowledge_base.chunk_counts(),
                    "文件解析或预览生成失败",
                )
                continue
            asset_store.sync_chunk_counts(knowledge_base.chunk_counts())
            return rebuilt
```

The status transition guarantees each loop removes one candidate. The existing re-entrant rebuild lock keeps this compatible with callers that already hold the transaction.

- [ ] **Step 5: Run the focused regression and startup module tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_status_runtime.py::test_startup_quarantines_unreadable_ready_asset_and_rebuilds_remaining \
  tests/test_status_runtime.py
```

Expected: the focused regression and all status/runtime tests pass.

- [ ] **Step 6: Run knowledge-base regressions**

Run:

```bash
.venv/bin/pytest -q tests/test_knowledge_base.py
```

Expected: all processor, publication, concurrency, and retrieval tests pass.

- [ ] **Step 7: Commit the startup recovery**

```bash
git add app/knowledge_base.py app/main.py tests/test_status_runtime.py
git commit -m "fix: isolate unreadable assets during startup rebuild"
```

---

### Task 2: Verify the repository and launch the local desktop app

**Files:**
- Verify only: `frontend/src-tauri/tauri.conf.json`
- Observe: `frontend/src-tauri/target/debug/app`

**Interfaces:**
- Consumes: FastAPI at `http://127.0.0.1:8000`, the committed `frontend/dist`, and Tauri CLI `--config` JSON overlay support.
- Produces: a running local FastAPI process and a running Tauri `Knowledge Helper` desktop process whose WebView loads the local origin.

- [ ] **Step 1: Run the full regression suite**

Run these independent commands:

```bash
.venv/bin/pytest -q
(cd frontend && node --test tests/*.test.mjs && npm run lint && npm run build)
(cd agent_sdk && npm run check)
```

Expected: 0 failures; Vite may report its existing large-chunk warning, and Python may report the existing FastAPI/SWIG deprecation warnings.

- [ ] **Step 2: Confirm ports and start the backend**

Verify ports `8000` and `5173` are not occupied, then run from the repository root:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Expected: application startup completes and Uvicorn reports listening on `http://127.0.0.1:8000`.

- [ ] **Step 3: Verify the backend and local frontend origin**

Run:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/status
curl --fail --silent --show-error http://127.0.0.1:8000/
```

Expected: status JSON and the Vite-built HTML document are returned with HTTP 200.

- [ ] **Step 4: Start Tauri with a non-persistent local override**

From `frontend/`, run:

```bash
npm exec tauri -- dev --config '{
  "build": {"devUrl": "http://127.0.0.1:8000"},
  "app": {
    "windows": [{
      "label": "main",
      "url": "http://127.0.0.1:8000",
      "title": "知识库助手（本地）",
      "width": 1200,
      "height": 800,
      "minWidth": 840,
      "minHeight": 560
    }]
  }
}'
```

Expected: Cargo builds or reuses `frontend/src-tauri/target/debug/app`; the process stays alive and macOS shows the Knowledge Helper native window. The command-line overlay is ephemeral and `frontend/src-tauri/tauri.conf.json` remains unchanged.

- [ ] **Step 5: Verify process, URL response, and repository hygiene**

Run:

```bash
pgrep -fl 'target/debug/app'
curl --fail --silent --show-error http://127.0.0.1:8000/api/status
git diff --check
git status --short --branch
```

Expected: the Tauri process and backend are running, status remains HTTP 200, there are no whitespace errors, and only the planned commits differ from the base branch.
