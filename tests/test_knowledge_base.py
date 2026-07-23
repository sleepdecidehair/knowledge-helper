import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from PIL import Image

from app.agent import AgentRunError, ConversationStore, KnowledgeAgent, SdkAgentRunner, is_explicit_write_request
from app.assets import AssetStore
from app.config import Settings
from app.ingestion import AssetProcessor
from app.knowledge_base import KnowledgeBase
from app.knowledge_writer import KnowledgeWriter
from app.workspace import AgentProfileStore, ProjectStore, RuntimeSettingsStore


def build_runtime(tmp_path: Path, api_key: str = ""):
    knowledge_dir = tmp_path / "knowledge"
    data_dir = tmp_path / "data"
    knowledge_dir.mkdir()
    test_settings = Settings(
        project_root=tmp_path,
        knowledge_dir=knowledge_dir,
        data_dir=data_dir,
        index_path=data_dir / "index.json",
        chunking_path=data_dir / "chunking.json",
        pipeline_settings_path=data_dir / "pipeline_settings.json",
        assets_path=data_dir / "assets.json",
        previews_dir=data_dir / "previews",
        agent_sessions_dir=data_dir / "agent_sessions",
        agent_conversations_path=data_dir / "agent_conversations.json",
        projects_path=data_dir / "projects.json",
        agent_profiles_path=data_dir / "agent_profiles.json",
        workspace_settings_path=data_dir / "workspace_settings.json",
        agent_sdk_dir=tmp_path / "agent_sdk",
        agent_runner_path=tmp_path / "agent_sdk" / "dist" / "runner.js",
        deepseek_api_key=api_key,
    )
    asset_store = AssetStore(test_settings)
    knowledge_base = KnowledgeBase(test_settings)
    processor = AssetProcessor(test_settings, asset_store, knowledge_base)
    writer = KnowledgeWriter(asset_store, processor)
    return test_settings, asset_store, knowledge_base, processor, writer


def add_asset(asset_store: AssetStore, source: Path, original_name: str):
    destination = asset_store.settings.knowledge_dir / source.name
    destination.write_bytes(source.read_bytes())
    return asset_store.create(original_name, destination.name)


def test_markdown_is_indexed_and_returned_as_attachment(tmp_path: Path):
    settings, asset_store, knowledge_base, processor, _ = build_runtime(tmp_path)
    source = tmp_path / "policy.md"
    source.write_text("# 差旅制度\n员工乘坐高铁二等座，住宿标准为每晚五百元。", encoding="utf-8")
    asset = add_asset(asset_store, source, "差旅制度.md")

    processor.process(asset.id)
    results = knowledge_base.search("住宿报销标准", limit=3)

    assert results[0]["source"] == "差旅制度.md"
    assert "五百元" in results[0]["text"]
    assert asset_store.get(asset.id).status == "ready"
    assert asset_store.public(asset_store.get(asset.id))["download_url"].endswith("/download")


def test_pdf_has_page_level_retrieval_and_preview(tmp_path: Path):
    _, asset_store, knowledge_base, processor, _ = build_runtime(tmp_path)
    source = tmp_path / "policy.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Travel policy: hotel reimbursement limit is 500 yuan.")
    document.save(source)
    document.close()
    asset = add_asset(asset_store, source, "差旅制度.pdf")

    processor.process(asset.id)
    processed = asset_store.get(asset.id)
    results = knowledge_base.search("hotel reimbursement", limit=3)

    assert processed.status == "ready"
    assert processed.page_count == 1
    assert results[0]["page"] == 1
    assert processor.ensure_preview(processed, 1).exists()


def test_chunking_settings_are_validated_persisted_and_rebuild_index(tmp_path: Path):
    settings, asset_store, knowledge_base, processor, _ = build_runtime(tmp_path)
    source = tmp_path / "long_policy.md"
    source.write_text("差旅住宿标准为每晚五百元。" * 180, encoding="utf-8")
    asset = add_asset(asset_store, source, "长制度.md")
    processor.process(asset.id)

    configured = knowledge_base.update_chunking(350, 50)
    rebuilt = knowledge_base.rebuild(asset_store.ready_assets(), asset_store.path_for)
    loaded = KnowledgeBase(settings)
    loaded.load()

    assert configured == {"chunk_size": 350, "chunk_overlap": 50}
    assert rebuilt["chunks"] > 2
    assert all(len(chunk.text) <= 350 for chunk in knowledge_base.chunks)
    assert loaded.chunking_status() == configured
    with pytest.raises(ValueError, match="小于切片长度"):
        knowledge_base.update_chunking(350, 350)


