# Expandable Source Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each answer show one compact, non-duplicated source file control whose name expands its cited excerpts and whose icons provide separate inline preview and download actions.

**Architecture:** Keep citations as the source-of-truth for answer references. `MessageView` will group citations by asset, store expanded asset IDs locally, and render excerpts only beneath expanded file controls. Assistant `attachments` will no longer be rendered because they duplicate citations; user-uploaded attachments stay visible on user messages. A new local view endpoint will serve the original supported asset with an inline content disposition so preview never falls back to the download endpoint.

**Tech Stack:** FastAPI/Starlette `FileResponse`, React, TypeScript, HeroUI, Lucide, pytest, Node built-in test runner, Vite.

## Global Constraints

- Do not alter the knowledge retrieval algorithm, Agent SDK execution, source text persistence, or diagnostics.
- A source file starts collapsed; clicking only its name toggles that file’s excerpts. Multiple source files may be expanded at once.
- Do not display source score. Keep source page and excerpt only inside the expanded card.
- The eye action must request a distinct inline-view URL in a new tab; the download action must request `download_url` with the `download` attribute.
- Do not render assistant attachments because they duplicate source file controls; user attachments stay visible.
- Do not stage, commit, or publish any change.

---

### Task 1: Serve original assets for inline browser preview

**Files:**
- Modify: `tests/test_status_runtime.py`
- Modify: `app/main.py`
- Modify: `app/agent.py`

**Interfaces:**
- Produces: `GET /api/assets/{asset_id}/view`, which returns a ready local original file as `Content-Disposition: inline`.
- Produces: citation `preview_url` equal to `/api/assets/{asset_id}/view` for every supported local asset.

- [ ] **Step 1: Write failing endpoint and citation tests**

```python
def test_view_asset_returns_the_original_file_inline(monkeypatch, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("local notes", encoding="utf-8")
    asset = SimpleNamespace(status="ready", media_type="text/plain", original_name="notes.txt")
    monkeypatch.setattr(main, "asset_store", SimpleNamespace(get=lambda _: asset, path_for=lambda _: path))

    response = main.view_asset("asset-1")

    assert response.headers["content-disposition"].startswith("inline;")
    assert response.media_type == "text/plain"
```

```python
assert citations[0]["preview_url"] == "/api/assets/asset-1/view"
```

- [ ] **Step 2: Run the targeted tests and verify failure**

Run: `pytest tests/test_status_runtime.py -q`

Expected: FAIL because `view_asset` does not exist and citations use generated image preview URLs or omit preview URLs.

- [ ] **Step 3: Implement the distinct inline view route and citation URL**

```python
@app.get("/api/assets/{asset_id}/view")
def view_asset(asset_id: str) -> FileResponse:
    asset = asset_store.get(asset_id)
    if not asset or asset.status != "ready":
        raise HTTPException(status_code=404, detail="预览尚不可用")
    try:
        return FileResponse(
            asset_store.path_for(asset),
            media_type=asset.media_type,
            filename=asset.original_name,
            content_disposition_type="inline",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
```

Set every citation’s `preview_url` to `f"/api/assets/{asset_id}/view"`; keep `download_url` unchanged.

- [ ] **Step 4: Run the targeted tests and verify pass**

Run: `pytest tests/test_status_runtime.py -q`

Expected: all targeted tests PASS.

### Task 2: Merge duplicate answer file displays into expandable source controls

**Files:**
- Modify: `frontend/tests/retrieval-evidence.test.mjs`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`

**Interfaces:**
- Consumes: `Message.sources?: Source[]`, grouped by `asset_id`.
- Produces: a compact control with a file-name toggle, inline preview action, download action, and conditional excerpts for that one file.

- [ ] **Step 1: Write the failing UI contract test**

```js
assert.match(source, /const \[expandedSourceIds, setExpandedSourceIds\] = useState/);
assert.match(source, /aria-expanded={isSourceExpanded}/);
assert.match(source, /source-preview-card/);
assert.match(source, /!user && displaySources.length/);
assert.match(source, /user && message\.attachments\?\.length/);
assert.doesNotMatch(source, /!user && message\.attachments/);
```

- [ ] **Step 2: Run the contract test and verify failure**

Run: `cd frontend && node --test tests/retrieval-evidence.test.mjs`

Expected: FAIL because source files do not toggle an excerpt card and assistant attachments are still rendered.

- [ ] **Step 3: Implement the source grouping, toggle, and conditional excerpt cards**

```tsx
const [expandedSourceIds, setExpandedSourceIds] = useState<Set<string>>(() => new Set());
const sourcesByAsset = new Map<string, Source[]>();
for (const source of message.sources || []) {
  sourcesByAsset.set(source.asset_id, [...(sourcesByAsset.get(source.asset_id) || []), source]);
}
```

Render a native `<button>` around the filename only, use `aria-expanded`, stop propagation on both action links, and render each asset’s `excerpt` in a conditional `source-preview-card`. Gate the existing attachment cards with `user`.

- [ ] **Step 4: Add compact styling for the toggle and hidden-by-default excerpt cards**

```css
.compact-source-name { cursor: pointer; }
.source-preview-card { flex-basis: 100%; }
```

Use existing black/silver palette. Ensure a long source name truncates and icon actions cannot be mistaken for the toggle.

- [ ] **Step 5: Verify the frontend contract and production build**

Run: `cd frontend && node --test tests/retrieval-evidence.test.mjs && npm run lint && npm run build`

Expected: contract test, lint, TypeScript, and Vite build exit 0. The Vite bundle-size advisory may remain non-fatal.

### Task 3: Verify the integrated API and UI build

**Files:**
- No additional code files.

- [ ] **Step 1: Run backend tests, frontend tests, and diff validation**

Run: `pytest -q && cd frontend && node --test tests/*.test.mjs && npm run lint && npm run build && git diff --check`

Expected: all tests and checks exit 0; Vite may report its non-fatal chunk-size advisory.

- [ ] **Step 2: Restart the local server and check the main page**

Run: `lsof -ti tcp:8000 | xargs -r kill && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`

Expected: the service listens on `http://127.0.0.1:8000`; clicking a source filename exposes only that file’s excerpts, preview opens `/view`, and assistant answer attachments are absent.
