import json
import logging
import os
import re
import secrets
import select
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from app.assets import AssetStore
from app.config import Settings
from app.knowledge_base import KnowledgeBase
from app.knowledge_writer import KnowledgeWriter
from app.workspace import AgentProfileStore, DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, RuntimeSettingsStore


WRITE_ACTION = r"(?:写入|保存|添加|加入|收录|记录|存入|存进|放入)"
WRITE_TARGET = r"(?:(?:本地|当前|这个)?(?:知识库|资料库|数据库))"
# A write grant is a capability boundary, not a topic classifier.  A matching
# clause must begin as a direct imperative or an explicit "this content" form,
# and must end at the knowledge target.  This fails closed for questions such as
# "我应该把内容写入知识库吗" and for descriptive statements.
WRITE_COMMAND = re.compile(
    r"^\s*(?:"
    + r"(?:请|麻烦|帮我|请帮我|请将|把|将).{0,160}?"
    + r"|(?:这是|以下|上述|上面|这段|本轮|当前).{0,160}?"
    + r")"
    + WRITE_ACTION + r".{0,24}?" + WRITE_TARGET
    + r"\s*(?:[：:，,。.!！]|$)",
    re.DOTALL,
)
WRITE_QUESTION_PREFIX = re.compile(
    r"^(?:请问|如何|怎么|能否|是否|为什么|什么是|介绍(?:一下)?|(?:请)?说明)",
    re.DOTALL,
)
WRITE_NEGATION = re.compile(
    r"(?:不要|请勿|别|不需要|无需|不该|不必|不能|不应|不会|不用|未|不).{0,80}?"
    + WRITE_ACTION + r".{0,24}?" + WRITE_TARGET,
    re.DOTALL,
)
UNTRUSTED_WRITE_REFERENCE = re.compile(
    r"(?:知识库|资料库|数据库|文档|资料|上下文).{0,60}?"
    r"(?:要求|指示|提示|让你).{0,60}?"
    r"(?:写入|保存|添加|加入|收录|记录|存入|存进|放入)",
    re.DOTALL,
)


def is_explicit_write_request(question: str) -> bool:
    """只用当前用户消息决定是否给 SDK 暴露可写 MCP 工具。"""
    normalized = question.strip()
    if WRITE_QUESTION_PREFIX.search(normalized):
        return False
    if UNTRUSTED_WRITE_REFERENCE.search(normalized):
        return False
    for clause in (item.strip() for item in re.split(r"[。；;]+", normalized)):
        if not clause or WRITE_NEGATION.search(clause):
            continue
        if WRITE_COMMAND.search(clause):
            return True
    return False


class AgentRunError(RuntimeError):
    """SDK 子进程的可安全展示错误。"""


