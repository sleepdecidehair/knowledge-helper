import json
import re
import uuid
from pathlib import Path
from typing import Dict, List, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import AgentRunError, KnowledgeAgent
from app.assets import AssetStore, SUPPORTED_SUFFIXES
from app.config import settings
from app.ingestion import AssetProcessor
from app.knowledge_base import KnowledgeBase
from app.knowledge_writer import KnowledgeWriter
from app.workspace import AgentProfileStore, DEFAULT_PROJECT_ID, ProjectStore, RuntimeSettingsStore


asset_store = AssetStore(settings)
knowledge_base = KnowledgeBase(settings)
processor = AssetProcessor(settings, asset_store, knowledge_base)
knowledge_writer = KnowledgeWriter(asset_store, processor)
project_store = ProjectStore(settings)
profile_store = AgentProfileStore(settings)
runtime_store = RuntimeSettingsStore(settings)
agent = KnowledgeAgent(settings, knowledge_base, asset_store, knowledge_writer, profiles=profile_store, runtime_settings=runtime_store)
app = FastAPI(title="本地多模态知识库问答助手", version="0.4.0")
frontend_dist_dir = settings.project_root / "frontend" / "dist"
frontend_assets_dir = frontend_dist_dir / "assets"

if frontend_assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=frontend_assets_dir), name="frontend-assets")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = Field(default=None, max_length=64)
    attachment_asset_ids: List[str] = Field(default_factory=list, max_length=20)


class ConversationCreateRequest(BaseModel):
    project_id: str = Field(default=DEFAULT_PROJECT_ID, min_length=1, max_length=64)


class ConversationUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=80)
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    pinned: Optional[bool] = None


class ConversationBranchRequest(BaseModel):
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=64)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    icon: str = Field(default="✦", max_length=4)
    color: str = Field(default="indigo", max_length=20)
    instructions: str = Field(default="", max_length=4000)


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    icon: Optional[str] = Field(default=None, max_length=4)
    color: Optional[str] = Field(default=None, max_length=20)
    instructions: Optional[str] = Field(default=None, max_length=4000)


class RuntimeSettingsRequest(BaseModel):
    deepseek_model: str = Field(min_length=1, max_length=120)
    deepseek_base_url: str = Field(min_length=1, max_length=300)
    deepseek_api_key: Optional[str] = Field(default=None, max_length=512)
    agent_max_turns: int = Field(ge=1, le=12)
    agent_context_compaction_tokens: int = Field(ge=8_000, le=120_000)


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    project_id: str = Field(default=DEFAULT_PROJECT_ID, min_length=1, max_length=64)


class KnowledgeWriteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=4, max_length=12000)


class AssetUpdateRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)


class ChunkingRequest(BaseModel):
    chunk_size: int = Field(ge=300, le=3000)
    chunk_overlap: int = Field(ge=0, le=1500)


class PipelineRequest(BaseModel):
    chunk_size: int = Field(ge=300, le=3000)
    chunk_overlap: int = Field(ge=0, le=1500)
    boundary_mode: Literal["natural", "fixed"]
    pdf_chunk_scope: Literal["page", "document"]
    image_index_mode: Literal["attachment_only", "skip"]
    top_k: int = Field(ge=1, le=12)
    minimum_score: float = Field(ge=0, le=10)
    embedding_adapter: Literal["unconfigured", "local_openai_compatible"]
    embedding_model: str = Field(default="", max_length=200)
    embedding_base_url: str = Field(default="", max_length=300)
    vector_weight: float = Field(ge=0, le=1)
    reranker_adapter: Literal["unconfigured", "local_openai_compatible"]
    reranker_model: str = Field(default="", max_length=200)
    reranker_base_url: str = Field(default="", max_length=300)
    rerank_top_n: int = Field(ge=1, le=100)


