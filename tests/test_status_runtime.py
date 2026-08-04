import asyncio
from io import BytesIO
from types import SimpleNamespace

from fastapi import BackgroundTasks, UploadFile

from app.agent import KnowledgeAgent
from app import main
from app.assets import AssetStore
from app.config import Settings
from app.ingestion import AssetProcessor
from app.knowledge_base import Chunk, KnowledgeBase


def build_startup_runtime(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        knowledge_dir=tmp_path / "knowledge",
        data_dir=tmp_path / "data",
        index_path=tmp_path / "data" / "index.json",
        assets_path=tmp_path / "data" / "assets.json",
        previews_dir=tmp_path / "data" / "previews",
        mysql_host="",
        mysql_password="",
    )
    settings.knowledge_dir.mkdir(parents=True)
    asset_store = AssetStore(settings)
    knowledge_base = KnowledgeBase(settings)
    processor = AssetProcessor(settings, asset_store, knowledge_base)
    return settings, asset_store, knowledge_base, processor


def patch_startup_services(monkeypatch, asset_store, knowledge_base, processor):
    passive_store = SimpleNamespace(load=lambda: None)
    quality_store = SimpleNamespace(load=lambda: None, recover_interrupted_jobs=lambda: None)
    monkeypatch.setattr(main, "project_store", passive_store)
    monkeypatch.setattr(main, "profile_store", passive_store)
    monkeypatch.setattr(main, "runtime_store", passive_store)
    monkeypatch.setattr(main, "quality_store", quality_store)
    monkeypatch.setattr(main, "asset_store", asset_store)
    monkeypatch.setattr(main, "knowledge_base", knowledge_base)
    monkeypatch.setattr(main, "processor", processor)
    monkeypatch.setattr(main, "agent", SimpleNamespace(initialize=lambda: None))
    monkeypatch.setattr(asset_store, "load", lambda: None)
    monkeypatch.setattr(knowledge_base, "load", lambda: None)


def test_status_uses_runtime_api_key_state_after_a_key_is_saved_in_settings(monkeypatch):
    monkeypatch.setattr(main, "settings", SimpleNamespace(deepseek_api_key=""))
    monkeypatch.setattr(main, "project_store", SimpleNamespace(exists=lambda project_id: True))
    monkeypatch.setattr(
        main,
        "asset_store",
        SimpleNamespace(all_assets=lambda project_id: [], ready_asset_ids=lambda project_id: set()),
    )
    monkeypatch.setattr(
        main,
        "knowledge_base",
        SimpleNamespace(chunks=[], chunking_status=lambda: {}, pipeline_status=lambda: {}),
    )
    monkeypatch.setattr(
        main,
        "runtime_store",
        SimpleNamespace(
            get=lambda: {
                "api_key_configured": True,
                "deepseek_model": "deepseek-test",
            }
        ),
    )
    monkeypatch.setattr(main, "agent", SimpleNamespace(runner=SimpleNamespace(is_ready=lambda: True)))

    payload = main.status(project_id="project-test")

    assert payload["deepseek_configured"] is True
    assert payload["model"] == "deepseek-test"


def test_startup_clears_orphan_chunks_when_no_assets_are_ready(monkeypatch, tmp_path):
    _, asset_store, knowledge_base, processor = build_startup_runtime(tmp_path)
    asset = asset_store.create("failed.md", "failed.md")
    asset_store.update(asset.id, status="failed", chunk_count=1)
    knowledge_base.chunks = [
        Chunk("orphan", asset.id, asset.original_name, 1, None, "不应继续被检索")
    ]
    patch_startup_services(monkeypatch, asset_store, knowledge_base, processor)

    main.startup()

    assert knowledge_base.chunks == []
    assert asset_store.get(asset.id).chunk_count == 0


def test_startup_rebuilds_when_recorded_and_actual_chunk_counts_differ(monkeypatch, tmp_path):
    settings, asset_store, knowledge_base, processor = build_startup_runtime(tmp_path)
    source = settings.knowledge_dir / "policy.md"
    source.write_text("差旅住宿标准为每晚五百元。" * 240, encoding="utf-8")
    asset = asset_store.create("长制度.md", source.name)
    processor.process(asset.id)
    recorded_count = asset_store.get(asset.id).chunk_count
    assert recorded_count > 1
    knowledge_base.chunks.pop()
    assert knowledge_base.count_for_asset(asset.id) == recorded_count - 1
    patch_startup_services(monkeypatch, asset_store, knowledge_base, processor)

    main.startup()

    assert knowledge_base.count_for_asset(asset.id) == recorded_count
    assert asset_store.get(asset.id).chunk_count == recorded_count


