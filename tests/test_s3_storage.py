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