def require_project(project_id: str) -> None:
    if not project_store.exists(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")


def rebuild_ready_assets() -> Dict[str, int]:
    if asset_store.ready_assets():
        return knowledge_base.rebuild(asset_store.ready_assets(), asset_store.path_for)
    return knowledge_base.status()


@app.on_event("startup")
def startup() -> None:
    project_store.load()
    profile_store.load()
    runtime_store.load()
    asset_store.load()
    asset_store.recover_interrupted_jobs()
    asset_store.register_existing_files()
    knowledge_base.load()
    for asset in asset_store.queued_assets():
        processor.process(asset.id)
    if not knowledge_base.chunks and asset_store.ready_assets():
        knowledge_base.rebuild(asset_store.ready_assets(), asset_store.path_for)
    agent.initialize()


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    frontend_index = frontend_dist_dir / "index.html"
    return FileResponse(frontend_index if frontend_index.is_file() else Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status(project_id: str = Query(default=DEFAULT_PROJECT_ID)) -> Dict[str, object]:
    require_project(project_id)
    assets = asset_store.all_assets(project_id)
    ready_ids = asset_store.ready_asset_ids(project_id)
    project_chunks = [chunk for chunk in knowledge_base.chunks if chunk.asset_id in ready_ids]
    runtime = runtime_store.get()
    return {
        "documents": len({chunk.asset_id for chunk in project_chunks}), "chunks": len(project_chunks),
        "chunking": knowledge_base.chunking_status(), "pipeline": knowledge_base.pipeline_status(),
        "assets": len(assets), "processing": sum(asset.status in {"queued", "processing"} for asset in assets),
        "deepseek_configured": bool(runtime["api_key_configured"]), "model": runtime["deepseek_model"],
        "agent_sdk_ready": agent.runner.is_ready(), "agent_mode": "Claude Agent SDK + local MCP",
        "supported_files": sorted(SUPPORTED_SUFFIXES), "runtime": runtime,
    }


@app.get("/api/workspace-settings")
def get_workspace_settings() -> Dict[str, object]:
    return runtime_store.get()


@app.put("/api/workspace-settings")
def update_workspace_settings(request: RuntimeSettingsRequest) -> Dict[str, object]:
    try:
        payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
        api_key = payload.pop("deepseek_api_key", None)
        return runtime_store.update(deepseek_api_key=api_key, **payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects")
def list_projects() -> Dict[str, object]:
    projects = project_store.list()
    for project in projects:
        project_id = str(project["id"])
        project["asset_count"] = len(asset_store.all_assets(project_id))
        project["conversation_count"] = len(agent.list_conversations(project_id))
    return {"projects": projects}


@app.post("/api/projects")
def create_project(request: ProjectCreateRequest) -> Dict[str, object]:
    try:
        return project_store.create(request.name, request.icon, request.color, request.instructions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> Dict[str, object]:
    project = project_store.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    project["asset_count"] = len(asset_store.all_assets(project_id))
    project["conversation_count"] = len(agent.list_conversations(project_id))
    return project


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, request: ProjectUpdateRequest) -> Dict[str, object]:
    try:
        payload = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else request.dict(exclude_none=True)
        return project_store.update(project_id, **payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> Dict[str, object]:
    if asset_store.all_assets(project_id) or agent.list_conversations(project_id):
        raise HTTPException(status_code=409, detail="项目仍包含会话或文件，请先移动或删除其中内容。")
    try:
        return project_store.delete(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/assets")
def list_assets(project_id: str = Query(default=DEFAULT_PROJECT_ID)) -> Dict[str, object]:
    require_project(project_id)
    return {"assets": [asset_store.public(asset) for asset in asset_store.all_assets(project_id)]}


@app.patch("/api/assets/{asset_id}")
def update_asset(asset_id: str, request: AssetUpdateRequest) -> Dict[str, object]:
    require_project(request.project_id)
    try:
        return asset_store.public(asset_store.update(asset_id, project_id=request.project_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc


@app.delete("/api/assets/{asset_id}")
def delete_asset(asset_id: str) -> Dict[str, object]:
    try:
        deleted = asset_store.delete(asset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    rebuild_ready_assets()
    return {"deleted": asset_id, "name": deleted.original_name}


@app.get("/api/conversations")
def list_conversations(project_id: Optional[str] = Query(default=None), q: str = Query(default="", max_length=120)) -> Dict[str, object]:
    if project_id:
        require_project(project_id)
    return {"conversations": agent.list_conversations(project_id, q)}


@app.post("/api/conversations")
def create_conversation(request: ConversationCreateRequest) -> Dict[str, object]:
    require_project(request.project_id)
    return agent.create_conversation(request.project_id)


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> Dict[str, object]:
    try:
        conversation = agent.get_conversation(conversation_id)
    except AgentRunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation


@app.patch("/api/conversations/{conversation_id}")
def update_conversation(conversation_id: str, request: ConversationUpdateRequest) -> Dict[str, object]:
    payload = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else request.dict(exclude_none=True)
    if "project_id" in payload:
        require_project(str(payload["project_id"]))
    try:
        return agent.update_conversation(conversation_id, **payload)
    except (KeyError, AgentRunError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/conversations/{conversation_id}/branch")
def branch_conversation(conversation_id: str, request: ConversationBranchRequest) -> Dict[str, object]:
    source = agent.get_conversation(conversation_id)
    if not source:
        raise HTTPException(status_code=404, detail="会话不存在")
    project_id = request.project_id or str(source["project_id"])
    require_project(project_id)
    try:
        return agent.branch_conversation(conversation_id, project_id)
    except (KeyError, AgentRunError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> Dict[str, object]:
    try:
        deleted = agent.delete_conversation(conversation_id)
    except AgentRunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": conversation_id}


@app.get("/api/conversations/{conversation_id}/export")
def export_conversation(conversation_id: str, format: Literal["markdown", "json"] = Query(default="markdown")):
    conversation = agent.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    if format == "json":
        content = __import__("json").dumps(conversation, ensure_ascii=False, indent=2)
        return PlainTextResponse(content, media_type="application/json", headers={"Content-Disposition": 'attachment; filename="conversation.json"'})
    lines = [f"# {conversation['title']}", ""]
    for message in conversation.get("messages", []):
        role = "用户" if message.get("role") == "user" else "助手"
        lines.extend([f"## {role}", str(message.get("content") or ""), ""])
    return PlainTextResponse("\n".join(lines), media_type="text/markdown", headers={"Content-Disposition": 'attachment; filename="conversation.md"'})


@app.get("/api/chunking")
def get_chunking() -> Dict[str, int]:
    return knowledge_base.chunking_status()


@app.get("/api/pipeline")
def get_pipeline() -> Dict[str, object]:
    return knowledge_base.pipeline_status()


@app.put("/api/pipeline")
def update_pipeline(request: PipelineRequest) -> Dict[str, object]:
    try:
        request_payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
        pipeline = knowledge_base.update_pipeline(**request_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**pipeline, **rebuild_ready_assets(), "reindexed": True}


@app.put("/api/chunking")
def update_chunking(request: ChunkingRequest) -> Dict[str, int]:
    try:
        chunking = knowledge_base.update_chunking(request.chunk_size, request.chunk_overlap)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rebuild_ready_assets()
    return chunking


@app.post("/api/index/rebuild")
def rebuild_index(background_tasks: BackgroundTasks) -> Dict[str, object]:
    asset_store.register_existing_files()
    asset_store.requeue_failed_assets()
    for asset in asset_store.queued_assets():
        background_tasks.add_task(processor.process, asset.id)
    return {**rebuild_ready_assets(), "queued": len(asset_store.queued_assets())}


@app.post("/api/upload")
async def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...), project_id: str = Form(default=DEFAULT_PROJECT_ID)) -> Dict[str, object]:
    require_project(project_id)
    original_name = file.filename or ""
    safe_name = Path(original_name).name
    suffix = Path(safe_name).suffix.lower()
    if not safe_name or suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 txt、md、markdown、pdf、png、jpg、jpeg、webp 文件。")
    payload = await file.read(settings.max_upload_bytes + 1)
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="文件超过 MAX_UPLOAD_MB 限制。")
    stem = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", Path(safe_name).stem).strip("_") or "document"
    stored_name = f"{uuid.uuid4().hex[:8]}_{stem}{suffix}"
    settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
    (settings.knowledge_dir / stored_name).write_bytes(payload)
    asset = asset_store.create(safe_name, stored_name, project_id)
    background_tasks.add_task(processor.process, asset.id)
    return asset_store.public(asset)


@app.get("/api/assets/{asset_id}/download")
def download_asset(asset_id: str) -> FileResponse:
    asset = asset_store.get(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        return FileResponse(asset_store.path_for(asset), media_type=asset.media_type, filename=asset.original_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc


@app.get("/api/assets/{asset_id}/preview")
def preview_asset(asset_id: str, page: Optional[int] = Query(default=None, ge=1)) -> FileResponse:
    asset = asset_store.get(asset_id)
    if not asset or asset.status != "ready":
        raise HTTPException(status_code=404, detail="预览尚不可用")
    if asset.kind not in {"pdf", "image"}:
        raise HTTPException(status_code=404, detail="该文件不支持预览")
    try:
        preview_path = processor.ensure_preview(asset, page)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="预览不存在") from exc
    return FileResponse(preview_path, media_type="image/png" if asset.kind == "pdf" else "image/jpeg")


@app.post("/api/chat")
def chat(request: ChatRequest) -> Dict[str, object]:
    try:
        return agent.answer(
            request.question.strip(),
            request.conversation_id,
            attachment_asset_ids=request.attachment_asset_ids,
        )
    except AgentRunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Agent SDK 调用失败，请检查本地服务、DeepSeek 配置和网络后重试。") from exc


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    question = request.question.strip()

    def event_source():
        if not question:
            payload = {"message": "问题不能为空。"}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        answer_events = agent.answer_stream(
            question,
            request.conversation_id,
            attachment_asset_ids=request.attachment_asset_ids,
        )
        try:
            for item in answer_events:
                event_name = str(item.get("event") or "message")
                data = item.get("data")
                payload = data if isinstance(data, dict) else {}
                yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except AgentRunError as exc:
            payload = {"message": str(exc)}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception:
            payload = {"message": "Agent SDK 调用失败，请检查本地服务、DeepSeek 配置和网络后重试。"}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            answer_events.close()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def require_agent_tool_token(x_agent_tool_token: Optional[str] = Header(default=None)) -> None:
    if not agent.has_valid_tool_token(x_agent_tool_token):
        raise HTTPException(status_code=403, detail="本地 Agent 工具认证失败。")


@app.post("/api/internal/agent/search", include_in_schema=False)
def agent_search(request: KnowledgeSearchRequest, x_agent_tool_token: Optional[str] = Header(default=None)) -> Dict[str, object]:
    require_agent_tool_token(x_agent_tool_token)
    require_project(request.project_id)
    return agent.search_knowledge(request.query.strip(), request.project_id)


@app.post("/api/internal/agent/write-note", include_in_schema=False)
def agent_write_note(
    request: KnowledgeWriteRequest,
    x_agent_tool_token: Optional[str] = Header(default=None),
    x_agent_write_grant: Optional[str] = Header(default=None),
    x_agent_project_id: Optional[str] = Header(default=None),
) -> Dict[str, object]:
    require_agent_tool_token(x_agent_tool_token)
    project_id = x_agent_project_id or DEFAULT_PROJECT_ID
    require_project(project_id)
    try:
        return agent.write_knowledge_note(x_agent_write_grant or "", request.title, request.content, project_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