def test_startup_does_not_rebuild_a_consistent_ready_asset_with_zero_chunks(monkeypatch, tmp_path):
    settings, asset_store, knowledge_base, processor = build_startup_runtime(tmp_path)
    source = settings.knowledge_dir / "empty.md"
    source.write_text("", encoding="utf-8")
    asset = asset_store.create("空资料.md", source.name)
    processor.process(asset.id)
    processed = asset_store.get(asset.id)
    assert processed.status == "ready"
    assert processed.chunk_count == 0
    assert knowledge_base.chunks == []
    patch_startup_services(monkeypatch, asset_store, knowledge_base, processor)
    rebuild_calls = 0
    original_rebuild = knowledge_base.rebuild

    def count_rebuilds(assets, path_for):
        nonlocal rebuild_calls
        rebuild_calls += 1
        return original_rebuild(assets, path_for)

    monkeypatch.setattr(knowledge_base, "rebuild", count_rebuilds)

    main.startup()

    assert rebuild_calls == 0


def test_feedback_uses_the_normalized_conversation_id(monkeypatch):
    captured = {}
    canonical_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setattr(
        main,
        "agent",
        SimpleNamespace(
            get_conversation=lambda conversation_id: {
                "id": canonical_id,
                "project_id": "local-default",
                "messages": [{"id": "answer-1", "role": "assistant", "content": "回答"}],
            }
        ),
    )
    monkeypatch.setattr(
        main,
        "quality_store",
        SimpleNamespace(
            record_feedback=lambda conversation_id, message_id, project_id, rating, note: captured.update(
                conversation_id=conversation_id,
                message_id=message_id,
                project_id=project_id,
                rating=rating,
                note=note,
            )
            or captured
        ),
    )

    main.record_message_feedback(
        canonical_id.upper(),
        "answer-1",
        main.FeedbackRequest(rating="useful", note="准确"),
    )

    assert captured["conversation_id"] == canonical_id


def test_view_asset_returns_the_original_file_inline(monkeypatch, tmp_path):
    source_file = tmp_path / "notes.txt"
    source_file.write_text("本地资料", encoding="utf-8")
    asset = SimpleNamespace(
        status="ready",
        media_type="text/plain",
        original_name="notes.txt",
    )
    monkeypatch.setattr(
        main,
        "asset_store",
        SimpleNamespace(get=lambda _: asset, read_bytes=lambda _: "本地资料".encode()),
    )

    response = main.view_asset("asset-1")

    assert response.media_type == "text/plain"
    assert "本地资料".encode() in response.body


def test_citations_expose_an_inline_view_url_for_each_source_file():
    asset = SimpleNamespace(original_name="notes.txt", kind="text")
    agent = SimpleNamespace(
        asset_store=SimpleNamespace(get=lambda _: asset),
        _excerpt=KnowledgeAgent._excerpt,
    )

    citations = KnowledgeAgent._citations(
        agent,
        [{"asset_id": "asset-1", "chunk_no": 0, "score": 1.0, "text": "本地资料"}],
    )

    assert citations[0]["preview_url"] == "/api/assets/asset-1/view"


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


def test_upload_endpoint_forwards_and_returns_exact_file_size(monkeypatch, tmp_path):
    test_settings, asset_store, knowledge_base, processor = build_startup_runtime(tmp_path)
    monkeypatch.setattr(main, "settings", test_settings)
    monkeypatch.setattr(main, "s3_storage", None)
    monkeypatch.setattr(main, "asset_store", asset_store)
    monkeypatch.setattr(main, "knowledge_base", knowledge_base)
    monkeypatch.setattr(main, "processor", processor)
    monkeypatch.setattr(main, "require_project", lambda _: None)
    background_tasks = BackgroundTasks()
    payload = b"exact upload bytes"

    response = asyncio.run(
        main.upload_file(
            background_tasks,
            UploadFile(filename="guide.md", file=BytesIO(payload)),
            project_id="project-test",
        )
    )

    stored = asset_store.get(response["asset_id"])
    assert response["size_bytes"] == len(payload)
    assert stored.size_bytes == len(payload)
    assert len(background_tasks.tasks) == 1


def test_replace_endpoint_forwards_and_returns_exact_file_size(monkeypatch, tmp_path):
    test_settings, asset_store, knowledge_base, processor = build_startup_runtime(tmp_path)
    current = asset_store.create("old.md", "old.md", project_id="project-test", size_bytes=3)
    monkeypatch.setattr(main, "settings", test_settings)
    monkeypatch.setattr(main, "s3_storage", None)
    monkeypatch.setattr(main, "asset_store", asset_store)
    monkeypatch.setattr(main, "knowledge_base", knowledge_base)
    monkeypatch.setattr(main, "processor", processor)
    background_tasks = BackgroundTasks()
    payload = b"exact replacement bytes"

    response = asyncio.run(
        main.replace_asset(
            current.id,
            background_tasks,
            UploadFile(filename="new.md", file=BytesIO(payload)),
        )
    )

    replacement = asset_store.get(response["asset_id"])
    assert response["size_bytes"] == len(payload)
    assert replacement.size_bytes == len(payload)
    assert asset_store.get(current.id).size_bytes == 3
    assert len(background_tasks.tasks) == 1
