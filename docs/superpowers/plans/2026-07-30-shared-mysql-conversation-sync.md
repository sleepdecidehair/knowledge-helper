# Shared MySQL Conversation Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local and server processes immediately read the same MySQL conversation history without overwriting unrelated conversations.

**Architecture:** MySQL becomes the authoritative source after the one-time JSON migration. Reads and mutations refresh from MySQL; normal writes use per-conversation UPSERTs and scoped message replacement. JSON remains a local backup.

**Tech Stack:** Python 3.13, FastAPI, PyMySQL, pytest.

## Global Constraints

- Modify local code only; do not deploy, push, delete, or manually alter existing MySQL history.
- Preserve the one-time JSON-to-MySQL migration and complete payloads.

---

### Task 1: Add regression tests for shared-process consistency

**Files:**

- Modify: `tests/test_conversation_mysql.py`
- Modify: `app/agent.py`

- [ ] **Step 1: Write failing tests**

```python
def test_mysql_backed_list_refreshes_the_process_cache(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations.json", mysql_enabled=True, mysql_config={"host": "db"})
    store._mysql_conn = RecordingConnection()
    refreshed = []
    monkeypatch.setattr(store, "_load_from_mysql_locked", lambda: refreshed.append(True))
    store.list()
    assert refreshed == [True]


def test_mysql_persist_updates_only_the_changed_conversation(tmp_path):
    store._persist_locked(conversation_ids=["conversation-1"])
    assert "DELETE FROM conversations" not in statements
    assert any("ON DUPLICATE KEY UPDATE" in statement for statement in statements)
```

- [ ] **Step 2: Run the focused tests and observe red**

Run: `../../.venv/bin/pytest -q tests/test_conversation_mysql.py -k 'refreshes_the_process_cache or updates_only_the_changed'`

Expected: the list does not refresh and scoped persistence is unsupported.

- [ ] **Step 3: Implement the minimal store behavior**

```python
def _refresh_from_mysql_locked(self) -> bool:
    if not self._mysql_enabled:
        return False
    try:
        if self._mysql is None:
            raise RuntimeError("MySQL 未连接")
        self._load_from_mysql_locked()
        return True
    except Exception as exc:
        logger.warning("MySQL 历史刷新失败，保留当前内存副本: %s", exc)
        return False
```

Call the refresh before reads and mutations. Make `_persist_locked` accept changed and deleted IDs. Normal writes upsert only one conversation, replace only that conversation's messages, and delete only the target conversation. Keep full-table sync only for first migration.

- [ ] **Step 4: Run focused tests and observe green**

Run: `../../.venv/bin/pytest -q tests/test_conversation_mysql.py -k 'refreshes_the_process_cache or updates_only_the_changed'`

Expected: 2 passed.

- [ ] **Step 5: Commit**

Run: `git add app/agent.py tests/test_conversation_mysql.py && git commit -m "fix: synchronize conversation history through MySQL"`

### Task 2: Verify the change and retain the local validation service

**Files:**

- Verify: `app/agent.py`
- Verify: `tests/test_conversation_mysql.py`
- Verify: `tests/test_knowledge_base.py`

- [ ] **Step 1: Run full validation**

Run: `node --test frontend/tests/*.test.mjs && ../../.venv/bin/pytest -q && (cd frontend && npm run lint && npm run build) && git diff --check`

Expected: all tests pass; lint and production build exit 0.

- [ ] **Step 2: Compare the API and MySQL fingerprints**

Run a read-only local probe that compares the MySQL conversation ID fingerprint with `http://127.0.0.1:8002/api/conversations?project_id=local-default`.

Expected: the fresh local API read matches MySQL.

- [ ] **Step 3: Preserve the local branch**

Do not push, deploy, restart the remote server, or delete the worktree. Report the isolated local test URL.
