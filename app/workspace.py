"""本地工作台的项目、智能体和非敏感运行设置。"""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from app.config import Settings


DEFAULT_PROJECT_ID = "local-default"
DEFAULT_AGENT_ID = "knowledge-agent"


def _now() -> int:
    return int(time.time() * 1000)


def _copy(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False))


class LocalJsonStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _load_payload(self) -> Dict[str, object]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_payload(self, payload: Dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class ProjectStore(LocalJsonStore):
    """项目是本地隔离边界：会话、文件、检索均隶属于同一项目。"""

    def __init__(self, settings: Settings):
        super().__init__(settings.projects_path)
        self._items: Dict[str, Dict[str, object]] = {}

    def load(self) -> None:
        with self._lock:
            raw = self._load_payload().get("projects", {})
            self._items = raw if isinstance(raw, dict) else {}
            self._ensure_default_locked()
            self._persist_locked()

    def list(self) -> List[Dict[str, object]]:
        with self._lock:
            return sorted((_copy(self._record_locked(project_id)) for project_id in self._items), key=lambda item: (int(item["updated_at"]), str(item["id"])), reverse=True)  # type: ignore[index]

    def get(self, project_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            if project_id not in self._items:
                return None
            return _copy(self._record_locked(project_id))  # type: ignore[return-value]

    def create(self, name: str, icon: str = "✦", color: str = "indigo", instructions: str = "") -> Dict[str, object]:
        with self._lock:
            project_id = str(uuid.uuid4())
            now = _now()
            self._items[project_id] = self._new_record(project_id, name, icon, color, instructions, now)
            self._persist_locked()
            return _copy(self._items[project_id])  # type: ignore[return-value]

    def update(self, project_id: str, **changes: object) -> Dict[str, object]:
        with self._lock:
            if project_id not in self._items:
                raise KeyError("项目不存在")
            current = self._record_locked(project_id)
            name = changes.get("name", current["name"])
            icon = changes.get("icon", current["icon"])
            color = changes.get("color", current["color"])
            instructions = changes.get("instructions", current["instructions"])
            updated = self._new_record(project_id, str(name), str(icon), str(color), str(instructions), int(current["created_at"]))
            self._items[project_id] = updated
            self._persist_locked()
            return _copy(updated)  # type: ignore[return-value]

    def delete(self, project_id: str) -> Dict[str, object]:
        with self._lock:
            if project_id == DEFAULT_PROJECT_ID:
                raise ValueError("默认项目不能删除。请先将会话和文件移到其他项目。")
            record = self._items.pop(project_id, None)
            if record is None:
                raise KeyError("项目不存在")
            self._persist_locked()
            return _copy(self._record_from_raw(project_id, record))  # type: ignore[return-value]

    def exists(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._items

    def _ensure_default_locked(self) -> None:
        if DEFAULT_PROJECT_ID not in self._items:
            now = _now()
            self._items[DEFAULT_PROJECT_ID] = self._new_record(DEFAULT_PROJECT_ID, "本地知识库", "⌂", "indigo", "默认项目。历史会话和未归类文件会保存在这里。", now)

    def _record_locked(self, project_id: str) -> Dict[str, object]:
        raw = self._items.get(project_id)
        if not isinstance(raw, dict):
            raise KeyError("项目不存在")
        normalized = self._record_from_raw(project_id, raw)
        self._items[project_id] = normalized
        return normalized

    @staticmethod
    def _new_record(
        project_id: str,
        name: str,
        icon: str,
        color: str,
        instructions: str,
        created_at: int,
        updated_at: Optional[int] = None,
    ) -> Dict[str, object]:
        clean_name = re.sub(r"\s+", " ", name).strip()[:60]
        if not clean_name:
            raise ValueError("项目名称不能为空")
        clean_icon = icon.strip()[:4] or "✦"
        clean_color = color.strip().lower()[:20] or "indigo"
        clean_instructions = instructions.strip()[:4000]
        return {
            "id": project_id,
            "name": clean_name,
            "icon": clean_icon,
            "color": clean_color,
            "instructions": clean_instructions,
            "memory_mode": "project_only",
            "created_at": created_at,
            "updated_at": updated_at if isinstance(updated_at, int) and updated_at > 0 else _now(),
        }

    def _record_from_raw(self, project_id: str, raw: Dict[str, object]) -> Dict[str, object]:
        created_at = raw.get("created_at") if isinstance(raw.get("created_at"), int) else _now()
        updated_at = raw.get("updated_at") if isinstance(raw.get("updated_at"), int) else created_at
        return self._new_record(
            project_id,
            str(raw.get("name") or "未命名项目"),
            str(raw.get("icon") or "✦"),
            str(raw.get("color") or "indigo"),
            str(raw.get("instructions") or ""),
            created_at,
            updated_at,
        )

    def _persist_locked(self) -> None:
        self._save_payload({"version": 1, "projects": self._items})


class AgentProfileStore(LocalJsonStore):
    """唯一知识库助手的运行配置；SDK 仍是唯一的 Agent 运行时。"""

    def __init__(self, settings: Settings):
        super().__init__(settings.agent_profiles_path)
        self._items: Dict[str, Dict[str, object]] = {}

    def load(self) -> None:
        with self._lock:
            raw = self._load_payload().get("agents", {})
            self._items = raw if isinstance(raw, dict) else {}
            self._ensure_defaults_locked()
            self._persist_locked()

    def list(self) -> List[Dict[str, object]]:
        with self._lock:
            return sorted((_copy(self._record_locked(agent_id)) for agent_id in self._items), key=lambda item: (str(item["id"]) != DEFAULT_AGENT_ID, str(item["name"])))  # type: ignore[index]

    def get(self, agent_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            if agent_id not in self._items:
                return None
            return _copy(self._record_locked(agent_id))  # type: ignore[return-value]

    def create(self, name: str, description: str = "", icon: str = "✦", instructions: str = "", allow_write: bool = False) -> Dict[str, object]:
        with self._lock:
            agent_id = str(uuid.uuid4())
            self._items[agent_id] = self._new_record(agent_id, name, description, icon, instructions, allow_write, _now())
            self._persist_locked()
            return _copy(self._items[agent_id])  # type: ignore[return-value]

    def update(self, agent_id: str, **changes: object) -> Dict[str, object]:
        with self._lock:
            if agent_id not in self._items:
                raise KeyError("智能体不存在")
            current = self._record_locked(agent_id)
            updated = self._new_record(
                agent_id,
                str(changes.get("name", current["name"])),
                str(changes.get("description", current["description"])),
                str(changes.get("icon", current["icon"])),
                str(changes.get("instructions", current["instructions"])),
                bool(changes.get("allow_write", current["allow_write"])),
                int(current["created_at"]),
            )
            self._items[agent_id] = updated
            self._persist_locked()
            return _copy(updated)  # type: ignore[return-value]

    def delete(self, agent_id: str) -> Dict[str, object]:
        with self._lock:
            if agent_id == DEFAULT_AGENT_ID:
                raise ValueError("默认知识库助手不能删除。")
            record = self._items.pop(agent_id, None)
            if record is None:
                raise KeyError("智能体不存在")
            self._persist_locked()
            return _copy(self._record_from_raw(agent_id, record))  # type: ignore[return-value]

    def exists(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._items

    def _ensure_defaults_locked(self) -> None:
        defaults = [
            (DEFAULT_AGENT_ID, "知识库助手", "基于当前项目资料检索并回答，可在明确授权时写入知识笔记。", "KH", "始终以当前项目的检索结果为依据，优先给出可核对的来源。", True),
        ]
        for values in defaults:
            if values[0] not in self._items:
                self._items[values[0]] = self._new_record(*values, created_at=_now())

    def _record_locked(self, agent_id: str) -> Dict[str, object]:
        raw = self._items.get(agent_id)
        if not isinstance(raw, dict):
            raise KeyError("智能体不存在")
        normalized = self._record_from_raw(agent_id, raw)
        self._items[agent_id] = normalized
        return normalized

    @staticmethod
    def _new_record(
        agent_id: str,
        name: str,
        description: str,
        icon: str,
        instructions: str,
        allow_write: bool,
        created_at: int,
        updated_at: Optional[int] = None,
    ) -> Dict[str, object]:
        clean_name = re.sub(r"\s+", " ", name).strip()[:60]
        if not clean_name:
            raise ValueError("智能体名称不能为空")
        return {
            "id": agent_id,
            "name": clean_name,
            "description": description.strip()[:300],
            "icon": icon.strip()[:4] or "✦",
            "instructions": instructions.strip()[:4000],
            "allow_write": bool(allow_write),
            "tools": ["search_knowledge", *( ["save_knowledge_note"] if allow_write else [])],
            "created_at": created_at,
            "updated_at": updated_at if isinstance(updated_at, int) and updated_at > 0 else _now(),
        }

    def _record_from_raw(self, agent_id: str, raw: Dict[str, object]) -> Dict[str, object]:
        created_at = raw.get("created_at") if isinstance(raw.get("created_at"), int) else _now()
        updated_at = raw.get("updated_at") if isinstance(raw.get("updated_at"), int) else created_at
        return self._new_record(
            agent_id,
            str(raw.get("name") or "未命名智能体"),
            str(raw.get("description") or ""),
            str(raw.get("icon") or "✦"),
            str(raw.get("instructions") or ""),
            bool(raw.get("allow_write")),
            created_at,
            updated_at,
        )

    def _persist_locked(self) -> None:
        self._save_payload({"version": 1, "agents": self._items})


class RuntimeSettingsStore(LocalJsonStore):
    """页面运行参数与仅在服务器内存中使用的本地 DeepSeek 密钥。"""

    def __init__(self, settings: Settings):
        super().__init__(settings.workspace_settings_path)
        self.settings = settings
        self._values: Dict[str, object] = {}
        self._api_key = settings.deepseek_api_key.strip()

    def load(self) -> None:
        with self._lock:
            self._api_key = self.settings.deepseek_api_key.strip()
            payload = self._load_payload()
            self._values = self._validate({**self._defaults(), **payload.get("settings", {})})
            self._persist_locked()

    def get(self) -> Dict[str, object]:
        with self._lock:
            value = dict(self._values)
            value["api_key_configured"] = bool(self._api_key)
            return _copy(value)  # type: ignore[return-value]

    def update(self, **changes: object) -> Dict[str, object]:
        with self._lock:
            api_key = changes.pop("deepseek_api_key", None)
            if api_key is not None:
                self._api_key = self._validate_api_key(api_key)
                self._persist_api_key_locked()
            self._values = self._validate({**self._values, **changes})
            self._persist_locked()
            return self.get()

    def agent_values(self) -> Dict[str, object]:
        """仅供服务器调用 SDK；绝不作为 HTTP 响应返回。"""
        with self._lock:
            value = dict(self._values)
            value["deepseek_api_key"] = self._api_key
            return value

    def _defaults(self) -> Dict[str, object]:
        return {
            "deepseek_model": self.settings.deepseek_model,
            "deepseek_base_url": self.settings.deepseek_anthropic_base_url,
            "agent_max_turns": self.settings.agent_max_turns,
            "agent_context_compaction_tokens": self.settings.agent_context_compaction_tokens,
            "storage_mode": "local_only",
        }

    @staticmethod
    def _validate(values: Dict[str, object]) -> Dict[str, object]:
        model = str(values.get("deepseek_model") or "").strip()
        if not model or len(model) > 120:
            raise ValueError("DeepSeek 模型名不能为空且不能超过 120 个字符。")
        base_url = str(values.get("deepseek_base_url") or "").strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in {"api.deepseek.com", "api.deepseek.com.cn"} or not parsed.path.endswith("/anthropic"):
            raise ValueError("DeepSeek 地址只能使用官方 HTTPS Anthropic 兼容端点。")
        turns = values.get("agent_max_turns")
        if not isinstance(turns, int) or not 1 <= turns <= 12:
            raise ValueError("单轮最大步数必须在 1 到 12 之间。")
        compaction_tokens = values.get("agent_context_compaction_tokens")
        if not isinstance(compaction_tokens, int) or not 8_000 <= compaction_tokens <= 120_000:
            raise ValueError("上下文压缩阈值必须在 8,000 到 120,000 tokens 之间。")
        return {
            "deepseek_model": model,
            "deepseek_base_url": base_url,
            "agent_max_turns": turns,
            "agent_context_compaction_tokens": compaction_tokens,
            "storage_mode": "local_only",
        }

    def _persist_locked(self) -> None:
        persisted = {key: value for key, value in self._values.items() if key not in {"api_key_configured", "storage_mode"}}
        self._save_payload({"version": 1, "settings": persisted})

    @staticmethod
    def _validate_api_key(value: object) -> str:
        api_key = str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{16,512}", api_key):
            raise ValueError("DeepSeek API Key 格式无效。")
        return api_key

    def _persist_api_key_locked(self) -> None:
        env_path = self.settings.project_root / ".env"
        try:
            existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            lines = existing.splitlines()
            key_line = f"DEEPSEEK_API_KEY={self._api_key}"
            matcher = re.compile(r"^\s*(?:export\s+)?DEEPSEEK_API_KEY\s*=")
            for index, line in enumerate(lines):
                if matcher.match(line):
                    lines[index] = key_line
                    break
            else:
                lines.append(key_line)
            temporary = env_path.with_name(f".{env_path.name}.tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(env_path)
            os.chmod(env_path, 0o600)
        except OSError as exc:
            raise ValueError("无法写入本机 .env 中的 DeepSeek API Key。") from exc
