# File Upload Completion Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add truthful per-file upload progress that reaches “可用” only after primary-storage persistence and successful indexing, then merges into a size-aware asset card with short accessible motion.

**Architecture:** Keep `POST /api/upload` asynchronous after durable storage, persist `size_bytes` with each asset, and make `AssetProcessor` publish `ready` only after rebuilding the index with an unpublished candidate asset. The React client owns short bounded progress, polls the existing project asset endpoint, and swaps the completed feedback row for the server-backed asset card.

**Tech Stack:** Python 3.11+ production runtime, FastAPI, Pytest 8, MySQL/PyMySQL, optional S3/Boto3, React 19, TypeScript 6, HeroUI, CSS, Node test runner, Vite.

## Global Constraints

- “可用” means the file is persisted in the configured primary storage and its required preview/parsing/index rebuild has succeeded.
- With S3 configured, `put_object` must succeed before `/api/upload` creates the asset; local filesystem fallback remains supported for development.
- Progress may advance quickly but must stop at 92% until the server returns `status="ready"`.
- Use only `transform` and `opacity` for upload-record and asset-card motion; durations must stay between 160ms and 180ms, with movement no greater than 6px.
- `prefers-reduced-motion: reduce` must remove transforms and transitions without hiding status, percentage, errors, or the final asset.
- Do not add chunked upload, resumable upload, byte-level browser progress, WebSocket, upload SSE, or a task table.
- Preserve support for TXT, Markdown, PDF, PNG, JPG, JPEG, and WebP.
- Existing assets without a stored byte size must load as `size_bytes=0` and display “大小未知”.
- Follow TDD: every production behavior starts with a focused failing test that is observed failing for the expected reason.

---

### Task 1: Persist and expose exact file size

**Files:**
- Modify: `app/assets.py:64-84,142-170,217-250,421-449,459-514,544-578`
- Modify: `app/main.py:633-662,377-396`
- Modify: `tests/test_s3_storage.py`
- Modify: `tests/test_status_runtime.py`

**Interfaces:**
- Consumes: the byte payload already read by `store_upload_payload()` and the existing JSON/MySQL asset stores.
- Produces: `Asset.size_bytes: int`, `AssetStore.create(..., size_bytes: int = 0)`, `AssetStore.replace(..., size_bytes: int = 0)`, and public API field `size_bytes`.
- Produces: `store_upload_payload(file) -> Tuple[str, str, str, int]`, where the final item is the exact payload length.

- [ ] **Step 1: Write failing local-persistence and public-payload tests**

Append to `tests/test_s3_storage.py`:

```python
def test_asset_size_is_persisted_and_exposed_in_public_payload(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        assets_path=tmp_path / "data" / "assets.json",
        mysql_host="",
        mysql_password="",
    )
    store = AssetStore(settings)

    created = store.create("guide.md", "guide.md", size_bytes=2_048)
    replacement = store.replace(
        created.id,
        "guide-v2.md",
        "guide-v2.md",
        "hash-v2",
        size_bytes=4_096,
    )
    reloaded = AssetStore(settings)
    reloaded.load()

    assert created.size_bytes == 2_048
    assert reloaded.get(created.id).size_bytes == 2_048
    assert reloaded.get(replacement.id).size_bytes == 4_096
    assert reloaded.public(reloaded.get(created.id))["size_bytes"] == 2_048


def test_legacy_asset_without_size_loads_as_unknown_size(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        assets_path=tmp_path / "data" / "assets.json",
        mysql_host="",
        mysql_password="",
    )
    settings.data_dir.mkdir(parents=True)
    settings.assets_path.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "id": "legacy",
                        "original_name": "legacy.md",
                        "stored_name": "legacy.md",
                        "media_type": "text/markdown",
                        "status": "ready",
                        "created_at": "2026-08-04T00:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    store = AssetStore(settings)
    store.load()

    assert store.get("legacy").size_bytes == 0
```

- [ ] **Step 2: Run the persistence tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_s3_storage.py::test_asset_size_is_persisted_and_exposed_in_public_payload \
  tests/test_s3_storage.py::test_legacy_asset_without_size_loads_as_unknown_size
