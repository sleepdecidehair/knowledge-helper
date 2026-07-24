# Asset Lifecycle and Local Multimodal Indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each local knowledge asset editable, reprocessable, versioned and optionally visually extractable so only its current version participates in retrieval.

**Architecture:** `AssetStore` remains the local source of truth, adding metadata, immutable version lineage and visible visual-processing status. `AssetProcessor` optionally calls a loopback-only OpenAI-compatible vision service and stores its extraction on the asset; `KnowledgeBase` consumes that saved extraction during rebuild rather than asking the chat model to invent image contents. FastAPI exposes asset actions and pipeline configuration, and the existing HeroUI knowledge workspace renders and invokes them.

**Tech Stack:** Python 3.13, FastAPI, dataclasses/local JSON, PyMuPDF, Pillow, Python stdlib `urllib`, React/TypeScript, HeroUI.

## Global Constraints

- Keep files, metadata, indexes, visual extractions, projects and conversations on the local machine.
- Permit any user-owned content to be stored; do not redact or content-classify knowledge notes or asset metadata.
- Vision/OCR is optional and may only call `http://localhost`, `http://127.0.0.1` or `http://[::1]`; no API key is persisted for this adapter.
- DeepSeek's Anthropic-compatible chat endpoint is not used for image blocks; unavailable visual extraction must remain an explicit attachment-only state.
- Maintain existing text/PDF indexing, attachment downloads, project isolation and Agent SDK local-MCP contracts.
- Use test-first red-green-refactor for every behavioral change.
- Do not stage, commit or publish the existing application changes unless the user asks.

---

## File Structure

- Create: `app/vision.py` — validates/calls the local OpenAI-compatible visual extractor and returns saved text per page/image.
- Modify: `app/assets.py` — persistent metadata, content hashes, version lineage, current-version selection and public asset serialization.
- Modify: `app/knowledge_base.py` — pipeline fields for optional visual extraction plus indexing of already-saved visual segments.
- Modify: `app/ingestion.py` — preview, optional extraction, non-fatal visual status, and rebuilding from current ready assets.
- Modify: `app/knowledge_writer.py` — calculate content hash for Agent-created notes.
- Modify: `app/main.py` — metadata, reprocess, replace, restore, filter and visual pipeline APIs.
- Modify: `tests/test_knowledge_base.py` — store, extraction, indexing and lifecycle integration tests.
- Modify: `tests/test_status_runtime.py` — status compatibility test updates only if the mocked store gains a required method.
- Modify: `frontend/src/App.tsx` — expanded asset/pipeline types and HeroUI asset lifecycle controls.
- Modify: `frontend/src/App.css` — compact black/silver styling for tags, version status and lifecycle actions.
- Create: `frontend/tests/asset-lifecycle.test.mjs` — static frontend contract test for lifecycle endpoints and visual controls.

### Task 1: Define local asset metadata and version semantics

**Files:**
- Modify: `tests/test_knowledge_base.py`
- Modify: `app/assets.py`

**Interfaces:**
- Produces `AssetStore.create(original_name, stored_name, project_id="local-default", content_hash="", version_group_id=None, version_no=1, replaces_asset_id="") -> Asset`.
- Produces `AssetStore.update_metadata(asset_id, *, original_name=None, tags=None, description=None, project_id=None) -> Asset`.
- Produces `AssetStore.replace(asset_id, original_name, stored_name, content_hash) -> Asset`, `restore_version(asset_id) -> Asset`, `requeue(asset_id) -> Asset`, and `ready_current_assets(project_id=None) -> list[Asset]`.
- Adds serializable `Asset` fields `tags`, `description`, `content_hash`, `version_group_id`, `version_no`, `is_current_version`, `replaces_asset_id`, `vision_status`, `visual_segments`.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_asset_metadata_versions_and_current_retrieval_selection(tmp_path: Path):
    _, store, _, _, _ = build_runtime(tmp_path)
    first = store.create("制度-v1.md", "first.md", content_hash="hash-one")
    renamed = store.update_metadata(first.id, original_name="制度.md", tags=["财务", "制度"], description="2025 版")
    replacement = store.replace(first.id, "制度-v2.md", "second.md", "hash-two")

    assert renamed.tags == ["财务", "制度"]
    assert store.get(first.id).is_current_version is False
    assert replacement.version_group_id == first.version_group_id
    assert replacement.version_no == 2
    assert [asset.id for asset in store.ready_current_assets()] == []

    restored = store.restore_version(first.id)
    assert restored.is_current_version is True
    assert store.get(replacement.id).is_current_version is False