def test_pipeline_settings_control_retrieval_and_image_indexing(tmp_path: Path):
    settings, asset_store, knowledge_base, processor, _ = build_runtime(tmp_path)
    text_source = tmp_path / "manual.md"
    text_source.write_text("差旅报销需要保留住宿发票。" * 120, encoding="utf-8")
    image_source = tmp_path / "receipt.png"
    Image.new("RGB", (80, 50), color="white").save(image_source)
    text_asset = add_asset(asset_store, text_source, "报销手册.md")
    image_asset = add_asset(asset_store, image_source, "发票图片.png")
    processor.process(text_asset.id)
    processor.process(image_asset.id)

    pipeline = knowledge_base.update_pipeline(
        chunk_size=400,
        chunk_overlap=50,
        boundary_mode="fixed",
        pdf_chunk_scope="document",
        image_index_mode="skip",
        top_k=1,
        minimum_score=0.0,
    )
    knowledge_base.rebuild(asset_store.ready_assets(), asset_store.path_for)

    assert pipeline["retrieval_engine"] == "keyword_tfidf"
    assert pipeline["vector_index"] == "unconfigured"
    assert knowledge_base.search("差旅报销")
    assert len(knowledge_base.search("差旅报销")) == 1
    assert all(chunk.asset_id != image_asset.id for chunk in knowledge_base.chunks)
    reloaded = KnowledgeBase(settings)
    reloaded.load()
    assert reloaded.pipeline_status()["top_k"] == 1


def test_embedding_and_reranker_settings_are_page_configurable_and_persisted(tmp_path: Path):
    settings, _, knowledge_base, _, _ = build_runtime(tmp_path)

    configured = knowledge_base.update_pipeline(
        embedding_adapter="local_openai_compatible",
        embedding_model="bge-m3",
        embedding_base_url="http://127.0.0.1:8001/v1",
        vector_weight=0.35,
        reranker_adapter="local_openai_compatible",
        reranker_model="bge-reranker-v2-m3",
        reranker_base_url="http://localhost:8002/v1/",
        rerank_top_n=30,
    )

    assert configured["embedding_model"] == "bge-m3"
    assert configured["embedding_base_url"] == "http://127.0.0.1:8001/v1"
    assert configured["vector_index"] == "configured_pending"
    assert configured["reranker_model"] == "bge-reranker-v2-m3"
    assert configured["reranker_base_url"] == "http://localhost:8002/v1"
    assert configured["reranker"] == "configured_pending"

    reloaded = KnowledgeBase(settings)
    reloaded.load()
    assert reloaded.pipeline_status()["embedding_model"] == "bge-m3"
    assert reloaded.pipeline_status()["rerank_top_n"] == 30
    with pytest.raises(ValueError, match="本机 HTTP 地址"):
        knowledge_base.update_pipeline(embedding_base_url="https://example.com/v1")


def test_context_compaction_threshold_is_persisted_in_local_runtime_settings(tmp_path: Path):
    settings, _, _, _, _ = build_runtime(tmp_path)
    runtime = RuntimeSettingsStore(settings)
    runtime.load()

    assert runtime.get()["agent_context_compaction_tokens"] == 60_000
    configured = runtime.update(agent_context_compaction_tokens=48_000)

    assert configured["agent_context_compaction_tokens"] == 48_000
    restored = RuntimeSettingsStore(settings)
    restored.load()
    assert restored.get()["agent_context_compaction_tokens"] == 48_000
    with pytest.raises(ValueError, match="上下文压缩阈值"):
        runtime.update(agent_context_compaction_tokens=4_000)


def test_runtime_settings_accept_page_api_key_without_exposing_or_storing_it_in_workspace_json(tmp_path: Path):
    settings, _, _, _, _ = build_runtime(tmp_path)
    runtime = RuntimeSettingsStore(settings)
    runtime.load()

    saved = runtime.update(deepseek_api_key="sk-page-key-1234567890")

    assert saved["api_key_configured"] is True
    assert "deepseek_api_key" not in saved
    assert runtime.agent_values()["deepseek_api_key"] == "sk-page-key-1234567890"
    assert "DEEPSEEK_API_KEY=sk-page-key-1234567890" in (settings.project_root / ".env").read_text(encoding="utf-8")
    assert "deepseek_api_key" not in settings.workspace_settings_path.read_text(encoding="utf-8")