```

Expected: FAIL because `AssetStore.create()` does not accept `size_bytes` and `Asset` has no `size_bytes` attribute.

- [ ] **Step 3: Write a failing test for the upload payload length**

Add these imports to `tests/test_status_runtime.py`:

```python
import asyncio
from io import BytesIO

from fastapi import UploadFile
```

Append:

```python
def test_store_upload_payload_returns_exact_file_size(monkeypatch, tmp_path):
    from app import main as app_main

    test_settings = app_main.settings.__class__(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        mysql_host="",
        mysql_password="",
    )
    monkeypatch.setattr(app_main, "settings", test_settings)
    monkeypatch.setattr(app_main, "s3_storage", None)
    upload = UploadFile(filename="guide.md", file=BytesIO(b"hello"))

    original_name, stored_name, content_hash, size_bytes = asyncio.run(
        app_main.store_upload_payload(upload)
    )

    assert original_name == "guide.md"
    assert stored_name.endswith("_guide.md")
    assert len(content_hash) == 64
    assert size_bytes == 5
```

- [ ] **Step 4: Run the upload-payload test and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_status_runtime.py::test_store_upload_payload_returns_exact_file_size
```

Expected: FAIL with unpacking error because `store_upload_payload()` still returns three values.

- [ ] **Step 5: Implement local size persistence and upload propagation**

In `app/assets.py`, add the dataclass field and parameters:

```python
@dataclass(frozen=True)
class Asset:
    id: str
    original_name: str
    stored_name: str
    media_type: str
    status: str
    created_at: str
    project_id: str = "local-default"
    page_count: int = 0
    chunk_count: int = 0
    size_bytes: int = 0
    error: str = ""
    tags: List[str] = field(default_factory=list)
    description: str = ""
    content_hash: str = ""
    version_group_id: str = ""
    version_no: int = 1
    is_current_version: bool = True
    replaces_asset_id: str = ""
    vision_status: str = "unavailable"
    visual_segments: List[Dict[str, object]] = field(default_factory=list)
```

Add the final `size_bytes` parameter to `create()` and pass its normalized value into the existing `Asset(...)` constructor:

```python
def create(
    self,
    original_name: str,
    stored_name: str,
    project_id: str = "local-default",
    content_hash: str = "",
    version_group_id: Optional[str] = None,
    version_no: int = 1,
    replaces_asset_id: str = "",
    size_bytes: int = 0,
) -> Asset:
```

The resulting constructor is:

```python
asset = Asset(
    id=asset_id,
    original_name=self._clean_name(original_name),
    stored_name=stored_name,
    media_type=asset_media_type(suffix),
    status="queued",
    created_at=datetime.now(timezone.utc).isoformat(),
    project_id=project_id,
    size_bytes=max(0, int(size_bytes)),
    content_hash=str(content_hash or ""),
    version_group_id=version_group_id or asset_id,
    version_no=max(1, int(version_no)),
    replaces_asset_id=str(replaces_asset_id or ""),
)
```

Add the final `size_bytes` parameter to `replace()`:

```python
def replace(
    self,
    asset_id: str,
    original_name: str,
    stored_name: str,
    content_hash: str,
    size_bytes: int = 0,
) -> Asset:
```

The resulting replacement constructor is:

```python
replacement = Asset(
    id=asset_id_new,
    original_name=self._clean_name(original_name),
    stored_name=stored_name,
    media_type=asset_media_type(Path(stored_name).suffix.lower()),
    status="queued",
    created_at=datetime.now(timezone.utc).isoformat(),
    project_id=current.project_id,
    size_bytes=max(0, int(size_bytes)),
    tags=current.tags,
    description=current.description,
    content_hash=str(content_hash or ""),
    version_group_id=current.version_group_id or current.id,
    version_no=max(
        [
            candidate.version_no
            for candidate in self._assets.values()
            if candidate.version_group_id == (current.version_group_id or current.id)
        ]
        or [current.version_no]
    )
    + 1,
    replaces_asset_id=current.id,
)
```

Add `"size_bytes": asset.size_bytes` to `AssetStore.public()` and add this argument to `_asset_from_payload()`:

```python
size_bytes=max(0, int(payload.get("size_bytes") or 0)),
```

In `app/main.py`, change the function annotation and final return statement exactly as follows; the validation and S3/local storage branch above the return are not otherwise changed:

```python
async def store_upload_payload(file: UploadFile) -> Tuple[str, str, str, int]:
    return safe_name, stored_name, sha256(payload).hexdigest(), len(payload)
```

```python
safe_name, stored_name, content_hash, size_bytes = await store_upload_payload(file)
asset = asset_store.create(
    safe_name,
    stored_name,
    project_id,
    content_hash=content_hash,
    size_bytes=size_bytes,
)
```

Use the same four-value unpacking in `replace_asset()` and call:

```python
replacement = asset_store.replace(
    asset_id,
    original_name,
    stored_name,
    content_hash,
    size_bytes=size_bytes,
)
```

- [ ] **Step 6: Run the local persistence and upload tests and confirm GREEN**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_s3_storage.py::test_asset_size_is_persisted_and_exposed_in_public_payload \
  tests/test_s3_storage.py::test_legacy_asset_without_size_loads_as_unknown_size \
  tests/test_status_runtime.py::test_store_upload_payload_returns_exact_file_size
```

Expected: 3 passed.

- [ ] **Step 7: Write a failing MySQL schema/sync contract test**

Append to `tests/test_s3_storage.py`:

```python
def test_asset_mysql_schema_and_sync_include_size_bytes(tmp_path, monkeypatch):
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, statement, params=None):
            statements.append((statement, params))

        def fetchone(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        assets_path=tmp_path / "data" / "assets.json",
        mysql_host="mysql.example",
        mysql_password="secret",
    )
    monkeypatch.setattr("app.assets._get_mysql", lambda _: Connection())
    store = AssetStore(settings)

    store.create("guide.md", "guide.md", size_bytes=4_096)

    assert any("ADD COLUMN size_bytes" in sql for sql, _ in statements)
    insert_sql, insert_params = next(
        (sql, params)
        for sql, params in statements
        if sql.startswith("INSERT INTO knowledge_assets")
    )
    assert "size_bytes" in insert_sql
    assert insert_params[-1] == 4_096


def test_asset_mysql_load_restores_size_bytes(tmp_path, monkeypatch):
    mysql_row = (
        "mysql-asset",
        "guide.md",
        "guide.md",
        "text/markdown",
        "ready",
        "2026-08-04T00:00:00+00:00",
        "local-default",
        0,
        2,
        "",
        "[]",
        "",
        "hash",
        "mysql-asset",
        1,
        1,
        "",
        "unavailable",
        "[]",
        8_192,
    )

    class Cursor:
        def __init__(self):
            self.statement = ""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, statement, params=None):
            self.statement = statement

        def fetchone(self):
            return ("size_bytes",)

        def fetchall(self):
            return [mysql_row] if self.statement.startswith("SELECT id") else []

    class Connection:
        def cursor(self):
            return Cursor()

    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        assets_path=tmp_path / "data" / "assets.json",
        mysql_host="mysql.example",
        mysql_password="secret",
    )
    monkeypatch.setattr("app.assets._get_mysql", lambda _: Connection())
    store = AssetStore(settings)

    store._load_from_mysql_locked()

    assert store.get("mysql-asset").size_bytes == 8_192
```

- [ ] **Step 8: Run the MySQL contract test and confirm RED**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_s3_storage.py::test_asset_mysql_schema_and_sync_include_size_bytes \
  tests/test_s3_storage.py::test_asset_mysql_load_restores_size_bytes
```

Expected: both tests fail because the schema/INSERT and explicit SELECT/row mapping do not include `size_bytes`.

- [ ] **Step 9: Implement the additive MySQL migration and explicit column order**

Add to `AssetStore`:

```python
@staticmethod
def _ensure_mysql_schema_locked(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM knowledge_assets LIKE 'size_bytes'")
        if cur.fetchone() is None:
            cur.execute(
                "ALTER TABLE knowledge_assets "
                "ADD COLUMN size_bytes BIGINT NOT NULL DEFAULT 0"
            )
```

