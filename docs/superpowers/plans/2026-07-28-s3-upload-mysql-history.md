# S3 Upload and Shared MySQL History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix S3 uploads and migrate local/server conversation history to one shared MySQL-backed store without losing current history.

**Architecture:** Keep `conversations` and `messages` as the shared relational store and add JSON payload columns for complete message fidelity. First successful MySQL startup merges legacy database rows with the local JSON backup and writes a durable migration marker; all later loads use MySQL while JSON remains a recovery backup. The test-server container reaches host MySQL through Docker's `host-gateway` alias.

**Tech Stack:** Python 3.13, pytest, boto3, PyMySQL, Docker Compose, GitHub Actions.

## Global Constraints

- Use Python 3.13 for the new virtual environment.
- Keep `data/`, `knowledge/`, `.env`, and all credentials out of Git.
- Do not use irreversible deletion for conversation rows, messages, Docker volumes, or object-store files during migration; the relational replacement runs in one MySQL transaction.
- Preserve existing `conversations` and `messages` table data by using an additive schema migration.

---

### Task 1: S3 upload regression

**Files:**
- Create: `tests/test_s3_storage.py`
- Modify: `app/s3_storage.py:56-65`
- Test: `tests/test_s3_storage.py`

**Interfaces:**
- Consumes: `S3Storage.upload(key: str, data: bytes, content_type: str) -> str`.
- Produces: a valid boto3 `put_object` request without `ContentSHA256`.

- [ ] **Step 1: Write the failing test**

```python
def test_upload_passes_only_supported_put_object_arguments():
    client = RecordingS3Client()
    storage = S3Storage("https://s3.example", "key", "secret", "knowledge")
    storage._client = client
    storage.ensure_bucket = lambda: None

    assert storage.upload("guide.md", b"hello", "text/markdown") == "guide.md"
    assert client.request == {
        "Bucket": "knowledge",
        "Key": "guide.md",
        "Body": b"hello",
        "ContentType": "text/markdown",
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_s3_storage.py -v`

Expected: FAIL because the request includes `ContentSHA256`.

- [ ] **Step 3: Write the minimal implementation**

```python
self.client.put_object(
    Bucket=self.bucket,
    Key=key,
    Body=data,
    ContentType=content_type,
)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_s3_storage.py -v`

Expected: PASS.

### Task 2: Lossless shared conversation storage

**Files:**
- Modify: `app/agent.py:76-570`
- Modify: `tests/test_knowledge_base.py`
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Consumes: `ConversationStore(path, mysql_enabled, mysql_config)` and existing MySQL `conversations` / `messages` tables.
- Produces: MySQL-backed full conversation records, an idempotent migration marker, and JSON recovery backups.

- [ ] **Step 1: Write failing migration and merge tests**

```python
def test_merge_prefers_richer_json_message_and_keeps_database_only_conversation():
    merged = ConversationStore.merge_records(database_items, backup_items)
    assert set(merged) == {"database-only", "shared"}
    assert merged["shared"]["messages"][0]["agent"]["trace"] == [{"kind": "mcp_call"}]

def test_mysql_load_uses_snapshot_after_migration_marker(tmp_path):
    store = make_mysql_store(tmp_path, migrated=True)
    store.load()
    assert store.get("database-only") is not None
    assert store.get("backup-only") is None
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'merge_prefers_richer or mysql_load_uses_snapshot' -v`

Expected: FAIL because merge and migration-marker behavior do not exist.

- [ ] **Step 3: Implement additive MySQL migration and persistence**

```python
CREATE TABLE IF NOT EXISTS conversation_store_meta (
    meta_key VARCHAR(64) PRIMARY KEY,
    meta_value TEXT NOT NULL
)
ALTER TABLE conversations ADD COLUMN payload_json LONGTEXT NULL
ALTER TABLE messages ADD COLUMN payload_json LONGTEXT NULL
```

Implement `merge_records`, create the additive schema only when columns are absent, merge legacy database rows with JSON exactly once, serialize complete records into `payload_json`, then mark `payload_migration_v1`. Retain the existing local JSON write before MySQL synchronization and retain JSON fallback when MySQL cannot connect.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'merge_prefers_richer or mysql_load_uses_snapshot' -v`

Expected: PASS.

### Task 3: Runtime configuration and deployment

**Files:**
- Modify: `docker-compose.yml:6-35`
- Modify: `.env.example`
- Test: `docker compose config`

**Interfaces:**
- Consumes: existing host MySQL and Docker Compose environment variables.
- Produces: `host.docker.internal` resolution inside the backend container and documented local shared-MySQL configuration.

- [ ] **Step 1: Add a failing Compose assertion**

Run: `docker compose config | rg 'host.docker.internal:host-gateway'`

Expected: no output before adding the alias.

- [ ] **Step 2: Add the host gateway and configuration documentation**

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Document `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE` in `.env.example`; the test server `.env` will use `MYSQL_HOST=host.docker.internal`.

- [ ] **Step 3: Verify configuration and the complete suite**

Run: `docker compose config && .venv/bin/python -m pytest tests/ -q`

Expected: Compose renders the host alias; all task-related tests pass; retain the known unrelated baseline failure in the final report if it remains.

### Task 4: Publish and validate the test deployment

**Files:**
- Modify: server `/opt/knowledge-helper/.env` only for `MYSQL_HOST`

**Interfaces:**
- Consumes: the `dev` image pipeline and test-server `kh-test` Compose project.
- Produces: healthy backend connected to host MySQL, migrated conversations, and successful S3 uploads.

- [ ] **Step 1: Commit and push the feature branch, then merge to `dev`**

Run: `git add app/agent.py app/s3_storage.py docker-compose.yml .env.example tests docs && git commit -m 'fix: share history through mysql and restore s3 uploads'`

Expected: a clean commit; push and merge only after tests pass.

- [ ] **Step 2: Deploy through the existing `dev` GitHub Actions workflow**

Run on the test server: set `MYSQL_HOST=host.docker.internal`, then allow the workflow to pull the tagged `dev` image and run `docker compose -f docker-compose.yml -f docker-compose.test.yml -p kh-test up -d --wait --remove-orphans`.

Expected: backend remains healthy and data volume remains mounted.

- [ ] **Step 3: Verify data and upload behavior without exposing content**

Run read-only MySQL counts, JSON backup counts, and a backend S3 object write/delete probe with a generated key.

Expected: conversation and message counts are retained, local and server use the same database, and the S3 probe succeeds.