```

- [ ] **Step 2: Run the focused test and verify it fails because the metadata/version API is missing**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py::test_asset_metadata_versions_and_current_retrieval_selection -q`

Expected: FAIL with `AttributeError` for `update_metadata` or `replace`.

- [ ] **Step 3: Implement the minimal persistent AssetStore behavior**

```python
@dataclass(frozen=True)
class Asset:
    # existing required fields remain first
    tags: List[str] = field(default_factory=list)
    description: str = ""
    content_hash: str = ""
    version_group_id: str = ""
    version_no: int = 1
    is_current_version: bool = True
    replaces_asset_id: str = ""
    vision_status: str = "unavailable"
    visual_segments: List[Dict[str, object]] = field(default_factory=list)

def ready_current_assets(self, project_id: Optional[str] = None) -> List[Asset]:
    return [asset for asset in self.ready_assets(project_id) if asset.is_current_version]
```

Normalize legacy JSON at load time so an older asset gets its own `version_group_id` and remains current. Reject duplicate/empty/overlong tags, strip control characters from names and descriptions, and only let `replace` change a version group's current member inside the same project. Make `public()` expose metadata and a `version` object without exposing a local path or visual text.

- [ ] **Step 4: Run the focused test and the existing asset tests**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -q`

Expected: PASS.

- [ ] **Step 5: Review the persisted JSON contract**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py::test_asset_metadata_versions_and_current_retrieval_selection -q`

Expected: PASS; the test exercises saved metadata through the store API rather than an in-memory-only object.

### Task 2: Add a loopback-only visual extraction adapter and index saved output

**Files:**
- Create: `app/vision.py`
- Modify: `tests/test_knowledge_base.py`
- Modify: `app/knowledge_base.py`
- Modify: `app/ingestion.py`

**Interfaces:**
- Produces `extract_visual_text(*, base_url: str, model: str, image_bytes: bytes, mime_type: str, timeout_seconds: int = 45) -> str`.
- `KnowledgeBase.update_pipeline` accepts `vision_adapter`, `vision_model`, `vision_base_url`, and `vision_max_pages`; `pipeline_status()` returns `vision_index` as `unconfigured`, `configured`, or `active`.
- `AssetProcessor.process(asset_id)` saves `visual_segments` in the shape `[{"page": Optional[int], "text": str}]` and leaves the asset `ready` if visual extraction fails.

- [ ] **Step 1: Write failing extraction/indexing tests**

```python
def test_visual_text_is_saved_then_searchable_without_chat_model(tmp_path: Path, monkeypatch):
    _, store, kb, processor, _ = build_runtime(tmp_path)
    image = tmp_path / "receipt.png"
    Image.new("RGB", (80, 50), color="white").save(image)
    asset = add_asset(store, image, "发票.png")
    kb.update_pipeline(vision_adapter="local_openai_compatible", vision_model="local-vision", vision_base_url="http://127.0.0.1:9000/v1", vision_max_pages=4)
    monkeypatch.setattr(processor, "_extract_visual_segments", lambda asset, path: [{"page": None, "text": "发票金额 128 元"}])

    processor.process(asset.id)

    assert store.get(asset.id).vision_status == "ready"
    assert kb.search("发票金额")[0]["asset_id"] == asset.id


def test_visual_adapter_rejects_non_loopback_endpoint(tmp_path: Path):
    settings, _, kb, _, _ = build_runtime(tmp_path)
    with pytest.raises(ValueError, match="本机 HTTP 地址"):
        kb.update_pipeline(
            vision_adapter="local_openai_compatible", vision_model="vision", vision_base_url="https://example.com/v1", vision_max_pages=4
        )
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'visual_text or visual_adapter' -q`