Call `_ensure_mysql_schema_locked(conn)` before opening the main cursor in both `_sync_to_mysql_locked()` and `_load_from_mysql_locked()`.

Change the INSERT tail to:

```python
"replaces_asset_id, vision_status, visual_segments, size_bytes) "
"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
```

Append `asset.size_bytes` to the parameter tuple. Replace `SELECT *` with this explicit order:

```python
cur.execute(
    "SELECT id, original_name, stored_name, media_type, status, created_at, "
    "project_id, page_count, chunk_count, error, tags, description, content_hash, "
    "version_group_id, version_no, is_current_version, replaces_asset_id, "
    "vision_status, visual_segments, size_bytes FROM knowledge_assets"
)
```

When constructing the MySQL-loaded `Asset`, add:

```python
size_bytes=max(0, int(row[19] or 0)),
```

- [ ] **Step 10: Run focused and surrounding backend tests**

Run:

```bash
.venv/bin/pytest -q tests/test_s3_storage.py tests/test_status_runtime.py
```

Expected: all tests pass.

- [ ] **Step 11: Commit Task 1**

```bash
git add app/assets.py app/main.py tests/test_s3_storage.py tests/test_status_runtime.py
git commit -m "feat: persist uploaded file sizes"
```

---

### Task 2: Publish `ready` only after successful indexing

**Files:**
- Modify: `app/ingestion.py:1-49`
- Modify: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `AssetStore.ready_current_assets()`, `KnowledgeBase.rebuild(assets, path_for)`, and immutable `Asset` values.
- Produces: strict external invariant `asset.status == "ready"` implies the current index rebuild has completed and `chunk_count` is final.

- [ ] **Step 1: Write a failing publication-order test**

Append to `tests/test_knowledge_base.py`:

```python
def test_processor_publishes_ready_only_after_index_rebuild(tmp_path: Path, monkeypatch):
    settings, asset_store, knowledge_base, processor, _ = build_runtime(tmp_path)
    source = settings.knowledge_dir / "policy.md"
    source.write_text("差旅住宿标准为每晚五百元。", encoding="utf-8")
    asset = asset_store.create(
        "差旅制度.md",
        source.name,
        size_bytes=source.stat().st_size,
    )
    observed_published_statuses = []
    original_rebuild = knowledge_base.rebuild

    def inspect_rebuild(candidates, path_for):
        observed_published_statuses.append(asset_store.get(asset.id).status)
        unpublished_candidate = next(item for item in candidates if item.id == asset.id)
        assert unpublished_candidate.status == "ready"
        return original_rebuild(candidates, path_for)

    monkeypatch.setattr(knowledge_base, "rebuild", inspect_rebuild)

    processor.process(asset.id)

    processed = asset_store.get(asset.id)
    assert observed_published_statuses == ["processing"]
    assert processed.status == "ready"
    assert processed.chunk_count == knowledge_base.count_for_asset(asset.id)
    assert processed.chunk_count > 0
```

- [ ] **Step 2: Run the order test and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_knowledge_base.py::test_processor_publishes_ready_only_after_index_rebuild
```

Expected: FAIL because the currently published status observed inside `rebuild()` is `ready`, not `processing`.

- [ ] **Step 3: Add a failure-state regression test**

Append:

```python
def test_processor_marks_asset_failed_when_index_rebuild_fails(tmp_path: Path, monkeypatch):
    settings, asset_store, knowledge_base, processor, _ = build_runtime(tmp_path)
    source = settings.knowledge_dir / "policy.md"
    source.write_text("差旅住宿标准为每晚五百元。", encoding="utf-8")
    asset = asset_store.create("差旅制度.md", source.name)

    def fail_rebuild(*_):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(knowledge_base, "rebuild", fail_rebuild)

    processor.process(asset.id)

    processed = asset_store.get(asset.id)
    assert processed.status == "failed"
    assert processed.error == "文件解析或预览生成失败"