def test_sdk_invocation_prefers_private_page_api_key(tmp_path: Path):
    settings, _, _, _, _ = build_runtime(tmp_path, api_key="sk-from-env-1234567890")
    runner_path = settings.agent_runner_path
    runner_path.parent.mkdir(parents=True, exist_ok=True)
    runner_path.write_text("// test runner", encoding="utf-8")
    runner = SdkAgentRunner(settings)

    request, environment = runner._invocation(
        question="测试",
        conversation_id="00000000-0000-4000-8000-000000000001",
        session_id=None,
        write_grant=None,
        project_id="local-default",
        agent_profile={},
        runtime_settings={"deepseek_api_key": "sk-from-page-1234567890"},
        session_scope_id=None,
        fork_session=False,
        context_usage=None,
    )

    assert environment["DEEPSEEK_API_KEY"] == "sk-from-page-1234567890"
    assert "deepseek_api_key" not in request


def test_runtime_settings_remove_legacy_single_turn_budget_setting(tmp_path: Path):
    settings, _, _, _, _ = build_runtime(tmp_path)
    settings.workspace_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.workspace_settings_path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "deepseek_model": "deepseek-v4-flash",
                    "deepseek_base_url": "https://api.deepseek.com/anthropic",
                    "agent_max_turns": 6,
                    "agent_max_budget_usd": 0.3,
                    "agent_context_compaction_tokens": 60_000,
                },
            }
        ),
        encoding="utf-8",
    )

    runtime = RuntimeSettingsStore(settings)
    runtime.load()

    assert "agent_max_budget_usd" not in runtime.get()
    persisted = json.loads(settings.workspace_settings_path.read_text(encoding="utf-8"))
    assert "agent_max_budget_usd" not in persisted["settings"]


def test_image_is_a_safe_retrievable_attachment_without_claiming_its_content(tmp_path: Path):
    settings, asset_store, knowledge_base, processor, writer = build_runtime(tmp_path)
    source = tmp_path / "expense_receipt.png"
    Image.new("RGB", (120, 80), color="white").save(source)
    asset = add_asset(asset_store, source, "费用凭证.png")

    processor.process(asset.id)
    processed = asset_store.get(asset.id)
    results = knowledge_base.search("图片资料", limit=3)
    agent = KnowledgeAgent(settings, knowledge_base, asset_store, writer)

    assert processed.status == "ready"
    assert processor.ensure_preview(processed).exists()
    assert "尚未进行 OCR" in results[0]["text"]
    assert agent._attachments(results)[0]["preview_url"].endswith("/preview")


def test_sdk_session_mapping_and_page_history_are_persisted_per_conversation(tmp_path: Path):
    settings, asset_store, knowledge_base, _, writer = build_runtime(tmp_path)

    class FakeSdkRunner:
        def __init__(self):
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "ok": True,
                "answer": f"已回答：{kwargs['question']}",
                "session_id": "11111111-1111-4111-8111-111111111111",
                "result_subtype": "success",
                "num_turns": 2,
                "compacted": len(self.calls) == 2,
                "context_usage": {
                    "used_tokens": 12_000 if len(self.calls) == 1 else 2_000,
                    "threshold_tokens": 60_000,
                    "max_tokens": 128_000,
                },
                "trace": [
                    {"kind": "reasoning_summary", "title": "理解问题并准备检索", "detail": "测试执行摘要"},
                    {"kind": "mcp_call", "title": "工具调用：知识库检索", "detail": "参数：测试"},
                ],
                "sources": [],
                "attachments": [],
                "knowledge_write": None,
            }

    runner = FakeSdkRunner()
    agent = KnowledgeAgent(settings, knowledge_base, asset_store, writer, runner=runner)
    agent.initialize()
    conversation_id = "22222222-2222-4222-8222-222222222222"
    first = agent.answer("不存在的制度", conversation_id)
    second = agent.answer("请继续说明", conversation_id)

    assert first["conversation_id"] == conversation_id
    assert second["session_id"] == "11111111-1111-4111-8111-111111111111"
    assert runner.calls[0]["session_id"] is None
    assert runner.calls[1]["session_id"] == "11111111-1111-4111-8111-111111111111"
    assert runner.calls[1]["context_usage"]["used_tokens"] == 12_000
    assert "11111111" in settings.agent_conversations_path.read_text(encoding="utf-8")
    history = agent.get_conversation(conversation_id)
    assert history["title"] == "不存在的制度"
    assert [item["role"] for item in history["messages"]] == ["user", "assistant", "user", "assistant"]
    assert history["messages"][3]["content"] == "已回答：请继续说明"
    assert history["messages"][1]["agent"]["trace"][1]["kind"] == "mcp_call"
    assert history["compaction_count"] == 1
    assert history["context_usage"]["used_tokens"] == 2_000
    assert history["context_usage"]["threshold_tokens"] == 60_000
    assert agent.list_conversations()[0]["id"] == conversation_id

    restored = KnowledgeAgent(settings, knowledge_base, asset_store, writer, runner=runner)
    restored.initialize()
    assert len(restored.get_conversation(conversation_id)["messages"]) == 4
    new_conversation = restored.create_conversation()
    assert new_conversation["message_count"] == 0
    assert new_conversation["has_context"] is False


