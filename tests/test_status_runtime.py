from types import SimpleNamespace

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