```

- [ ] **Step 4: Implement unpublished candidate indexing**

Change the first import in `app/ingestion.py` to:

```python
from dataclasses import replace
from pathlib import Path
```

Replace the successful middle of `AssetProcessor.process()` with:

```python
ready_candidate = replace(
    asset,
    status="ready",
    page_count=page_count,
    error="",
    vision_status=vision_status,
    visual_segments=visual_segments,
)
index_assets = [
    candidate
    for candidate in self.asset_store.ready_current_assets()
    if candidate.id != ready_candidate.id
]
if ready_candidate.is_current_version:
    index_assets.append(ready_candidate)
self.knowledge_base.rebuild(index_assets, self.asset_store.path_for)
chunk_count = (
    self.knowledge_base.count_for_asset(ready_candidate.id)
    if ready_candidate.is_current_version
    else 0
)
self.asset_store.update(
    asset.id,
    status="ready",
    page_count=page_count,
    chunk_count=chunk_count,
    error="",
    vision_status=vision_status,
    visual_segments=visual_segments,
)
```

Delete the old pre-rebuild `status="ready"` update and the separate post-rebuild `chunk_count` update. Retain `cleanup_local()` in the existing `finally` block and retain the outer failure handler without changing its error message.

- [ ] **Step 5: Run focused processor tests and confirm GREEN**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_knowledge_base.py::test_processor_publishes_ready_only_after_index_rebuild \
  tests/test_knowledge_base.py::test_processor_marks_asset_failed_when_index_rebuild_fails \
  tests/test_knowledge_base.py::test_markdown_is_indexed_and_returned_as_attachment \
  tests/test_knowledge_base.py::test_asset_metadata_versions_and_current_retrieval_selection
```

Expected: 4 passed.

- [ ] **Step 6: Run the complete knowledge-base test module**

```bash
.venv/bin/pytest -q tests/test_knowledge_base.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add app/ingestion.py tests/test_knowledge_base.py
git commit -m "fix: publish assets after indexing"
```

---

### Task 3: Add per-file feedback, truthful polling, and accessible motion

**Files:**
- Create: `frontend/tests/upload-completion-feedback.test.mjs`
- Modify: `frontend/src/App.tsx:93-113,356-380,868-1055,1058-1100,1285-1303,1505-1530,2314-2338,2650-2700`
- Modify: `frontend/src/App.css:670-720,988-1001`

**Interfaces:**
- Consumes: public `Asset.size_bytes`, `POST /api/upload`, and `GET /api/assets?project_id=...` statuses `queued`, `processing`, `ready`, and `failed`.
- Produces: `UploadFeedback` view model, `formatFileSize(sizeBytes)`, `UploadFeedbackList`, bounded progress, and `AssetCard.isNew` entrance state.

- [ ] **Step 1: Create a failing frontend behavior/style contract test**

Create `frontend/tests/upload-completion-feedback.test.mjs`:

```javascript
import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const stylePath = new URL("../src/App.css", import.meta.url);

test("知识库上传反馈以服务端 ready 作为 100% 和可用条件", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /type UploadFeedback = \{/);
  assert.match(source, /size_bytes: number/);
  assert.match(source, /function formatFileSize\(sizeBytes: number\)/);
  assert.match(source, /progress: 8/);
  assert.match(source, /Math\.min\(item\.assetId \? 92 : 68, item\.progress \+ 4\)/);
  assert.match(source, /status: "available", progress: 100/);
  assert.match(source, /waitForUploadedAssets\(\[uploadedAsset\], uploadProjectId\)/);
  assert.match(source, /上传中/);
  assert.match(source, /可用/);
  assert.match(source, /formatFileSize\(asset\.size_bytes\)/);
});

test("上传记录仅使用短 transform opacity 动效并支持减弱动效", async () => {
  const css = await readFile(stylePath, "utf8");

  assert.match(css, /\.upload-feedback-item[\s\S]*?animation: upload-feedback-enter 160ms ease-out/);
  assert.match(css, /\.upload-feedback-item\.is-exiting[\s\S]*?opacity: 0;[\s\S]*?transform: translateY\(-6px\)/);
  assert.match(css, /\.upload-feedback-progress > span[\s\S]*?transition: transform 180ms ease-out/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /\.upload-feedback-item[\s\S]*?\.asset-card\.is-new[\s\S]*?animation: none !important/);
  assert.match(css, /transform: none !important/);
});
```

