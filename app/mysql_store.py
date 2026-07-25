"""MySQL 会话存储 —— 替换本地 JSON 文件，对话历史写入云端 MySQL。"""

import json
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pymysql

from app.config import Settings

DEFAULT_AGENT_ID = "local-default"
DEFAULT_PROJECT_ID = "local-default"


class MySQLConversationStore:
    """MySQL 版对话存储，接口对齐 ConversationStore。"""

    def __init__(self, app_settings: Settings):
        self.settings = app_settings
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, object]] = {}
        self._connection: Optional[pymysql.Connection] = None

    @property
    def conn(self) -> pymysql.Connection:
        if self._connection is None or not self._connection.open:
            self._connection = pymysql.connect(
                host=self.settings.mysql_host,
                port=self.settings.mysql_port,
                user=self.settings.mysql_user,
                password=self.settings.mysql_password,
                database=self.settings.mysql_database,
                charset="utf8mb4",
                autocommit=True,
            )
        return self._connection

    # --- 对齐 ConversationStore 的公有接口 ---

    def load(self) -> None:
        """从 MySQL 加载所有对话到内存。"""
        with self._lock:
            self._items = {}
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, project_id, title, pinned, agent_id, session_id, "
                        "sdk_scope_id, branch_parent_id, archived, created_at, updated_at "
                        "FROM conversations ORDER BY updated_at DESC"
                    )
                    rows = cursor.fetchall()
                    for row in rows:
                        conv_id = row[0]
                        record = {
                            "id": conv_id,
                            "project_id": row[1],
                            "title": row[2] or "",
                            "pinned": bool(row[3]),
                            "agent_id": row[4] or DEFAULT_AGENT_ID,
                            "session_id": row[5] or "",
                            "sdk_scope_id": row[6] or "",
                            "branch_parent_id": row[7] or "",
                            "archived": bool(row[8]),
                            "created_at": row[9],
                            "updated_at": row[10],
                            "messages": [],
                        }
                        self._items[conv_id] = record

                    # 加载消息
                    cursor.execute(
                        "SELECT id, conversation_id, role, content, "
                        "feedback_rating, feedback_note, created_at "
                        "FROM messages ORDER BY created_at ASC"
                    )
                    msg_rows = cursor.fetchall()
                    for mrow in msg_rows:
                        conv_id = mrow[1]
                        if conv_id in self._items:
                            msg = {
                                "id": mrow[0],
                                "role": mrow[2],
                                "content": mrow[3] or "",
                                "created_at": mrow[6],
                            }
                            fb_rating = mrow[4]
                            fb_note = mrow[5]
                            if fb_rating:
                                msg["feedback"] = {"rating": fb_rating, "note": fb_note or ""}
                            self._items[conv_id]["messages"].append(msg)

            except Exception as exc:
                raise RuntimeError(f"MySQL 连接失败: {exc}") from exc

    def list_conversations(self, project_id: Optional[str] = None, query: str = "") -> List[Dict[str, object]]:
        with self._lock:
            result = []
            for conv in self._items.values():
                if project_id and conv["project_id"] != project_id:
                    continue
                if query and query.lower() not in str(conv["title"]).lower():
                    continue
                result.append(self._summary(conv))
            return result

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            record = self._items.get(conversation_id)
            if record is None:
                return None
            return self._public(conversation_id, record, include_messages=True)

    def create_conversation(self, project_id: str, title: str = "新对话") -> Dict[str, object]:
        import uuid
        conv_id = uuid.uuid4().hex
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        record = {
            "id": conv_id,
            "project_id": project_id,
            "title": title,
            "pinned": False,
            "agent_id": DEFAULT_AGENT_ID,
            "session_id": "",
            "sdk_scope_id": conv_id,
            "branch_parent_id": "",
            "archived": False,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        with self._lock:
            self._items[conv_id] = record
            self._insert_conversation_locked(record)
        return self._public(conv_id, record, include_messages=True)

    def update_conversation(self, conversation_id: str, **changes: object) -> Dict[str, object]:
        with self._lock:
            record = self._items.get(conversation_id)
            if record is None:
                raise KeyError(conversation_id)
            for key, value in changes.items():
                if key in record:
                    record[key] = value
            record["updated_at"] = int(datetime.now(timezone.utc).timestamp() * 1000)
            self._update_conversation_locked(record)
            return self._public(conversation_id, record, include_messages=True)

    def delete_conversation(self, conversation_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            record = self._items.pop(conversation_id, None)
            if record is None:
                return None
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
            except Exception:
                pass
            return self._summary(record)

    def save_messages(self, conversation_id: str, messages: List[Dict[str, object]]) -> None:
        """替换对话的全部消息（用于 SDK 结果落库）。"""
        with self._lock:
            record = self._items.get(conversation_id)
            if record is None:
                return
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute("DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
                    for msg in messages:
                        feedback = msg.get("feedback", {}) if isinstance(msg.get("feedback"), dict) else {}
                        cursor.execute(
                            "INSERT INTO messages (id, conversation_id, role, content, feedback_rating, feedback_note, created_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (
                                str(msg.get("id", "")),
                                conversation_id,
                                str(msg.get("role", "")),
                                str(msg.get("content", "")),
                                feedback.get("rating") or None,
                                feedback.get("note") or "",
                                int(msg.get("created_at", 0)),
                            ),
                        )
                record["messages"] = [
                    {k: v for k, v in m.items() if k != "feedback"}
                    | ({"feedback": m["feedback"]} if isinstance(m.get("feedback"), dict) else {})
                    for m in messages
                ]
            except Exception:
                pass

    def initialize_agent_session(
        self, conversation_id: str, session_id: str, sdk_scope_id: str, profile_id: Optional[str] = None
    ) -> Dict[str, object]:
        with self._lock:
            record = self._items.get(conversation_id)
            if record is None:
                raise KeyError(conversation_id)
            record["session_id"] = session_id
            record["sdk_scope_id"] = sdk_scope_id
            record["updated_at"] = int(datetime.now(timezone.utc).timestamp() * 1000)
            self._update_conversation_locked(record)
        return self._public(conversation_id, record, include_messages=True)

    def session_id_for(self, conversation_id: str) -> Optional[str]:
        with self._lock:
            record = self._items.get(conversation_id, {})
            return record.get("session_id") or None

    def branch_conversation(self, conversation_id: str, project_id: str) -> Dict[str, object]:
        with self._lock:
            source = self._items.get(conversation_id)
            if source is None:
                raise KeyError(conversation_id)
            new_id = __import__("uuid").uuid4().hex
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            new_record = {
                **{k: v for k, v in source.items()},
                "id": new_id,
                "project_id": project_id,
                "title": f"{source['title']} (分支)",
                "session_id": "",
                "branch_parent_id": conversation_id,
                "created_at": now,
                "updated_at": now,
                "messages": list(source.get("messages", [])),
            }
            self._items[new_id] = new_record
            self._insert_conversation_locked(new_record)
            return self._public(new_id, new_record, include_messages=True)

    def list_sessions(self) -> List[Dict[str, object]]:
        return self.list_conversations()

    @staticmethod
    def _summary(record: Dict[str, object]) -> Dict[str, object]:
        return {
            "id": record["id"],
            "project_id": record["project_id"],
            "title": record["title"],
            "pinned": record.get("pinned", False),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "message_count": len(record.get("messages", [])),
            "has_context": bool(record.get("session_id")),
            "compaction_count": 0,
            "last_compacted_at": None,
            "context_usage": None,
        }

    @staticmethod
    def _public(conv_id: str, record: Dict[str, object], include_messages: bool = False) -> Dict[str, object]:
        result = {
            "id": conv_id,
            "project_id": record["project_id"],
            "title": record["title"],
            "pinned": record.get("pinned", False),
            "agent_id": record.get("agent_id", DEFAULT_AGENT_ID),
            "session_id": record.get("session_id", ""),
            "sdk_scope_id": record.get("sdk_scope_id", ""),
            "branch_parent_id": record.get("branch_parent_id", ""),
            "archived": record.get("archived", False),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }
        if include_messages:
            result["messages"] = record.get("messages", [])
        return result

    # --- 内部 MySQL 操作 ---

    def _insert_conversation_locked(self, record: Dict[str, object]) -> None:
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO conversations (id, project_id, title, pinned, agent_id, "
                    "session_id, sdk_scope_id, branch_parent_id, archived, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record["id"],
                        record["project_id"],
                        record["title"],
                        int(record.get("pinned", False)),
                        record.get("agent_id", DEFAULT_AGENT_ID),
                        record.get("session_id", ""),
                        record.get("sdk_scope_id", ""),
                        record.get("branch_parent_id", ""),
                        int(record.get("archived", False)),
                        record["created_at"],
                        record["updated_at"],
                    ),
                )
        except Exception:
            pass

    def _update_conversation_locked(self, record: Dict[str, object]) -> None:
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE conversations SET title=%s, pinned=%s, session_id=%s, "
                    "sdk_scope_id=%s, project_id=%s, archived=%s, updated_at=%s WHERE id=%s",
                    (
                        record["title"],
                        int(record.get("pinned", False)),
                        record.get("session_id", ""),
                        record.get("sdk_scope_id", ""),
                        record["project_id"],
                        int(record.get("archived", False)),
                        record["updated_at"],
                        record["id"],
                    ),
                )
        except Exception:
            pass