Expected: FAIL because `vision_adapter` is not a pipeline field and `_extract_visual_segments` does not exist.

- [ ] **Step 3: Implement the adapter and pipeline fields**

```python
def extract_visual_text(*, base_url: str, model: str, image_bytes: bytes, mime_type: str, timeout_seconds: int = 45) -> str:
    payload = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "提取图片中可检索的可见文字和事实；不要猜测。"},
        {"type": "image_url", "image_url": {"url": data_url(image_bytes, mime_type)}},
    ]}]}
    # urllib POST to the already-validated loopback /chat/completions endpoint
```

Reuse the existing `_validated_model_adapter` for `vision_*`. For images, send the source image to the adapter; for PDFs, render at most `vision_max_pages` pages using the existing preview renderer and store each non-empty extraction with its page number. In `_asset_segments`, prefer saved visual text for an image, append saved per-page visual text to a PDF page, and otherwise preserve the existing explicit attachment-only fallback. Do not call the adapter from search or chat.

- [ ] **Step 4: Run visual and existing pipeline tests**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -q`

Expected: PASS; a failed visual call leaves the text/PDF asset ready and `vision_status="failed"`.

- [ ] **Step 5: Check that no external HTTP endpoint is accepted**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'visual_adapter or embedding_and_reranker' -q`

Expected: PASS.

### Task 3: Expose lifecycle and visual configuration through local APIs

**Files:**
- Modify: `tests/test_knowledge_base.py`
- Modify: `app/main.py`
- Modify: `app/knowledge_writer.py`

**Interfaces:**
- `PATCH /api/assets/{asset_id}` accepts any subset of `project_id`, `name`, `tags`, `description`.
- `POST /api/assets/{asset_id}/reprocess` queues that asset.
- `POST /api/assets/{asset_id}/replace` accepts `multipart/form-data` `file` and returns the new version.
- `POST /api/assets/{asset_id}/restore` makes the selected version current and rebuilds the index.
- `GET /api/assets?project_id=...&tag=...` filters local assets by an exact normalized tag.
- `PipelineRequest` passes the Task 2 vision fields; both upload and Agent-written notes calculate SHA-256 before `AssetStore.create`.

- [ ] **Step 1: Write failing endpoint-level contract tests around helper behavior**

```python
def test_requeue_and_restore_make_only_current_ready_asset_retrievable(tmp_path: Path):
    settings, store, kb, processor, _ = build_runtime(tmp_path)
    old_path = settings.knowledge_dir / "old.md"
    new_path = settings.knowledge_dir / "new.md"
    old_path.write_text("旧版住宿上限五百元", encoding="utf-8")
    new_path.write_text("新版住宿上限八百元", encoding="utf-8")
    first = store.create("制度.md", old_path.name)
    processor.process(first.id)
    second = store.replace(first.id, "制度.md", new_path.name, "new-hash")
    processor.process(second.id)

    kb.rebuild(store.ready_current_assets(), store.path_for)
    assert kb.search("八百元")[0]["asset_id"] == second.id

    store.restore_version(first.id)
    kb.rebuild(store.ready_current_assets(), store.path_for)
    assert kb.search("五百元")[0]["asset_id"] == first.id
    assert not kb.search("八百元")
```

Use direct app helpers/store methods where FastAPI's global app would make isolation brittle; verify request-model optionality with `AssetUpdateRequest(name="新名")` and `PipelineRequest(... vision_adapter=...)`.

- [ ] **Step 2: Run the focused contract test and verify it fails**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'requeue_and_restore' -q`

Expected: FAIL because index rebuild still uses all ready versions.

- [ ] **Step 3: Implement request models and endpoints**

```python
class AssetUpdateRequest(BaseModel):
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    tags: Optional[List[str]] = Field(default=None, max_length=20)
    description: Optional[str] = Field(default=None, max_length=800)

def rebuild_ready_assets() -> Dict[str, int]:
    return knowledge_base.rebuild(asset_store.ready_current_assets(), asset_store.path_for)