class ConversationStore:
    """会话存储。优先使用 MySQL，回退到本机 JSON 文件。"""

    def __init__(self, path: Path, mysql_enabled: bool = False, mysql_config: dict | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, object]] = {}
        self._mysql_enabled = mysql_enabled
        self._mysql_config = mysql_config or {}
        self._mysql_conn = None

    @property
    def _mysql(self):
        if not self._mysql_enabled or not self._mysql_config:
            return None
        if self._mysql_conn is None or not self._mysql_conn.open:
            try:
                import pymysql
                self._mysql_conn = pymysql.connect(
                    host=self._mysql_config.get("host", ""),
                    port=int(self._mysql_config.get("port", 3306)),
                    user=self._mysql_config.get("user", "kh_user"),
                    password=self._mysql_config.get("password", ""),
                    database=self._mysql_config.get("database", "knowledge"),
                    charset="utf8mb4",
                    autocommit=True,
                    connect_timeout=5,
                )
            except Exception as exc:
                logger.warning("MySQL 连接失败: %s", exc)
                return None
        return self._mysql_conn

    def load(self) -> None:
        with self._lock:
            # 优先从 MySQL 加载
            if self._mysql_enabled:
                try:
                    self._load_from_mysql_locked()
                    return
                except Exception:
                    pass
            # 回退到本地 JSON
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                return
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                items = payload.get("conversations", {})
            except (OSError, ValueError, TypeError):
                self._items = {}
                return
            if not isinstance(items, dict):
                self._items = {}
                return
            normalized_items: Dict[str, Dict[str, object]] = {}
            changed = False
            for conversation_id, raw in items.items():
                if not isinstance(conversation_id, str) or not isinstance(raw, dict):
                    changed = True
                    continue
                normalized = self._record_from_raw(conversation_id, raw)
                normalized_items[conversation_id] = normalized
                if normalized != raw:
                    changed = True
            self._items = normalized_items
            if changed:
                self._persist_locked()

    def migrate_to_single_agent(self) -> int:
        """保留页面历史，移除旧多智能体会话的 SDK 上下文归属。"""
        with self._lock:
            migrated = 0
            for conversation_id in list(self._items):
                record = self._record_locked(conversation_id)
                if record["agent_id"] != DEFAULT_AGENT_ID:
                    record["agent_id"] = DEFAULT_AGENT_ID
                    self._reset_sdk_context_locked(conversation_id, record)
                    record["updated_at"] = self._now()
                    migrated += 1
            if migrated:
                self._persist_locked()
            return migrated

    def remove_archival_state(self) -> int:
        """归档功能已移除；将旧归档会话恢复到正常历史列表。"""
        with self._lock:
            removed = 0
            for conversation_id in list(self._items):
                raw = self._items.get(conversation_id)
                if isinstance(raw, dict) and "archived" in raw:
                    del raw["archived"]
                    removed += 1
                self._record_locked(conversation_id)
            if removed:
                self._persist_locked()
            return removed

    def session_id_for(self, conversation_id: str) -> Optional[str]:
        with self._lock:
            value = self._items.get(conversation_id, {}).get("session_id")
            return value if isinstance(value, str) and value else None

    def create(
        self,
        conversation_id: str,
        project_id: str = DEFAULT_PROJECT_ID,
        agent_id: str = DEFAULT_AGENT_ID,
        parent_id: Optional[str] = None,
        fork_from_session_id: Optional[str] = None,
        sdk_scope_id: Optional[str] = None,
    ) -> Dict[str, object]:
        with self._lock:
            is_new = conversation_id not in self._items
            record = self._record_locked(conversation_id)
            if not is_new:
                return self._public(conversation_id, record, include_messages=True)
            record["project_id"] = project_id
            record["agent_id"] = agent_id
            record["parent_id"] = parent_id
            record["fork_from_session_id"] = fork_from_session_id or ""
            record["sdk_scope_id"] = sdk_scope_id or conversation_id
            self._persist_locked()
            return self._public(conversation_id, record, include_messages=True)

    def list(self, project_id: Optional[str] = None, query: str = "") -> List[Dict[str, object]]:
        with self._lock:
            normalized_query = query.strip().lower()
            conversations = []
            for conversation_id in self._items:
                record = self._record_locked(conversation_id)
                if project_id and record["project_id"] != project_id:
                    continue
                if normalized_query and normalized_query not in str(record["title"]).lower():
                    continue
                conversations.append(self._public(conversation_id, record))
            return sorted(
                conversations,
                key=lambda item: (bool(item["pinned"]), int(item["updated_at"]), str(item["id"])),
                reverse=True,
            )

    def get(self, conversation_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            if conversation_id not in self._items:
                return None
            return self._public(conversation_id, self._record_locked(conversation_id), include_messages=True)

    def record_user_message(
        self,
        conversation_id: str,
        question: str,
        user_attachments: Optional[List[Dict[str, object]]] = None,
    ) -> Dict[str, object]:
        """在启动模型前持久化用户输入，取消生成时也能保留本轮会话。"""
        with self._lock:
            record = self._record_locked(conversation_id)
            now = self._now()
            messages = record["messages"]
            assert isinstance(messages, list)
            user_message: Dict[str, object] = {
                "id": uuid.uuid4().hex,
                "role": "user",
                "content": question,
                "created_at": now,
            }
            if user_attachments:
                user_message["attachments"] = user_attachments
            messages.append(user_message)
            if record["title"] == "新建会话":
                record["title"] = self._title_for(question)
            record["updated_at"] = now
            self._persist_locked()
            return self._public(conversation_id, record, include_messages=True)

    def record_turn(
        self,
        conversation_id: str,
        session_id: Optional[str],
        question: str,
        answer: str,
        sources: List[Dict[str, object]],
        attachments: List[Dict[str, object]],
        knowledge_write: Optional[Dict[str, object]],
        agent_state: Dict[str, object],
        user_attachments: Optional[List[Dict[str, object]]] = None,
        context_usage: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        with self._lock:
            record = self._record_locked(conversation_id)
            now = self._now()
            if session_id:
                record["session_id"] = session_id
                record["fork_from_session_id"] = ""
            messages = record["messages"]
            assert isinstance(messages, list)
            last_message = messages[-1] if messages else None
            user_already_recorded = (
                isinstance(last_message, dict)
                and last_message.get("role") == "user"
                and last_message.get("content") == question
            )
            if not user_already_recorded:
                user_message: Dict[str, object] = {
                    "id": uuid.uuid4().hex,
                    "role": "user",
                    "content": question,
                    "created_at": now,
                }
                if user_attachments:
                    user_message["attachments"] = user_attachments
                messages.append(user_message)
            messages.append(
                {
                    "id": uuid.uuid4().hex,
                    "role": "assistant",
                    "content": answer,
                    "created_at": now,
                    "sources": sources,
                    "attachments": attachments,
                    "knowledge_write": knowledge_write,
                    "agent": agent_state,
                }
            )
            if record["title"] == "新建会话":
                record["title"] = self._title_for(question)
            record["updated_at"] = now
            if agent_state.get("compacted"):
                record["compaction_count"] = int(record["compaction_count"]) + 1
                record["last_compacted_at"] = now
            if context_usage is not None:
                normalized_usage = self._context_usage_from_raw(context_usage)
                normalized_usage["updated_at"] = now
                record["context_usage"] = normalized_usage
            self._persist_locked()
            return self._public(conversation_id, record, include_messages=True)

    def update(self, conversation_id: str, **changes: object) -> Dict[str, object]:
        with self._lock:
            if conversation_id not in self._items:
                raise KeyError("会话不存在")
            record = self._record_locked(conversation_id)
            if "title" in changes:
                record["title"] = self._title_for(str(changes["title"]))[:80]
            if "project_id" in changes:
                project_id = str(changes["project_id"])
                if project_id != record["project_id"]:
                    record["project_id"] = project_id
                    self._reset_sdk_context_locked(conversation_id, record)
            if "agent_id" in changes:
                agent_id = str(changes["agent_id"])
                if agent_id != record["agent_id"]:
                    record["agent_id"] = agent_id
                    self._reset_sdk_context_locked(conversation_id, record)
            if "pinned" in changes:
                record["pinned"] = bool(changes["pinned"])
            record["updated_at"] = self._now()
            self._persist_locked()
            return self._public(conversation_id, record, include_messages=True)

    def delete(self, conversation_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            record = self._items.pop(conversation_id, None)
            if record is None:
                return None
            normalized = self._record_from_raw(conversation_id, record)
            self._persist_locked()
            return self._public(conversation_id, normalized, include_messages=True)

    def sdk_scope_for(self, conversation_id: str) -> Optional[str]:
        with self._lock:
            if conversation_id not in self._items:
                return None
            scope = self._record_locked(conversation_id).get("sdk_scope_id")
            return scope if isinstance(scope, str) and scope else None

    def sdk_scope_in_use(self, sdk_scope_id: str) -> bool:
        with self._lock:
            return any(
                self._record_locked(conversation_id).get("sdk_scope_id") == sdk_scope_id
                for conversation_id in self._items
            )

    def branch(self, source_id: str, project_id: str, agent_id: str = DEFAULT_AGENT_ID) -> Dict[str, object]:
        with self._lock:
            if source_id not in self._items:
                raise KeyError("会话不存在")
            source = self._record_locked(source_id)
            if project_id != source["project_id"]:
                raise ValueError("会话分支必须保留在原项目；如需整理到其他项目，请移动原会话，系统会重置其 SDK 上下文。")
            branch_id = str(uuid.uuid4())
            now = self._now()
            clone = self._record_from_raw(branch_id, {
                "title": f"{source['title']} · 分支",
                "created_at": now,
                "updated_at": now,
                "messages": json.loads(json.dumps(source["messages"], ensure_ascii=False)),
                "project_id": project_id,
                "agent_id": agent_id,
                "parent_id": source_id,
                "fork_from_session_id": source["session_id"],
                "sdk_scope_id": source.get("sdk_scope_id") or source_id,
                "pinned": False,
                "compaction_count": source["compaction_count"],
                "context_usage": source["context_usage"],
            })
            clone["session_id"] = ""
            self._items[branch_id] = clone
            self._persist_locked()
            return self._public(branch_id, clone, include_messages=True)

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _title_for(question: str) -> str:
        normalized = re.sub(r"\s+", " ", question).strip()
        return (normalized[:36] + "…") if len(normalized) > 36 else (normalized or "新建会话")

    @staticmethod
    def _timestamp(value: object, fallback: int) -> int:
        if isinstance(value, int) and value > 0:
            return value * 1000 if value < 10_000_000_000 else value
        return fallback

    def _record_locked(self, conversation_id: str) -> Dict[str, object]:
        now = self._now()
        raw = self._items.get(conversation_id)
        raw = raw if isinstance(raw, dict) else {}
        record = self._record_from_raw(conversation_id, raw, now)
        self._items[conversation_id] = record
        return record

    def _record_from_raw(self, conversation_id: str, raw: Dict[str, object], now: Optional[int] = None) -> Dict[str, object]:
        now = now or self._now()
        messages = raw.get("messages")
        valid_messages: List[Dict[str, object]] = []
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"} or not isinstance(item.get("content"), str):
                    continue
                normalized_message = dict(item)
                message_id = normalized_message.get("id")
                normalized_message["id"] = message_id if isinstance(message_id, str) and message_id else uuid.uuid4().hex
                valid_messages.append(normalized_message)
        title = raw.get("title")
        record: Dict[str, object] = {
            "session_id": raw.get("session_id") if isinstance(raw.get("session_id"), str) else "",
            "title": title.strip()[:80] if isinstance(title, str) and title.strip() else "新建会话",
            "created_at": self._timestamp(raw.get("created_at", raw.get("updated_at")), now),
            "updated_at": self._timestamp(raw.get("updated_at"), now),
            "messages": valid_messages,
            "compaction_count": raw.get("compaction_count") if isinstance(raw.get("compaction_count"), int) and raw.get("compaction_count", 0) >= 0 else 0,
            "last_compacted_at": self._timestamp(raw.get("last_compacted_at"), 0) or None,
            "context_usage": self._context_usage_from_raw(raw.get("context_usage")),
            "project_id": raw.get("project_id") if isinstance(raw.get("project_id"), str) and raw.get("project_id") else DEFAULT_PROJECT_ID,
            "agent_id": raw.get("agent_id") if isinstance(raw.get("agent_id"), str) and raw.get("agent_id") else DEFAULT_AGENT_ID,
            "parent_id": raw.get("parent_id") if isinstance(raw.get("parent_id"), str) and raw.get("parent_id") else None,
            "fork_from_session_id": raw.get("fork_from_session_id") if isinstance(raw.get("fork_from_session_id"), str) else "",
            "sdk_scope_id": raw.get("sdk_scope_id") if isinstance(raw.get("sdk_scope_id"), str) and raw.get("sdk_scope_id") else conversation_id,
            "pinned": bool(raw.get("pinned")),
        }
        return record

    @staticmethod
    def _context_usage_from_raw(value: object) -> Dict[str, object]:
        raw = value if isinstance(value, dict) else {}

        def token_count(key: str) -> int:
            item = raw.get(key)
            return item if isinstance(item, int) and item >= 0 else 0

        return {
            "used_tokens": token_count("used_tokens"),
            "threshold_tokens": token_count("threshold_tokens"),
            "max_tokens": token_count("max_tokens"),
            "updated_at": None,
        }

    @staticmethod
    def _reset_sdk_context_locked(conversation_id: str, record: Dict[str, object]) -> None:
        """保留页面历史，但隔离下一次 SDK 会话，不复用原项目/智能体上下文。"""
        record["session_id"] = ""
        record["fork_from_session_id"] = ""
        record["sdk_scope_id"] = str(uuid.uuid4())
        record["context_usage"] = ConversationStore._context_usage_from_raw(None)

    def _public(self, conversation_id: str, record: Dict[str, object], include_messages: bool = False) -> Dict[str, object]:
        messages = record["messages"]
        assert isinstance(messages, list)
        item: Dict[str, object] = {
            "id": conversation_id,
            "title": record["title"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "message_count": len(messages),
            "has_context": bool(record["session_id"]),
            "compaction_count": record["compaction_count"],
            "last_compacted_at": record["last_compacted_at"],
            "context_usage": record["context_usage"],
            "project_id": record["project_id"],
            "agent_id": record["agent_id"],
            "parent_id": record["parent_id"],
            "pinned": record["pinned"],
        }
        if include_messages:
            item["messages"] = json.loads(json.dumps(messages, ensure_ascii=False))
        return item

    def _persist_locked(self) -> None:
        # 始终写入本地 JSON 作为备份
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": 2, "conversations": self._items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        # 同步到 MySQL
        if self._mysql_enabled:
            self._sync_to_mysql_locked()

    def _load_from_mysql_locked(self) -> None:
        """从 MySQL 加载所有对话和消息。"""
        conn = self._mysql
        if conn is None:
            raise RuntimeError("MySQL 未连接")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, project_id, title, pinned, agent_id, session_id, "
                "sdk_scope_id, branch_parent_id, archived, created_at, updated_at "
                "FROM conversations ORDER BY updated_at DESC"
            )
            rows = cur.fetchall()
            self._items = {}
            for row in rows:
                conv_id = row[0]
                self._items[conv_id] = {
                    "id": conv_id, "project_id": row[1], "title": row[2] or "",
                    "pinned": bool(row[3]), "agent_id": row[4] or DEFAULT_AGENT_ID,
                    "session_id": row[5] or "", "sdk_scope_id": row[6] or "",
                    "branch_parent_id": row[7] or "", "archived": bool(row[8]),
                    "created_at": row[9], "updated_at": row[10], "messages": [],
                }
            cur.execute(
                "SELECT id, conversation_id, role, content, feedback_rating, feedback_note, created_at "
                "FROM messages ORDER BY created_at ASC"
            )
            for mrow in cur.fetchall():
                conv_id = mrow[1]
                if conv_id in self._items:
                    msg = {"id": mrow[0], "role": mrow[2], "content": mrow[3] or "", "created_at": mrow[6]}
                    if mrow[4]:
                        msg["feedback"] = {"rating": mrow[4], "note": mrow[5] or ""}
                    self._items[conv_id]["messages"].append(msg)

    def _sync_to_mysql_locked(self) -> None:
        """将内存中的对话全量同步到 MySQL。"""
        conn = self._mysql
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM messages")
                cur.execute("DELETE FROM conversations")
                for conv_id, record in self._items.items():
                    cur.execute(
                        "INSERT INTO conversations (id, project_id, title, pinned, agent_id, "
                        "session_id, sdk_scope_id, branch_parent_id, archived, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            conv_id, str(record.get("project_id", DEFAULT_PROJECT_ID)),
                            str(record.get("title", "")), int(record.get("pinned", False)),
                            str(record.get("agent_id", DEFAULT_AGENT_ID)),
                            str(record.get("session_id", "")),
                            str(record.get("sdk_scope_id", "")),
                            str(record.get("branch_parent_id", "")),
                            int(record.get("archived", False)),
                            int(record.get("created_at", 0)),
                            int(record.get("updated_at", 0)),
                        ),
                    )
                    for msg in record.get("messages", []):
                        fb = msg.get("feedback", {}) if isinstance(msg.get("feedback"), dict) else {}
                        cur.execute(
                            "INSERT INTO messages (id, conversation_id, role, content, "
                            "feedback_rating, feedback_note, created_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (
                                str(msg.get("id", "")), conv_id,
                                str(msg.get("role", "")), str(msg.get("content", "")),
                                fb.get("rating") or None, fb.get("note") or "",
                                int(msg.get("created_at", 0)),
                            ),
                        )
        except Exception as exc:
            logger.warning("MySQL 同步失败: %s", exc, exc_info=True)


class WriteGrantStore:
    """一次性写入许可，防止 SDK 进程外的本地调用绕过当前消息意图。"""

    def __init__(self):
        self._grants: Dict[str, float] = {}
        self._lock = threading.RLock()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._grants[token] = time.monotonic() + 180.0
        return token

    def consume(self, token: str) -> bool:
        with self._lock:
            self._prune_locked()
            if token not in self._grants:
                return False
            del self._grants[token]
            return True

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for token, expiry in list(self._grants.items()):
            if expiry <= now:
                del self._grants[token]


class SdkAgentRunner:
    """调用官方 TypeScript Agent SDK；Python 不参与任何模型或工具循环。"""

    def __init__(self, app_settings: Settings):
        self.settings = app_settings

    def is_ready(self) -> bool:
        return bool(shutil.which("node") and self.settings.agent_runner_path.is_file())

    def run(
        self,
        question: str,
        conversation_id: str,
        session_id: Optional[str],
        write_grant: Optional[str],
        project_id: str = DEFAULT_PROJECT_ID,
        agent_profile: Optional[Dict[str, object]] = None,
        runtime_settings: Optional[Dict[str, object]] = None,
        session_scope_id: Optional[str] = None,
        fork_session: bool = False,
        context_usage: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        request, environment = self._invocation(
            question, conversation_id, session_id, write_grant, project_id,
            agent_profile, runtime_settings, session_scope_id, fork_session, context_usage,
        )
        try:
            completed = subprocess.run(
                [shutil.which("node") or "node", str(self.settings.agent_runner_path)],
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                capture_output=True,
                cwd=str(self.settings.agent_sdk_dir),
                env=environment,
                timeout=self.settings.agent_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentRunError("Agent SDK 调用超时，请稍后重试。") from exc
        except OSError as exc:
            raise AgentRunError("无法启动 Agent SDK 运行时。") from exc

        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise AgentRunError("Agent SDK 未返回有效结果。") from exc
        if not isinstance(payload, dict):
            raise AgentRunError("Agent SDK 返回格式错误。")
        if completed.returncode != 0 or not payload.get("ok"):
            detail = payload.get("error")
            if not isinstance(detail, str) or not detail:
                detail = "Agent SDK 执行失败，请检查 DeepSeek 配置和网络。"
            raise AgentRunError(detail[:500])
        return payload

    def stream(
        self,
        question: str,
        conversation_id: str,
        session_id: Optional[str],
        write_grant: Optional[str],
        project_id: str = DEFAULT_PROJECT_ID,
        agent_profile: Optional[Dict[str, object]] = None,
        runtime_settings: Optional[Dict[str, object]] = None,
        session_scope_id: Optional[str] = None,
        fork_session: bool = False,
        context_usage: Optional[Dict[str, object]] = None,
    ):
        """将官方 SDK 的受控增量事件逐行转发，模型循环仍完全在 Node SDK 内。"""
        request, environment = self._invocation(
            question, conversation_id, session_id, write_grant, project_id,
            agent_profile, runtime_settings, session_scope_id, fork_session, context_usage,
        )
        request["stream"] = True
        try:
            process = subprocess.Popen(
                [shutil.which("node") or "node", str(self.settings.agent_runner_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.settings.agent_sdk_dir),
                env=environment,
            )
        except OSError as exc:
            raise AgentRunError("无法启动 Agent SDK 运行时。") from exc
        if not process.stdin or not process.stdout:
            process.kill()
            raise AgentRunError("无法读取 Agent SDK 流式输出。")
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False))
            process.stdin.close()
            deadline = time.monotonic() + self.settings.agent_timeout_seconds
            saw_result = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise AgentRunError("Agent SDK 调用超时，请稍后重试。")
                ready, _, _ = select.select([process.stdout], [], [], min(0.5, remaining))
                if ready:
                    line = process.stdout.readline()
                    if line:
                        try:
                            event = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(event, dict):
                            saw_result = saw_result or event.get("event") == "result"
                            yield event
                        continue
                if process.poll() is not None:
                    while True:
                        line = process.stdout.readline()
                        if not line:
                            break
                        try:
                            event = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(event, dict):
                            saw_result = saw_result or event.get("event") == "result"
                            yield event
                    break
            if process.returncode and process.returncode != 0 and not saw_result:
                stderr = process.stderr.read() if process.stderr else ""
                if stderr.strip():
                    raise AgentRunError("Agent SDK 执行失败，请检查 DeepSeek 配置和网络。")
        finally:
            if process.poll() is None:
                process.kill()
            if process.stderr:
                process.stderr.close()

    def _invocation(
        self,
        question: str,
        conversation_id: str,
        session_id: Optional[str],
        write_grant: Optional[str],
        project_id: str,
        agent_profile: Optional[Dict[str, object]],
        runtime_settings: Optional[Dict[str, object]],
        session_scope_id: Optional[str],
        fork_session: bool,
        context_usage: Optional[Dict[str, object]],
    ) -> tuple[Dict[str, object], Dict[str, str]]:
        api_key = str((runtime_settings or {}).get("deepseek_api_key") or self.settings.deepseek_api_key).strip()
        if not api_key:
            raise AgentRunError("未配置 DEEPSEEK_API_KEY，请在 .env 中填写后重试。")
        if not self.is_ready():
            raise AgentRunError("Agent SDK 尚未构建。请在 agent_sdk/ 中执行 npm install && npm run build。")
        tool_base_url = self._local_tool_base_url()
        session_root = self.settings.agent_sessions_dir / self._session_scope_id(session_scope_id or conversation_id)
        session_cwd = session_root / "workspace"
        claude_config_dir = session_root / "claude_config"
        session_cwd.mkdir(parents=True, exist_ok=True)
        claude_config_dir.mkdir(parents=True, exist_ok=True)
        previous_context_tokens = (context_usage or {}).get("used_tokens")
        if not isinstance(previous_context_tokens, int) or previous_context_tokens < 0:
            previous_context_tokens = 0
        compaction_tokens = (runtime_settings or {}).get("agent_context_compaction_tokens")
        if not isinstance(compaction_tokens, int) or compaction_tokens < 8_000:
            compaction_tokens = self.settings.agent_context_compaction_tokens
        request: Dict[str, object] = {
            "question": question,
            "session_id": session_id,
            "write_grant": write_grant,
            "project_id": project_id,
            "agent_profile": agent_profile or {},
            "fork_session": fork_session,
            "tool_base_url": tool_base_url,
            "session_cwd": str(session_cwd),
            "claude_config_dir": str(claude_config_dir),
            "model": str((runtime_settings or {}).get("deepseek_model") or self.settings.deepseek_model),
            "max_turns": int((runtime_settings or {}).get("agent_max_turns") or self.settings.agent_max_turns),
            "context_compaction_tokens": compaction_tokens,
            "previous_context_tokens": previous_context_tokens,
        }
        environment = {
            **os.environ,
            "DEEPSEEK_API_KEY": api_key,
            "DEEPSEEK_ANTHROPIC_BASE_URL": str((runtime_settings or {}).get("deepseek_base_url") or self.settings.deepseek_anthropic_base_url),
            "AGENT_TOOL_TOKEN": self.settings.agent_tool_token,
        }
        return request, environment

    def _local_tool_base_url(self) -> str:
        parsed = urlparse(self.settings.agent_tool_base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AgentRunError("AGENT_TOOL_BASE_URL 只能是本机 HTTP 地址（localhost 或 127.0.0.1）。")
        return self.settings.agent_tool_base_url.rstrip("/")

    @staticmethod
    def _session_scope_id(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise AgentRunError("SDK 会话目录标识无效。") from exc


class KnowledgeAgent:
    """应用编排边界：知识服务 + SDK 会话映射，不实现模型工具循环。"""

    def __init__(
        self,
        app_settings: Settings,
        knowledge_base: KnowledgeBase,
        asset_store: AssetStore,
        knowledge_writer: KnowledgeWriter,
        runner: Optional[SdkAgentRunner] = None,
        profiles: Optional[AgentProfileStore] = None,
        runtime_settings: Optional[RuntimeSettingsStore] = None,
    ):
        self.settings = app_settings
        self.knowledge_base = knowledge_base
        self.asset_store = asset_store
        self.knowledge_writer = knowledge_writer
        self.runner = runner or SdkAgentRunner(app_settings)
        mysql_enabled = app_settings.mysql_enabled
        mysql_config = {}
        if mysql_enabled:
            mysql_config = {
                "host": app_settings.mysql_host,
                "port": app_settings.mysql_port,
                "user": app_settings.mysql_user,
                "password": app_settings.mysql_password,
                "database": app_settings.mysql_database,
            }
        self.conversations = ConversationStore(
            app_settings.agent_conversations_path,
            mysql_enabled=mysql_enabled,
            mysql_config=mysql_config,
        )
        self.profiles = profiles or AgentProfileStore(app_settings)
        self.runtime_settings = runtime_settings or RuntimeSettingsStore(app_settings)
        self.write_grants = WriteGrantStore()

    def initialize(self) -> None:
        self.settings.agent_sessions_dir.mkdir(parents=True, exist_ok=True)
        self.profiles.load()
        self.runtime_settings.load()
        self.conversations.load()
        self.conversations.migrate_to_single_agent()
        self.conversations.remove_archival_state()

    def create_conversation(self, project_id: str = DEFAULT_PROJECT_ID) -> Dict[str, object]:
        return self.conversations.create(str(uuid.uuid4()), project_id, DEFAULT_AGENT_ID)

    def list_conversations(self, project_id: Optional[str] = None, query: str = "") -> List[Dict[str, object]]:
        return self.conversations.list(project_id, query)

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, object]]:
        return self.conversations.get(self._conversation_id(conversation_id))

    def evaluate(self, question: str, project_id: str) -> Dict[str, object]:
        """执行一轮无会话记忆、无写入授权的评测，不写入聊天历史。"""
        normalized_question = question.strip()
        if not normalized_question:
            raise AgentRunError("评测问题不能为空。")
        agent_profile = self.profiles.get(DEFAULT_AGENT_ID)
        if not agent_profile:
            raise AgentRunError("知识库助手配置不存在。")
        evaluation_id = str(uuid.uuid4())
        started_at = time.monotonic()
        try:
            payload = self.runner.run(
                question=normalized_question,
                conversation_id=evaluation_id,
                session_id=None,
                write_grant=None,
                project_id=project_id,
                agent_profile=agent_profile,
                runtime_settings=self.runtime_settings.agent_values(),
                session_scope_id=evaluation_id,
                fork_session=False,
                context_usage=None,
            )
        finally:
            self._remove_session_scope(evaluation_id)
        if not isinstance(payload, dict) or not payload.get("ok"):
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise AgentRunError(str(detail)[:500] if isinstance(detail, str) and detail else "Agent SDK 评测执行失败，请检查 DeepSeek 配置和网络。")
        agent_state = {
            "result_subtype": payload.get("result_subtype"),
            "num_turns": payload.get("num_turns"),
            "compacted": bool(payload.get("compacted")),
            "retrieval_repaired": bool(payload.get("retrieval_repaired")),
            "trace": payload.get("trace") if isinstance(payload.get("trace"), list) else [],
            "retrieval": payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {},
        }
        return {
            "answer": str(payload.get("answer") or "模型未返回可显示的回答。"),
            "sources": payload.get("sources") if isinstance(payload.get("sources"), list) else [],
            "attachments": payload.get("attachments") if isinstance(payload.get("attachments"), list) else [],
            "agent": agent_state,
            "context_usage": payload.get("context_usage") if isinstance(payload.get("context_usage"), dict) else {},
            "latency_ms": max(0, round((time.monotonic() - started_at) * 1000)),
        }

    def answer(
        self,
        question: str,
        conversation_id: Optional[str] = None,
        attachment_asset_ids: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        conversation_id = self._conversation_id(conversation_id)
        self.conversations.create(conversation_id)
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            raise AgentRunError("会话不存在。")
        agent_profile = self.profiles.get(DEFAULT_AGENT_ID)
        if not agent_profile:
            raise AgentRunError("知识库助手配置不存在。")
        uploaded_attachments = self._resolve_uploaded_attachments(
            attachment_asset_ids or [], str(conversation["project_id"])
        )
        self.conversations.record_user_message(
            conversation_id,
            question,
            user_attachments=uploaded_attachments,
        )
        sdk_question = self._question_with_uploaded_attachments(question, uploaded_attachments)
        write_grant = self._write_grant_for(question)
        payload = self.runner.run(
            question=sdk_question,
            conversation_id=conversation_id,
            session_id=self.conversations.session_id_for(conversation_id) or str(conversation.get("fork_from_session_id") or "") or None,
            write_grant=write_grant,
            project_id=str(conversation["project_id"]),
            agent_profile=agent_profile,
            runtime_settings=self.runtime_settings.agent_values(),
            session_scope_id=str(conversation.get("sdk_scope_id") or conversation_id),
            fork_session=bool(conversation.get("fork_from_session_id")),
            context_usage=conversation.get("context_usage") if isinstance(conversation.get("context_usage"), dict) else None,
        )
        session_id = payload.get("session_id")
        agent_state = {
            "result_subtype": payload.get("result_subtype"),
            "num_turns": payload.get("num_turns"),
            "compacted": bool(payload.get("compacted")),
            "retrieval_repaired": bool(payload.get("retrieval_repaired")),
            "trace": payload.get("trace") if isinstance(payload.get("trace"), list) else [],
            "retrieval": payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {},
        }
        answer = str(payload.get("answer") or "模型未返回可显示的回答。")
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        knowledge_write = payload.get("knowledge_write") if isinstance(payload.get("knowledge_write"), dict) else None
        context_usage = payload.get("context_usage") if isinstance(payload.get("context_usage"), dict) else None
        self.conversations.record_turn(
            conversation_id=conversation_id,
            session_id=session_id if isinstance(session_id, str) and session_id else None,
            question=question,
            answer=answer,
            sources=sources,
            attachments=attachments,
            knowledge_write=knowledge_write,
            agent_state=agent_state,
            user_attachments=uploaded_attachments,
            context_usage=context_usage,
        )
        return {
            "answer": answer,
            "sources": sources,
            "attachments": attachments,
            "knowledge_write": knowledge_write,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "agent": agent_state,
            "context_usage": context_usage,
        }

    def answer_stream(
        self,
        question: str,
        conversation_id: Optional[str] = None,
        attachment_asset_ids: Optional[List[str]] = None,
    ):
        """将 SDK 的公开执行事件转为页面 SSE；用户消息会先写入本地历史。"""
        conversation_id = self._conversation_id(conversation_id)
        self.conversations.create(conversation_id)
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            raise AgentRunError("会话不存在。")
        agent_profile = self.profiles.get(DEFAULT_AGENT_ID)
        if not agent_profile:
            raise AgentRunError("知识库助手配置不存在。")
        uploaded_attachments = self._resolve_uploaded_attachments(
            attachment_asset_ids or [], str(conversation["project_id"])
        )
        self.conversations.record_user_message(
            conversation_id,
            question,
            user_attachments=uploaded_attachments,
        )
        sdk_question = self._question_with_uploaded_attachments(question, uploaded_attachments)
        write_grant = self._write_grant_for(question)
        yield {"event": "status", "data": {"message": "正在启动 Agent SDK 并处理当前请求…"}}
        payload: Optional[Dict[str, object]] = None
        runner_events = self.runner.stream(
            question=sdk_question,
            conversation_id=conversation_id,
            session_id=self.conversations.session_id_for(conversation_id) or str(conversation.get("fork_from_session_id") or "") or None,
            write_grant=write_grant,
            project_id=str(conversation["project_id"]),
            agent_profile=agent_profile,
            runtime_settings=self.runtime_settings.agent_values(),
            session_scope_id=str(conversation.get("sdk_scope_id") or conversation_id),
            fork_session=bool(conversation.get("fork_from_session_id")),
            context_usage=conversation.get("context_usage") if isinstance(conversation.get("context_usage"), dict) else None,
        )
        try:
            for event in runner_events:
                event_name = event.get("event")
                data = event.get("data")
                if event_name == "result" and isinstance(data, dict):
                    payload = data
                elif isinstance(event_name, str) and event_name in {"trace", "delta", "status"} and isinstance(data, dict):
                    yield {"event": event_name, "data": data}
        finally:
            close = getattr(runner_events, "close", None)
            if callable(close):
                close()
        if not payload:
            raise AgentRunError("Agent SDK 未产生结束结果。")
        if not payload.get("ok"):
            detail = payload.get("error")
            raise AgentRunError(str(detail)[:500] if isinstance(detail, str) and detail else "Agent SDK 执行失败，请检查 DeepSeek 配置和网络。")
        session_id = payload.get("session_id")
        agent_state = {
            "result_subtype": payload.get("result_subtype"),
            "num_turns": payload.get("num_turns"),
            "compacted": bool(payload.get("compacted")),
            "retrieval_repaired": bool(payload.get("retrieval_repaired")),
            "trace": payload.get("trace") if isinstance(payload.get("trace"), list) else [],
            "retrieval": payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {},
        }
        answer = str(payload.get("answer") or "模型未返回可显示的回答。")
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        knowledge_write = payload.get("knowledge_write") if isinstance(payload.get("knowledge_write"), dict) else None
        context_usage = payload.get("context_usage") if isinstance(payload.get("context_usage"), dict) else None
        self.conversations.record_turn(
            conversation_id=conversation_id,
            session_id=session_id if isinstance(session_id, str) and session_id else None,
            question=question,
            answer=answer,
            sources=sources,
            attachments=attachments,
            knowledge_write=knowledge_write,
            agent_state=agent_state,
            user_attachments=uploaded_attachments,
            context_usage=context_usage,
        )
        yield {
            "event": "done",
            "data": {
                "answer": answer,
                "sources": sources,
                "attachments": attachments,
                "knowledge_write": knowledge_write,
                "conversation_id": conversation_id,
                "session_id": session_id,
                "agent": agent_state,
                "context_usage": context_usage,
            },
        }

    def _resolve_uploaded_attachments(
        self, asset_ids: List[str], project_id: str
    ) -> List[Dict[str, object]]:
        """确认本轮附件属于当前项目且已完成本地解析。"""
        resolved: List[Dict[str, object]] = []
        seen = set()
        for asset_id in asset_ids:
            if not isinstance(asset_id, str) or not asset_id or asset_id in seen:
                continue
            seen.add(asset_id)
            asset = self.asset_store.get(asset_id)
            if not asset or asset.project_id != project_id:
                raise AgentRunError("本轮附件不存在，或不属于当前项目。")
            if asset.status != "ready":
                raise AgentRunError(f"附件「{asset.original_name}」仍在本地解析中，请稍后重试。")
            resolved.append(self.asset_store.public(asset))
        return resolved

    @staticmethod
    def _question_with_uploaded_attachments(
        question: str, attachments: List[Dict[str, object]]
    ) -> str:
        if not attachments:
            return question
        names = "、".join(str(item.get("name") or "未命名文件") for item in attachments)
        return (
            f"{question}\n\n"
            f"【本轮刚上传的本地资料】{names}。"
            "请先调用 search_knowledge 检索并优先参考这些资料；"
            "只能依据工具返回的内容作答，若未检索到请明确说明。"
        )

    def search_knowledge(self, question: str, project_id: str = DEFAULT_PROJECT_ID) -> Dict[str, object]:
        """供 SDK MCP 只读工具调用；返回有界、无路径的检索结果。"""
        diagnostics = self.knowledge_base.search_with_diagnostics(
            question,
            allowed_asset_ids=self.asset_store.ready_asset_ids(project_id),
        )
        sources = diagnostics.get("results")
        sources = sources if isinstance(sources, list) else []
        citations = self._citations(sources)
        diagnostic_for_display = {
            key: value for key, value in diagnostics.items() if key != "results"
        }
        diagnostic_for_display["results"] = citations
        return {
            "chunks": [
                {
                    "asset_id": item["asset_id"],
                    "source": item["source"],
                    "page": item.get("page"),
                    "chunk_no": item["chunk_no"],
                    "text": item["text"],
                    "score": item["score"],
                }
                for item in sources
            ],
            "sources": citations,
            "attachments": self._attachments(sources),
            "diagnostic": diagnostic_for_display,
        }

    def write_knowledge_note(self, grant: str, title: str, content: str, project_id: str = DEFAULT_PROJECT_ID) -> Dict[str, object]:
        if not self.write_grants.consume(grant):
            raise PermissionError("当前请求没有有效的知识库写入授权。")
        return self.knowledge_writer.write_note(title, content, project_id)

    def _write_grant_for(self, question: str) -> Optional[str]:
        """当前用户明确要求写入时一律签发一次性本机写入授权，不按内容分类。"""
        return self.write_grants.issue() if is_explicit_write_request(question) else None

    def update_conversation(self, conversation_id: str, **changes: object) -> Dict[str, object]:
        conversation_id = self._conversation_id(conversation_id)
        old_scope_id = self.conversations.sdk_scope_for(conversation_id)
        updated = self.conversations.update(conversation_id, **changes)
        if old_scope_id and not self.conversations.sdk_scope_in_use(old_scope_id):
            self._remove_session_scope(old_scope_id)
        return updated

    def delete_conversation(self, conversation_id: str) -> Optional[Dict[str, object]]:
        conversation_id = self._conversation_id(conversation_id)
        sdk_scope_id = self.conversations.sdk_scope_for(conversation_id)
        deleted = self.conversations.delete(conversation_id)
        if deleted and sdk_scope_id and not self.conversations.sdk_scope_in_use(sdk_scope_id):
            self._remove_session_scope(sdk_scope_id)
        return deleted

    def branch_conversation(self, conversation_id: str, project_id: str) -> Dict[str, object]:
        return self.conversations.branch(self._conversation_id(conversation_id), project_id, DEFAULT_AGENT_ID)

    def has_valid_tool_token(self, token: Optional[str]) -> bool:
        return bool(token) and secrets.compare_digest(token, self.settings.agent_tool_token)

    def _remove_session_scope(self, sdk_scope_id: str) -> None:
        try:
            session_root = self.settings.agent_sessions_dir / str(uuid.UUID(sdk_scope_id))
        except (ValueError, AttributeError, TypeError):
            return
        if session_root.is_dir() and session_root.parent == self.settings.agent_sessions_dir:
            shutil.rmtree(session_root)

    @staticmethod
    def _conversation_id(value: Optional[str]) -> str:
        if not value:
            return str(uuid.uuid4())
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise AgentRunError("conversation_id 必须是 UUID。") from exc

    def _citations(self, sources: List[Dict[str, object]]) -> List[Dict[str, object]]:
        citations = []
        seen = set()
        for source in sources:
            asset_id = str(source["asset_id"])
            page = source.get("page")
            key = (asset_id, page, source["chunk_no"])
            if key in seen:
                continue
            seen.add(key)
            asset = self.asset_store.get(asset_id)
            if not asset:
                continue
            item = {
                "asset_id": asset_id,
                "name": asset.original_name,
                "kind": asset.kind,
                "page": page,
                "chunk_no": source["chunk_no"],
                "score": source["score"],
                "excerpt": self._excerpt(str(source.get("text") or ""), asset.original_name),
                "preview_url": f"/api/assets/{asset_id}/view",
                "download_url": f"/api/assets/{asset_id}/download",
            }
            citations.append(item)
        return citations

    @staticmethod
    def _excerpt(text: str, source_name: str, limit: int = 280) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        generated_prefix = re.compile(
            rf"^文档：{re.escape(source_name)}(?:；第\s*\d+\s*页)?。\s*"
        )
        normalized = generated_prefix.sub("", normalized)
        return normalized if len(normalized) <= limit else f"{normalized[:limit]}…"

    def _attachments(self, sources: List[Dict[str, object]]) -> List[Dict[str, object]]:
        attachments = []
        seen_asset_ids = set()
        for source in sources:
            asset_id = str(source["asset_id"])
            if asset_id in seen_asset_ids:
                continue
            seen_asset_ids.add(asset_id)
            asset = self.asset_store.get(asset_id)
            if asset:
                attachments.append(self.asset_store.public(asset))
        return attachments
