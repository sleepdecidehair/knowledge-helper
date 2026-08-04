import json
from dataclasses import asdict

from app.assets import Asset, AssetStore
from app.config import Settings
from app.s3_storage import S3Storage


class RecordingS3Client:
    def __init__(self):
        self.request = None

    def put_object(self, **kwargs):
        self.request = kwargs


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


def test_asset_store_merges_json_backup_when_mysql_has_only_partial_records(tmp_path, monkeypatch):
    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        assets_path=tmp_path / "data" / "assets.json",
        mysql_host="mysql.example",
        mysql_password="secret",
    )
    json_asset = Asset(
        id="json-only",
        original_name="JSON 备份.md",
        stored_name="aaaaaaaa_JSON.md",
        media_type="text/markdown",
        status="ready",
        created_at="2026-07-28T00:00:00+00:00",
    )
    mysql_asset = Asset(
        id="mysql-only",
        original_name="MySQL 资料.md",
        stored_name="bbbbbbbb_MySQL.md",
        media_type="text/markdown",
        status="ready",
        created_at="2026-07-28T00:00:00+00:00",
    )
    settings.data_dir.mkdir(parents=True)
    settings.assets_path.write_text(json.dumps({"assets": [asdict(json_asset)]}), encoding="utf-8")

    store = AssetStore(settings)
    monkeypatch.setattr(store, "_load_from_mysql_locked", lambda: store._assets.update({mysql_asset.id: mysql_asset}))
    monkeypatch.setattr(store, "_save_locked", lambda: None)

    store.load()

    assert set(store._assets) == {"mysql-only", "json-only"}


def test_asset_store_registers_safe_orphaned_s3_files(tmp_path):
    class FakeS3:
        def list_keys(self):
            return [
                "1a2b3c4d_policy.md",
                "handbook.txt",
                "existing.pdf",
                "nested/unsafe.md",
                "unsupported.exe",
            ]

    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        assets_path=tmp_path / "data" / "assets.json",
        mysql_host="",
        mysql_password="",
    )
    store = AssetStore(settings, s3_storage=FakeS3())
    store.create("已有资料.pdf", "existing.pdf")

    created = store.register_existing_files()

    assert [(asset.original_name, asset.stored_name) for asset in created] == [
        ("policy.md", "1a2b3c4d_policy.md"),
        ("handbook.txt", "handbook.txt"),
    ]


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


def test_chunk_count_sync_persists_all_asset_eligibility_changes_once(tmp_path, monkeypatch):
    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        assets_path=tmp_path / "data" / "assets.json",
        mysql_host="",
        mysql_password="",
    )
    store = AssetStore(settings)
    ready = store.create("ready.md", "ready.md")
    queued = store.create("queued.md", "queued.md")
    noncurrent = store.create("old.md", "old.md")
    store.update(ready.id, status="ready", chunk_count=1)
    store.update(queued.id, chunk_count=4)
    store.update(noncurrent.id, status="ready", is_current_version=False, chunk_count=5)
    save_calls = 0
    original_save = store._save_locked

    def count_save():
        nonlocal save_calls
        save_calls += 1
        original_save()

    monkeypatch.setattr(store, "_save_locked", count_save)

    store.sync_chunk_counts({ready.id: 7, queued.id: 8, noncurrent.id: 9})

    reloaded = AssetStore(settings)
    reloaded.load()
    assert save_calls == 1
    assert reloaded.get(ready.id).chunk_count == 7
    assert reloaded.get(queued.id).chunk_count == 0
    assert reloaded.get(noncurrent.id).chunk_count == 0


def test_chunk_count_sync_updates_mysql_rows_in_one_batch(tmp_path, monkeypatch):
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, statement, params=None):
            statements.append((statement, params))

        def fetchone(self):
            return ("size_bytes",)

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
    ready = store.create("ready.md", "ready.md")
    queued = store.create("queued.md", "queued.md")
    store.update(ready.id, status="ready", chunk_count=1)
    store.update(queued.id, chunk_count=4)
    statements.clear()

    store.sync_chunk_counts({ready.id: 6, queued.id: 7})

    delete_statements = [sql for sql, _ in statements if sql.startswith("DELETE FROM knowledge_assets")]
    insert_params = {
        params[0]: params
        for sql, params in statements
        if sql.startswith("INSERT INTO knowledge_assets")
    }
    assert len(delete_statements) == 1
    assert insert_params[ready.id][8] == 6
    assert insert_params[queued.id][8] == 0


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