```

Factor the existing upload validation/writing into one private async helper used by upload and replace so filename validation, size limits, hashing and safe stored names cannot diverge. `reprocess` sets the asset to queued then schedules `processor.process`; `restore` rebuilds synchronously after changing current version. Rebuild after project moves and deletes as well, because retrieval permissions are project-scoped.

- [ ] **Step 4: Run all backend tests**

Run: `.venv/bin/python -m pytest -q`

Expected: PASS.

- [ ] **Step 5: Check FastAPI routes load without secrets in output**

Run: `.venv/bin/python -c 'from app.main import app; print(len(app.routes))'`

Expected: a positive route count only; no API key is printed.

### Task 4: Add compact HeroUI lifecycle and visual controls

**Files:**
- Create: `frontend/tests/asset-lifecycle.test.mjs`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`

**Interfaces:**
- Expanded `Asset` type consumes the Task 1 public contract; expanded `Pipeline` consumes Task 2 fields.
- `AssetCard` receives `onSaveMetadata`, `onReprocess`, `onReplace`, `onRestore` alongside existing move/delete handlers.
- `savePipeline` serializes all visual fields to `PUT /api/pipeline`.

- [ ] **Step 1: Write a failing frontend contract test**

```js
test('knowledge workspace exposes metadata, version and visual lifecycle actions', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /\/api\/assets\/\$\{asset\.asset_id\}\/reprocess/);
  assert.match(app, /\/api\/assets\/\$\{asset\.asset_id\}\/replace/);
  assert.match(app, /视觉服务/);
  assert.match(app, /版本\s*v\{asset\.version\.number\}/);
});
```

- [ ] **Step 2: Run the frontend contract test and verify it fails**

Run: `node --test frontend/tests/asset-lifecycle.test.mjs`

Expected: FAIL because lifecycle endpoints and visual controls are not rendered.

- [ ] **Step 3: Implement HeroUI-first controls**

```tsx
<Input
  value={descriptionDraft}
  onChange={(event) => setDescriptionDraft(event.target.value)}
  aria-label="资料说明"
/>
<Input
  value={tagsDraft}
  onChange={(event) => setTagsDraft(event.target.value)}
  aria-label="资料标签，使用逗号分隔"
/>
<Button variant="secondary" onPress={onReprocess}>重新处理</Button>
<Button variant="ghost" onPress={openReplacementPicker}>替换为新版本</Button>
{!asset.version.is_current ? <Button variant="ghost" onPress={onRestore}>恢复此版本</Button> : null}
```

Use a hidden file input only for the replace action; retain the HeroUI DropZone for normal upload. Render a readable current/old-version badge, current tags, extraction state and no local path. Add the visual service select/model/address/max-page fields beside the other processing controls, explain that it is loopback-only and an unconfigured service keeps images as attachments. Keep the established black/silver palette and use icon buttons with accessible text/tooltips where space is tight.

- [ ] **Step 4: Run frontend tests, lint and build**

Run: `node --test frontend/tests/*.test.mjs && (cd frontend && npm run lint && npm run build)`

Expected: all tests, lint and Vite build pass; an existing bundle-size warning is non-fatal.

- [ ] **Step 5: Run end-to-end static verification**

Run: `git diff --check && .venv/bin/python -m pytest -q && node --test agent_sdk/tests/*.test.mjs && node --test frontend/tests/*.test.mjs`

Expected: all commands pass and `git diff --check` emits no whitespace errors.

## Plan Self-Review

- Coverage: Task 1 implements editable metadata, hash, versions and current selection; Task 2 implements optional local visual extraction and saved-content indexing; Task 3 exposes every lifecycle/pipeline action and updates writes; Task 4 implements the user-facing HeroUI controls.
- Placeholder scan: no TBD/TODO markers or deferred behavior are present.
- Type consistency: the `visual_segments` asset shape produced in Task 2 is consumed by `KnowledgeBase`; current-version filtering is produced by Task 1 and used by Task 3; frontend properties come from `AssetStore.public` in Task 1.
