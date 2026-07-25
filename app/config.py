import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"


def harden_env_permissions(env_path: Path) -> None:
    """将本机密钥文件限制为当前用户可读写；不影响缺失或不支持 chmod 的环境。"""
    if not env_path.exists():
        return
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


harden_env_permissions(ENV_PATH)
load_dotenv(ENV_PATH)
AGENT_TOOL_TOKEN = os.getenv("AGENT_TOOL_TOKEN") or secrets.token_urlsafe(32)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return int(value)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name, str(default))
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    knowledge_dir: Path = PROJECT_ROOT / "knowledge"
    data_dir: Path = PROJECT_ROOT / "data"
    index_path: Path = PROJECT_ROOT / "data" / "index.json"
    chunking_path: Path = PROJECT_ROOT / "data" / "chunking.json"
    pipeline_settings_path: Path = PROJECT_ROOT / "data" / "pipeline_settings.json"
    assets_path: Path = PROJECT_ROOT / "data" / "assets.json"
    quality_path: Path = PROJECT_ROOT / "data" / "quality.json"
    previews_dir: Path = PROJECT_ROOT / "data" / "previews"
    agent_sessions_dir: Path = PROJECT_ROOT / "data" / "agent_sessions"
    agent_conversations_path: Path = PROJECT_ROOT / "data" / "agent_conversations.json"
    projects_path: Path = PROJECT_ROOT / "data" / "projects.json"
    agent_profiles_path: Path = PROJECT_ROOT / "data" / "agent_profiles.json"
    workspace_settings_path: Path = PROJECT_ROOT / "data" / "workspace_settings.json"
    agent_sdk_dir: Path = PROJECT_ROOT / "agent_sdk"
    agent_runner_path: Path = PROJECT_ROOT / "agent_sdk" / "dist" / "runner.js"
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_anthropic_base_url: str = os.getenv("DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    agent_tool_base_url: str = os.getenv("AGENT_TOOL_BASE_URL", "http://127.0.0.1:8000")
    agent_tool_token: str = AGENT_TOOL_TOKEN
    agent_max_turns: int = _int_env("AGENT_MAX_TURNS", 6)
    agent_context_compaction_tokens: int = _int_env("AGENT_CONTEXT_COMPACTION_TOKENS", 60000)
    agent_timeout_seconds: int = _int_env("AGENT_TIMEOUT_SECONDS", 90)
    top_k: int = _int_env("TOP_K", 5)
    chunk_size: int = _int_env("CHUNK_SIZE", 900)
    chunk_overlap: int = _int_env("CHUNK_OVERLAP", 120)
    chunk_boundary_mode: str = os.getenv("CHUNK_BOUNDARY_MODE", "natural")
    pdf_chunk_scope: str = os.getenv("PDF_CHUNK_SCOPE", "page")
    image_index_mode: str = os.getenv("IMAGE_INDEX_MODE", "attachment_only")
    minimum_retrieval_score: float = _float_env("MINIMUM_RETRIEVAL_SCORE", 0.0)
    max_upload_bytes: int = _int_env("MAX_UPLOAD_MB", 15) * 1024 * 1024
    s3_endpoint: str = os.getenv("S3_ENDPOINT", "")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "")
    s3_bucket: str = os.getenv("S3_BUCKET", "knowledge")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")

    @property
    def s3_enabled(self) -> bool:
        return bool(self.s3_endpoint and self.s3_access_key and self.s3_secret_key)

    # MySQL 配置
    mysql_host: str = os.getenv("MYSQL_HOST", "")
    mysql_port: int = _int_env("MYSQL_PORT", 3306)
    mysql_user: str = os.getenv("MYSQL_USER", "kh_user")
    mysql_password: str = os.getenv("MYSQL_PASSWORD", "")
    mysql_database: str = os.getenv("MYSQL_DATABASE", "knowledge")

    @property
    def mysql_enabled(self) -> bool:
        return bool(self.mysql_host and self.mysql_user and self.mysql_password)


settings = Settings()