- [ ] **Step 2: Run the new frontend test and confirm RED**

Run:

```bash
cd frontend && node --test tests/upload-completion-feedback.test.mjs
```

Expected: both tests fail because no upload feedback model or styles exist.

- [ ] **Step 3: Add asset size, upload feedback types, and format helper**

In `frontend/src/App.tsx`, add `size_bytes` to `Asset` and define `UploadFeedback`:

```typescript
type Asset = {
  asset_id: string;
  project_id: string;
  name: string;
  kind: string;
  status: string;
  size_bytes: number;
  preview_url?: string;
  download_url: string;
  page_count?: number;
  chunk_count?: number;
  error?: string;
  tags: string[];
  description: string;
  vision_status: "unavailable" | "queued" | "processing" | "ready" | "empty" | "failed";
  version: {
    group_id: string;
    number: number;
    is_current: boolean;
    replaces_asset_id?: string;
  };
};

type UploadFeedback = {
  id: string;
  projectId: string;
  assetId?: string;
  name: string;
  sizeBytes: number;
  progress: number;
  status: "uploading" | "available" | "failed";
  error?: string;
  exiting?: boolean;
};
```

Add beside the existing formatting helpers:

```typescript
function formatFileSize(sizeBytes: number): string {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) return "大小未知";
  const units = ["B", "KB", "MB", "GB"];
  let value = sizeBytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const rounded = unitIndex === 0 ? Math.round(value) : Number(value.toFixed(1));
  return `${rounded} ${units[unitIndex]}`;
}

const wait = (milliseconds: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
```

- [ ] **Step 4: Add the upload feedback component and asset-card size**

Add before `AssetCard`:

```tsx
function UploadFeedbackList({ items }: { items: UploadFeedback[] }) {
  if (!items.length) return null;
  return (
    <div className="upload-feedback-list" aria-live="polite">
      {items.map((item) => (
        <div
          key={item.id}
          className={`upload-feedback-item upload-feedback-${item.status}${item.exiting ? " is-exiting" : ""}`}
        >
          <div className="upload-feedback-copy">
            <strong>{item.name}</strong>
            <span>{formatFileSize(item.sizeBytes)}</span>
          </div>
          <div className="upload-feedback-status">
            <span>{item.status === "available" ? "可用" : item.status === "failed" ? "失败" : "上传中"}</span>
            <span>{Math.round(item.progress)}%</span>
          </div>
          <div
            className="upload-feedback-progress"
            role="progressbar"
            aria-label={`${item.name} 上传进度`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(item.progress)}
          >
            <span style={{ transform: `scaleX(${item.progress / 100})` }} />
          </div>
          {item.error ? <p className="error-text">{item.error}</p> : null}
        </div>
      ))}
    </div>
  );
}
```

Add `isNew: boolean` to `AssetCard` props, render the card as:

```tsx
<Card className={`asset-card${isNew ? " is-new" : ""}`}>
```

Change its metadata line to:

```tsx
<p>
  {asset.kind.toUpperCase()} · {formatFileSize(asset.size_bytes)} ·{" "}
  {asset.page_count || 0} 页 · {asset.chunk_count || 0} 个片段
</p>
```

- [ ] **Step 5: Make polling project-aware and resilient to transient errors**

Replace `waitForUploadedAssets()` with:

```typescript
async function waitForUploadedAssets(
  uploaded: Asset[],
  targetProjectId = projectId,
) {
  const ids = new Set(uploaded.map((asset) => asset.asset_id));
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    let data: { assets: Asset[] };
    try {
      data = await request<{ assets: Asset[] }>(
        `/api/assets?project_id=${encodeURIComponent(targetProjectId)}`,
      );
    } catch {
      await wait(500);
      continue;
    }
    const selected = data.assets.filter((asset) => ids.has(asset.asset_id));
    const failed = selected.find((asset) => asset.status === "failed");
    if (failed) {
      throw new Error(`${failed.name} 处理失败：${failed.error || "请检查文件内容。"}`);
    }
    if (
      selected.length === ids.size &&
      selected.every((asset) => asset.status === "ready")
    ) {
      return { assets: selected, allAssets: data.assets };
    }
    await wait(500);
  }
  throw new Error("文件仍在处理，未能确认已完成索引。请稍后在资料库中查看状态。");
}
```

