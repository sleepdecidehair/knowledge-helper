import json

from app.agent import ConversationStore


class RecordingCursor:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return None


class RecordingConnection:
    open = True

    def __init__(self):
        self.calls = []

    def cursor(self):
        return RecordingCursor(self.calls)


class PayloadCursor:
    def __init__(self, conversation_payload, message_payload):
        self.conversation_payload = conversation_payload
        self.message_payload = message_payload
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        if "FROM conversations" in query:
            self.rows = [
                (
                    "conversation-1", "local-default", "标题", 0, "default", "", "conversation-1", "", 0, 1, 2,
                    json.dumps(self.conversation_payload, ensure_ascii=False),
                )
            ]
        elif "FROM messages" in query:
            self.rows = [
                (
                    "message-1", "conversation-1", "assistant", "回答", None, "", 2,
                    json.dumps(self.message_payload, ensure_ascii=False),
                )
            ]

    def fetchall(self):
        return self.rows


class PayloadConnection:
    open = True

    def __init__(self, conversation_payload, message_payload):
        self.conversation_payload = conversation_payload
        self.message_payload = message_payload

    def cursor(self):
        return PayloadCursor(self.conversation_payload, self.message_payload)


class MigrationCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.connection.calls.append((query, params))
        if "SHOW COLUMNS" in query:
            self.row = ("payload_json",)
        elif "SELECT meta_value FROM conversation_store_meta" in query:
            self.row = None
        elif "FROM conversations" in query:
            self.rows = [
                (
                    "database-only", "local-default", "数据库独有", 0, "default", "", "database-only", "", 0, 1, 2,
                    None,
                )
            ]
        elif "FROM messages" in query:
            self.rows = []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class MigrationConnection:
    open = True

    def __init__(self):
        self.calls = []

    def cursor(self):
        return MigrationCursor(self)


def test_merge_records_keeps_database_only_conversations_and_richer_json_messages():
    database_items = {
        "database-only": {
            "id": "database-only",
            "title": "数据库独有",
            "updated_at": 100,
            "messages": [],
        },
        "shared": {
            "id": "shared",
            "title": "旧标题",
            "updated_at": 100,
            "messages": [
                {"id": "message-1", "role": "assistant", "content": "旧回答", "created_at": 10},
            ],
        },
    }
    backup_items = {
        "shared": {
            "id": "shared",
            "title": "JSON 标题",
            "updated_at": 120,
            "compaction_count": 1,
            "messages": [
                {
                    "id": "message-1",
                    "role": "assistant",
                    "content": "旧回答",
                    "created_at": 10,
                    "agent": {"trace": [{"kind": "mcp_call"}]},
                },
                {"id": "message-2", "role": "user", "content": "继续", "created_at": 20},
            ],
        },
    }

    merged = ConversationStore.merge_records(database_items, backup_items)

    assert set(merged) == {"database-only", "shared"}
    assert merged["shared"]["title"] == "JSON 标题"
    assert [message["id"] for message in merged["shared"]["messages"]] == ["message-1", "message-2"]
    assert merged["shared"]["messages"][0]["agent"]["trace"] == [{"kind": "mcp_call"}]


def test_mysql_sync_serializes_complete_conversation_and_message_payloads(tmp_path):
    connection = RecordingConnection()
    store = ConversationStore(tmp_path / "conversations.json", mysql_enabled=True, mysql_config={"host": "db"})
    store._mysql_conn = connection
    store._items = {
        "conversation-1": {
            "id": "conversation-1",
            "project_id": "local-default",
            "title": "测试",
            "pinned": False,
            "agent_id": "default",
            "session_id": "",
            "sdk_scope_id": "conversation-1",
            "parent_id": "parent-conversation",
            "created_at": 1,
            "updated_at": 2,
            "messages": [
                {
                    "id": "message-1",
                    "role": "assistant",
                    "content": "回答",
                    "created_at": 2,
                    "agent": {"trace": [{"kind": "mcp_call"}]},
                }
            ],
        }
    }

    store._sync_to_mysql_locked()

    conversation_insert = next(call for call in connection.calls if "INSERT INTO conversations" in call[0])
    message_insert = next(call for call in connection.calls if "INSERT INTO messages" in call[0])
    assert "payload_json" in conversation_insert[0]
    assert conversation_insert[1][7] == "parent-conversation"
    assert json.loads(conversation_insert[1][-1])["messages"][0]["agent"]["trace"] == [{"kind": "mcp_call"}]
    assert "payload_json" in message_insert[0]
    assert json.loads(message_insert[1][-1])["agent"]["trace"] == [{"kind": "mcp_call"}]


def test_mysql_schema_adds_payload_columns_and_migration_metadata(tmp_path):
    connection = RecordingConnection()
    store = ConversationStore(tmp_path / "conversations.json", mysql_enabled=True, mysql_config={"host": "db"})

    store._ensure_mysql_schema_locked(connection)

    statements = [query for query, _ in connection.calls]
    assert any("CREATE TABLE IF NOT EXISTS conversation_store_meta" in query for query in statements)
    assert any("ALTER TABLE conversations ADD COLUMN payload_json" in query for query in statements)
    assert any("ALTER TABLE messages ADD COLUMN payload_json" in query for query in statements)


def test_mysql_load_restores_complete_conversation_and_message_payloads(tmp_path):
    conversation_payload = {
        "id": "conversation-1",
        "project_id": "local-default",
        "title": "标题",
        "updated_at": 2,
        "context_usage": {"used_tokens": 120},
        "messages": [],
    }
    message_payload = {
        "id": "message-1",
        "role": "assistant",
        "content": "回答",
        "created_at": 2,
        "agent": {"trace": [{"kind": "mcp_call"}]},
    }
    store = ConversationStore(tmp_path / "conversations.json", mysql_enabled=True, mysql_config={"host": "db"})
    store._mysql_conn = PayloadConnection(conversation_payload, message_payload)

    store._load_from_mysql_locked()

    loaded = store.get("conversation-1")
    assert loaded["context_usage"]["used_tokens"] == 120
    assert loaded["messages"][0]["agent"]["trace"] == [{"kind": "mcp_call"}]


def test_first_mysql_load_merges_json_backup_and_marks_migration_complete(tmp_path):
    path = tmp_path / "conversations.json"
    path.write_text(
        json.dumps(
            {
                "conversations": {
                    "json-only": {
                        "id": "json-only",
                        "title": "JSON 独有",
                        "created_at": 3,
                        "updated_at": 4,
                        "messages": [
                            {
                                "id": "message-1",
                                "role": "assistant",
                                "content": "保留完整轨迹",
                                "created_at": 4,
                                "agent": {"trace": [{"kind": "mcp_call"}]},
                            }
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    connection = MigrationConnection()
    store = ConversationStore(path, mysql_enabled=True, mysql_config={"host": "db"})
    store._mysql_conn = connection

    store.load()

    assert set(store._items) == {"database-only", "json-only"}
    assert store._items["json-only"]["messages"][0]["agent"]["trace"] == [{"kind": "mcp_call"}]
    assert any("INSERT INTO conversation_store_meta" in query for query, _ in connection.calls)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert set(persisted["conversations"]) == {"database-only", "json-only"}