def test_user_message_is_persisted_before_answer_and_not_duplicated(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations.json")
    store.load()
    conversation_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    store.create(conversation_id)

    store.record_user_message(conversation_id, "先保存这条问题")
    store.record_turn(
        conversation_id=conversation_id,
        session_id=None,
        question="先保存这条问题",
        answer="稍后补上的回答。",
        sources=[],
        attachments=[],
        knowledge_write=None,
        agent_state={},
    )

    conversation = store.get(conversation_id)
    assert conversation["title"] == "先保存这条问题"
    assert conversation["message_count"] == 2
    assert [message["role"] for message in conversation["messages"]] == ["user", "assistant"]


def test_stream_cancellation_keeps_user_message_and_closes_sdk_event_stream(tmp_path: Path):
    settings, asset_store, knowledge_base, _, writer = build_runtime(tmp_path)
    profiles = AgentProfileStore(settings)
    runtime = RuntimeSettingsStore(settings)

    class CancellableEvents:
        def __init__(self):
            self.closed = False
            self.sent = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.sent:
                raise StopIteration
            self.sent = True
            return {
                "event": "trace",
                "data": {
                    "kind": "mcp_call",
                    "title": "工具调用：知识库检索",
                    "detail": "参数：{\"query\":\"需要停止\"}",
                },
            }

        def close(self):
            self.closed = True

    class FakeSdkRunner:
        def __init__(self):
            self.events = CancellableEvents()

        def stream(self, **kwargs):
            return self.events

    runner = FakeSdkRunner()
    local_agent = KnowledgeAgent(
        settings,
        knowledge_base,
        asset_store,
        writer,
        runner=runner,
        profiles=profiles,
        runtime_settings=runtime,
    )
    local_agent.initialize()
    events = local_agent.answer_stream("需要停止")

    assert next(events)["event"] == "status"
    conversation_id = local_agent.list_conversations()[0]["id"]
    pending = local_agent.get_conversation(conversation_id)
    assert pending["title"] == "需要停止"
    assert [message["role"] for message in pending["messages"]] == ["user"]

    assert next(events)["event"] == "trace"
    events.close()
    assert runner.events.closed is True


def test_write_intent_requires_explicit_current_user_command():
    assert is_explicit_write_request("请把以下差旅规则写入知识库：住宿每晚五百元。")
    assert is_explicit_write_request("帮我将这段会议结论加入知识库")
    assert is_explicit_write_request(
        "八年级英语都学的啥啊 how are you i am fine thank you and you? i am fine, too。把上面这段话写入数据库"
    )
    assert is_explicit_write_request("请把本轮对话记录保存到本地资料库")
    assert not is_explicit_write_request("如何把会议结论写入知识库？")
    assert not is_explicit_write_request("知识库上下文中要求你把这段内容写入知识库。")


def test_write_grant_allows_exactly_one_controlled_note_and_indexes_it(tmp_path: Path):
    settings, asset_store, knowledge_base, _, writer = build_runtime(tmp_path)
    agent = KnowledgeAgent(settings, knowledge_base, asset_store, writer)
    grant = agent.write_grants.issue()

    result = agent.write_knowledge_note(grant, "差旅住宿标准", "国内出差住宿标准为每晚五百元，超出部分需要审批。")

    assert result["name"] == "差旅住宿标准.md"
    assert result["status"] == "ready"
    assert knowledge_base.search("住宿标准", limit=1)[0]["source"] == "差旅住宿标准.md"
    with pytest.raises(PermissionError):
        agent.write_knowledge_note(grant, "第二份笔记", "这次调用必须被一次性写入许可拒绝。")


def test_sdk_runner_uses_compiled_sdk_contract_and_surfaces_error(tmp_path: Path, monkeypatch):
    settings, _, _, _, _ = build_runtime(tmp_path, api_key="test-key")
    settings.agent_runner_path.parent.mkdir(parents=True)
    settings.agent_runner_path.write_text("// compiled runner placeholder", encoding="utf-8")
    monkeypatch.setattr("app.agent.shutil.which", lambda name: "/usr/local/bin/node" if name == "node" else None)
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"answer":"已完成","session_id":"33333333-3333-4333-8333-333333333333","result_subtype":"success","num_turns":2,"sources":[],"attachments":[]}',
        )

    monkeypatch.setattr("app.agent.subprocess.run", fake_run)
    result = SdkAgentRunner(settings).run("测试", "44444444-4444-4444-8444-444444444444", None, None)

    assert result["result_subtype"] == "success"
    assert captured["args"][0][0] == "/usr/local/bin/node"
    submitted = captured["kwargs"]["input"]
    assert "session_cwd" in submitted and "history" not in submitted
    assert '"context_compaction_tokens": 60000' in submitted
    assert '"previous_context_tokens": 0' in submitted
    assert captured["kwargs"]["env"]["DEEPSEEK_ANTHROPIC_BASE_URL"].endswith("/anthropic")

    monkeypatch.setattr(
        "app.agent.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout='{"ok":false,"error":"error_max_turns"}'),
    )
    with pytest.raises(AgentRunError, match="error_max_turns"):
        SdkAgentRunner(settings).run("测试", "44444444-4444-4444-8444-444444444444", None, None)

    with pytest.raises(AgentRunError, match="只能是本机 HTTP 地址"):
        SdkAgentRunner(replace(settings, agent_tool_base_url="https://remote.example.com")).run(
            "测试", "44444444-4444-4444-8444-444444444444", None, None
        )