- [ ] **Step 6: Implement per-file bounded progress and merge behavior**

In `App()`, add state and a current-project ref:

```typescript
const [uploadFeedback, setUploadFeedback] = useState<UploadFeedback[]>([]);
const [recentlyAddedAssetIds, setRecentlyAddedAssetIds] = useState<Set<string>>(
  new Set(),
);
const projectIdRef = useRef(projectId);

useEffect(() => {
  projectIdRef.current = projectId;
}, [projectId]);
```

Replace knowledge-page `uploadFiles()` with:

```typescript
async function uploadFiles(files: FileList | File[]) {
  const selectedFiles = Array.from(files);
  if (!selectedFiles.length || busy) return;
  const uploadProjectId = projectId;
  const records = selectedFiles.map((file, index) => ({
    id: `${Date.now()}-${index}-${file.name}`,
    projectId: uploadProjectId,
    name: file.name,
    sizeBytes: file.size,
    progress: 8,
    status: "uploading" as const,
  }));
  setUploadFeedback((current) => [...current, ...records]);
  setBusy(true);
  setError("");
  let failedCount = 0;

  for (const [index, file] of selectedFiles.entries()) {
    const record = records[index];
    const progressTimer = window.setInterval(() => {
      setUploadFeedback((current) =>
        current.map((item) =>
          item.id === record.id && item.status === "uploading"
            ? {
                ...item,
                progress: Math.min(item.assetId ? 92 : 68, item.progress + 4),
              }
            : item,
        ),
      );
    }, 180);

    try {
      const [uploadedAsset] = await uploadFilesToProject([file]);
      setUploadFeedback((current) =>
        current.map((item) =>
          item.id === record.id
            ? { ...item, assetId: uploadedAsset.asset_id, progress: Math.max(72, item.progress) }
            : item,
        ),
      );
      const ready = await waitForUploadedAssets([uploadedAsset], uploadProjectId);
      window.clearInterval(progressTimer);
      setUploadFeedback((current) =>
        current.map((item) =>
          item.id === record.id
            ? { ...item, status: "available", progress: 100 }
            : item,
        ),
      );
      if (projectIdRef.current === uploadProjectId) {
        setAssets(ready.allAssets);
        setRecentlyAddedAssetIds((current) =>
          new Set([...current, uploadedAsset.asset_id]),
        );
      }
      await wait(180);
      setUploadFeedback((current) =>
        current.map((item) =>
          item.id === record.id ? { ...item, exiting: true } : item,
        ),
      );
      await wait(180);
      setUploadFeedback((current) => current.filter((item) => item.id !== record.id));
      window.setTimeout(() => {
        setRecentlyAddedAssetIds((current) => {
          const next = new Set(current);
          next.delete(uploadedAsset.asset_id);
          return next;
        });
      }, 180);
    } catch (reason) {
      window.clearInterval(progressTimer);
      failedCount += 1;
      const message = reason instanceof Error ? reason.message : "上传失败。";
      setUploadFeedback((current) =>
        current.map((item) =>
          item.id === record.id
            ? { ...item, status: "failed", error: message }
            : item,
        ),
      );
    }
  }

  if (projectIdRef.current === uploadProjectId) {
    try {
      await refresh(uploadProjectId, search);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "资料列表刷新失败。");
    }
  }
  setNotice(
    failedCount
      ? `${selectedFiles.length - failedCount} 个文件可用，${failedCount} 个文件处理失败。`
      : "文件已写入存储并完成索引。",
  );
  if (failedCount) setError("部分文件未能完成上传或索引，请查看文件反馈。");
  setBusy(false);
}
```

Render feedback directly after `</DropZone>`:

```tsx
<UploadFeedbackList
  items={uploadFeedback.filter((item) => item.projectId === projectId)}
/>
```

