from types import SimpleNamespace

from app.agent import KnowledgeAgent
from app import main


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