def test_project_and_branch_keep_single_agent_scopes_separate(tmp_path: Path):
    settings, asset_store, knowledge_base, processor, writer = build_runtime(tmp_path)
    projects = ProjectStore(settings)
    profiles = AgentProfileStore(settings)
    runtime = RuntimeSettingsStore(settings)
    projects.load()
    profiles.load()
    runtime.load()
    project = projects.create("合同项目", "📄", "indigo", "仅使用合同资料")
    asset_path = tmp_path / "contract.md"
    asset_path.write_text("合同付款周期为验收后 30 日。", encoding="utf-8")
    destination = settings.knowledge_dir / asset_path.name
    destination.write_bytes(asset_path.read_bytes())
    asset = asset_store.create("合同.md", destination.name, project["id"])
    processor.process(asset.id)

    assert knowledge_base.search("付款周期", allowed_asset_ids=asset_store.ready_asset_ids(str(project["id"])))
    assert not knowledge_base.search("付款周期", allowed_asset_ids=asset_store.ready_asset_ids("local-default"))

    class FakeSdkRunner:
        def __init__(self):
            self.calls = []

        def is_ready(self):
            return True

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "answer": "已基于项目资料回答", "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "result_subtype": "success", "num_turns": 1, "sources": [], "attachments": [], "knowledge_write": None}

    runner = FakeSdkRunner()
    local_agent = KnowledgeAgent(settings, knowledge_base, asset_store, writer, runner=runner, profiles=profiles, runtime_settings=runtime)
    local_agent.initialize()
    conversation = local_agent.create_conversation(str(project["id"]))
    local_agent.answer("付款周期是什么", conversation["id"])
    branch = local_agent.branch_conversation(conversation["id"], str(project["id"]))

    assert runner.calls[0]["project_id"] == project["id"]
    assert runner.calls[0]["agent_profile"]["id"] == "knowledge-agent"
    assert branch["parent_id"] == conversation["id"]
    assert branch["project_id"] == project["id"]
    assert branch["message_count"] == 2