Pass this prop to every `AssetCard`:

```tsx
isNew={recentlyAddedAssetIds.has(asset.asset_id)}
```

- [ ] **Step 7: Add short transform/opacity styles and reduced motion**

Add to `frontend/src/App.css` near `.assets-grid`:

```css
.upload-feedback-list {
  display: grid;
  gap: 10px;
  margin-top: 12px;
}
.upload-feedback-item {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid #3f3f46;
  border-radius: 12px;
  background: #18181b;
  animation: upload-feedback-enter 160ms ease-out both;
  transition: transform 180ms ease-out, opacity 180ms ease-out;
}
.upload-feedback-item.is-exiting {
  opacity: 0;
  transform: translateY(-6px);
}
.upload-feedback-copy,
.upload-feedback-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.upload-feedback-copy strong {
  overflow: hidden;
  color: #f4f4f5;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.upload-feedback-copy span,
.upload-feedback-status {
  color: #a1a1aa;
  font-size: 12px;
}
.upload-feedback-available .upload-feedback-status span:first-child {
  color: #86efac;
}
.upload-feedback-failed .upload-feedback-status span:first-child {
  color: #fca5a5;
}
.upload-feedback-progress {
  height: 5px;
  overflow: hidden;
  border-radius: 999px;
  background: #27272a;
}
.upload-feedback-progress > span {
  display: block;
  width: 100%;
  height: 100%;
  border-radius: inherit;
  background: #818cf8;
  transform-origin: left center;
  transition: transform 180ms ease-out;
}
.asset-card.is-new {
  animation: asset-card-enter 180ms ease-out both;
}
@keyframes upload-feedback-enter {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes asset-card-enter {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
@media (prefers-reduced-motion: reduce) {
  .upload-feedback-item,
  .upload-feedback-item.is-exiting,
  .upload-feedback-progress > span,
  .asset-card.is-new {
    animation: none !important;
    transition: none !important;
    transform: none !important;
  }
}
```

- [ ] **Step 8: Run the new frontend test and confirm GREEN**

Run:

```bash
cd frontend && node --test tests/upload-completion-feedback.test.mjs
```

Expected: 2 passed.

- [ ] **Step 9: Run frontend regressions, lint, and type-safe build**

Run:

```bash
cd frontend && node --test tests/*.test.mjs && npm run lint && npm run build
```

Expected: all tests pass, lint exits 0, TypeScript/Vite build succeeds.

- [ ] **Step 10: Commit Task 3 source and tests**

```bash
git add frontend/src/App.tsx frontend/src/App.css frontend/tests/upload-completion-feedback.test.mjs
git commit -m "feat: show truthful upload completion feedback"
```

---

### Task 4: Run full regression and publish the frontend build artifact

**Files:**
- Modify: `frontend/dist/index.html`
- Modify/Create/Delete: hashed files under `frontend/dist/assets/` produced by Vite

**Interfaces:**
- Consumes: all backend and frontend changes from Tasks 1–3.
- Produces: verified repository state and the prebuilt frontend assets consumed by `Dockerfile.frontend`.

- [ ] **Step 1: Run the complete Python test suite**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass with no failures or errors.

- [ ] **Step 2: Run all frontend checks again from a clean command boundary**

```bash
cd frontend && node --test tests/*.test.mjs && npm run lint && npm run build
```

Expected: all Node tests pass, lint exits 0, and Vite writes a successful production build.

- [ ] **Step 3: Verify the unaffected Agent SDK TypeScript contract**

```bash
cd agent_sdk && npm run check
```

Expected: TypeScript check exits 0.

- [ ] **Step 4: Check patch hygiene and inspect generated scope**

```bash
git diff --check
git status --short
git diff --stat HEAD
```

Expected: no whitespace errors; only the expected frontend build artifacts remain uncommitted after Tasks 1–3.

- [ ] **Step 5: Commit the generated frontend build**

```bash
git add frontend/dist
git commit -m "chore: rebuild frontend upload feedback assets"
```

- [ ] **Step 6: Confirm a clean final worktree**

```bash
git status --short --branch
```

Expected: branch status only, with no modified or untracked files.