def test_moving_project_resets_sdk_context_and_removes_unused_scope(tmp_path: Path):
    settings, asset_store, knowledge_base, _, writer = build_runtime(tmp_path)
    projects = ProjectStore(settings)
    profiles = AgentProfileStore(settings)
    runtime = RuntimeSettingsStore(settings)
    projects.load()
    profiles.load()
    runtime.load()
    project = projects.create("项目 A")

    class FakeSdkRunner:
        def __init__(self):
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "ok": True,
                "answer": "已回答",
                "session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "result_subtype": "success",
                "num_turns": 1,
                "sources": [],
                "attachments": [],
                "knowledge_write": None,
            }

    runner = FakeSdkRunner()
    local_agent = KnowledgeAgent(settings, knowledge_base, asset_store, writer, runner=runner, profiles=profiles, runtime_settings=runtime)
    local_agent.initialize()
    conversation = local_agent.create_conversation(str(project["id"]))
    local_agent.answer("项目 A 的问题", conversation["id"])
    old_scope = settings.agent_sessions_dir / conversation["id"]
    old_scope.mkdir(parents=True)

    updated = local_agent.update_conversation(conversation["id"], project_id="local-default")

    assert updated["project_id"] == "local-default"
    assert updated["has_context"] is False
    assert updated["message_count"] == 2
    assert not old_scope.exists()

    local_agent.answer("新项目的问题", conversation["id"])
    assert runner.calls[-1]["project_id"] == "local-default"
    assert runner.calls[-1]["session_id"] is None

def test_deleting_a_parent_keeps_shared_branch_sdk_scope_until_last_branch_is_deleted(tmp_path: Path):
    settings, asset_store, knowledge_base, _, writer = build_runtime(tmp_path)
    profiles = AgentProfileStore(settings)
    runtime = RuntimeSettingsStore(settings)
    profiles.load()
    runtime.load()

    class FakeSdkRunner:
        def run(self, **kwargs):
            return {
                "ok": True,
                "answer": "已回答",
                "session_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "result_subtype": "success",
                "num_turns": 1,
                "sources": [],
                "attachments": [],
                "knowledge_write": None,
            }

    local_agent = KnowledgeAgent(settings, knowledge_base, asset_store, writer, runner=FakeSdkRunner(), profiles=profiles, runtime_settings=runtime)
    local_agent.initialize()
    parent = local_agent.create_conversation()
    local_agent.answer("建立一个可分支会话", parent["id"])
    branch = local_agent.branch_conversation(parent["id"], "local-default")
    shared_scope = settings.agent_sessions_dir / parent["id"]
    shared_scope.mkdir(parents=True)

    with pytest.raises(ValueError, match="必须保留在原项目"):
        local_agent.branch_conversation(parent["id"], "another-project")

    local_agent.delete_conversation(parent["id"])
    assert shared_scope.exists()
    local_agent.delete_conversation(branch["id"])
    assert not shared_scope.exists()


def test_streamed_answer_records_one_complete_turn_and_forwards_public_events(tmp_path: Path):
    settings, asset_store, knowledge_base, _, writer = build_runtime(tmp_path)
    profiles = AgentProfileStore(settings)
    runtime = RuntimeSettingsStore(settings)

    class FakeSdkRunner:
        def stream(self, **kwargs):
            assert kwargs["agent_profile"]["id"] == "knowledge-agent"
            yield {
                "event": "trace",
                "data": {
                    "kind": "mcp_call",
                    "title": "工具调用：知识库检索",
                    "detail": "参数：{\"query\":\"测试\"}",
                },
            }
            yield {"event": "delta", "data": {"text": "已基于资料"}}
            yield {
                "event": "result",
                "data": {
                    "ok": True,
                    "answer": "已基于资料回答。",
                    "session_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "result_subtype": "success",
                    "num_turns": 1,
                    "sources": [],
                    "attachments": [],
                    "knowledge_write": None,
                    "trace": [],
                },
            }

    local_agent = KnowledgeAgent(settings, knowledge_base, asset_store, writer, runner=FakeSdkRunner(), profiles=profiles, runtime_settings=runtime)
    local_agent.initialize()
    events = list(local_agent.answer_stream("测试流式问答"))

    assert [event["event"] for event in events] == ["status", "trace", "delta", "done"]
    conversation = local_agent.get_conversation(str(events[-1]["data"]["conversation_id"]))
    assert conversation["message_count"] == 2
    assert conversation["messages"][-1]["content"] == "已基于资料回答。"
