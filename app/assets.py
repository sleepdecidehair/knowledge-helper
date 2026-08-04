"""本地资产登记簿。S3 主存储 + MySQL 同步，本地 JSON 仅作备份。"""

import json
import logging
import mimetypes
import re
import shutil
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.config import Settings

logger = logging.getLogger(__name__)

# MySQL 连接缓存（模块级复用）
_mysql_conn = None

def _get_mysql(settings: Settings):
    global _mysql_conn
    if not settings.mysql_enabled:
        return None
    if _mysql_conn is None or not _mysql_conn.open:
        try:
            import pymysql
            _mysql_conn = pymysql.connect(
                host=settings.mysql_host, port=settings.mysql_port,
                user=settings.mysql_user, password=settings.mysql_password,
                database=settings.mysql_database,
                charset="utf8mb4", autocommit=True, connect_timeout=5,
            )
        except Exception as exc:
            logger.warning("MySQL 连接失败: %s", exc)
            return None
    return _mysql_conn


TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | IMAGE_SUFFIXES | PDF_SUFFIXES


def asset_kind(suffix: str) -> str:
    suffix = suffix.lower()
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "text"


def asset_media_type(suffix: str) -> str:
    if suffix.lower() == ".pdf":
        return "application/pdf"
    return mimetypes.guess_type(f"file{suffix}")[0] or "application/octet-stream"


@dataclass(frozen=True)
class Asset:
    id: str
    original_name: str
    stored_name: str
    media_type: str
    status: str
    created_at: str
    project_id: str = "local-default"
    page_count: int = 0
    chunk_count: int = 0
    size_bytes: int = 0
    error: str = ""
    tags: List[str] = field(default_factory=list)
    description: str = ""
    content_hash: str = ""
    version_group_id: str = ""
    version_no: int = 1
    is_current_version: bool = True
    replaces_asset_id: str = ""
    vision_status: str = "unavailable"
    visual_segments: List[Dict[str, object]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return asset_kind(Path(self.stored_name).suffix)


class AssetStore:
    """本地资产登记簿。模型永远只接触 asset_id 和展示名称，不接触本地路径。"""

    def __init__(self, app_settings: Settings, s3_storage=None):
        self.settings = app_settings
        self._assets: Dict[str, Asset] = {}
        self._lock = threading.RLock()
        self._s3 = s3_storage
        self._mysql = None  # lazy init via _get_mysql

    def ensure_directories(self) -> None:
        self.settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.previews_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        self.ensure_directories()
        database_assets: Dict[str, Asset] = {}
        if self.settings.mysql_enabled:
            self._load_from_mysql_locked()
            database_assets = dict(self._assets)

        backup_assets = self._load_json_assets_locked()
        if database_assets:
            known_stored_names = {asset.stored_name for asset in database_assets.values()}
            recovered_assets = {
                asset_id: asset
                for asset_id, asset in backup_assets.items()
                if asset_id not in database_assets and asset.stored_name not in known_stored_names
            }
            self._assets.update(recovered_assets)
            if recovered_assets:
                self._save_locked()
            return

        self._assets = backup_assets

    def _load_json_assets_locked(self) -> Dict[str, Asset]:
        if not self.settings.assets_path.exists():
            return {}
        try:
            payload = json.loads(self.settings.assets_path.read_text(encoding="utf-8"))
            return {
                asset.id: asset
                for item in payload.get("assets", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                for asset in [self._asset_from_payload(item)]
            }
        except (OSError, ValueError, TypeError, KeyError):
            return {}

    def create(
        self,
        original_name: str,
        stored_name: str,
        project_id: str = "local-default",
        content_hash: str = "",
        version_group_id: Optional[str] = None,
        version_no: int = 1,
        replaces_asset_id: str = "",
        size_bytes: int = 0,
    ) -> Asset:
        suffix = Path(stored_name).suffix.lower()
        asset_id = uuid.uuid4().hex
        asset = Asset(
            id=asset_id,
            original_name=self._clean_name(original_name),
            stored_name=stored_name,
            media_type=asset_media_type(suffix),
            status="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
            project_id=project_id,
            size_bytes=max(0, int(size_bytes)),
            content_hash=str(content_hash or ""),
            version_group_id=version_group_id or asset_id,
            version_no=max(1, int(version_no)),
            replaces_asset_id=str(replaces_asset_id or ""),
        )
        with self._lock:
            self._assets[asset.id] = asset
            self._save_locked()
        return asset

    def get(self, asset_id: str) -> Optional[Asset]:
        with self._lock:
            return self._assets.get(asset_id)

    def update(self, asset_id: str, **changes: object) -> Asset:
        with self._lock:
            asset = self._assets[asset_id]
            updated = replace(asset, **changes)
            self._assets[asset_id] = updated
            self._save_locked()
            return updated

    def update_metadata(
        self,
        asset_id: str,
        *,
        original_name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        description: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Asset:
        with self._lock:
            asset = self._assets[asset_id]
            changes: Dict[str, object] = {}
            if original_name is not None:
                changes["original_name"] = self._clean_name(original_name)
            if tags is not None:
                changes["tags"] = self._clean_tags(tags)
            if description is not None:
                changes["description"] = self._clean_description(description)
            if project_id is not None:
                target_project_id = str(project_id)
                group_id = asset.version_group_id or asset.id
                has_other_version = any(
                    candidate.id != asset.id
                    and (candidate.version_group_id or candidate.id) == group_id
                    for candidate in self._assets.values()
                )
                if target_project_id != asset.project_id and has_other_version:
                    raise ValueError("版本资料必须保持在同一项目；请先删除其他版本后再移动。")
                changes["project_id"] = target_project_id
            if not changes:
                return asset
            return self.update(asset_id, **changes)

    def replace(
        self,
        asset_id: str,
        original_name: str,
        stored_name: str,
        content_hash: str,
        size_bytes: int = 0,
    ) -> Asset:
        """登记已写入本地目录的新版本，并让旧版本立刻退出检索范围。"""
        with self._lock:
            current = self._assets[asset_id]
            if not current.is_current_version:
                raise ValueError("只能替换当前版本的资料")
            asset_id_new = uuid.uuid4().hex
            replacement = Asset(
                id=asset_id_new,
                original_name=self._clean_name(original_name),
                stored_name=stored_name,
                media_type=asset_media_type(Path(stored_name).suffix.lower()),
                status="queued",
                created_at=datetime.now(timezone.utc).isoformat(),
                project_id=current.project_id,
                size_bytes=max(0, int(size_bytes)),
                tags=current.tags,
                description=current.description,
                content_hash=str(content_hash or ""),
                version_group_id=current.version_group_id or current.id,
                version_no=max(
                    [
                        candidate.version_no
                        for candidate in self._assets.values()
                        if candidate.version_group_id == (current.version_group_id or current.id)
                    ]
                    or [current.version_no]
                )
                + 1,
                replaces_asset_id=current.id,
            )
            self._assets[current.id] = replace(current, is_current_version=False)
            self._assets[replacement.id] = replacement
            self._save_locked()
            return replacement

    def restore_version(self, asset_id: str) -> Asset:
        with self._lock:
            target = self._assets[asset_id]
            group_id = target.version_group_id or target.id
            for candidate_id, candidate in list(self._assets.items()):
                if candidate.version_group_id == group_id or (not candidate.version_group_id and candidate.id == group_id):
                    self._assets[candidate_id] = replace(candidate, is_current_version=candidate_id == asset_id)
            self._save_locked()
            return self._assets[asset_id]

    def requeue(self, asset_id: str) -> Asset:
        with self._lock:
            asset = self._assets[asset_id]
            queued = replace(asset, status="queued", error="", vision_status="queued", visual_segments=[])
            self._assets[asset_id] = queued
            self._save_locked()
            return queued

    def get_required(self, asset_id: str) -> Asset:
        asset = self.get(asset_id)
        if asset is None:
            raise KeyError(asset_id)
        return asset

    def all_assets(self, project_id: Optional[str] = None) -> List[Asset]:
        with self._lock:
            assets = self._assets.values()
            if project_id:
                assets = (asset for asset in assets if asset.project_id == project_id)
            return sorted(assets, key=lambda item: item.created_at, reverse=True)

    def ready_assets(self, project_id: Optional[str] = None) -> List[Asset]:
        return [asset for asset in self.all_assets(project_id) if asset.status == "ready"]

    def ready_current_assets(self, project_id: Optional[str] = None) -> List[Asset]:
        return [asset for asset in self.ready_assets(project_id) if asset.is_current_version]

    def ready_asset_ids(self, project_id: str) -> set[str]:
        return {asset.id for asset in self.ready_current_assets(project_id)}

    def queued_assets(self, project_id: Optional[str] = None) -> List[Asset]:
        return [asset for asset in self.all_assets(project_id) if asset.status == "queued"]

    def delete(self, asset_id: str) -> Asset:
        """删除单个已登记资产及其预览；同时从 S3 和本地删除。"""
        with self._lock:
            asset = self._assets.get(asset_id)
            if asset is None:
                raise KeyError("文件不存在")
            del self._assets[asset_id]
            self._save_locked()
        # 删除本地缓存
        try:
            path = self.settings.knowledge_dir / asset.stored_name
            path.unlink(missing_ok=True)
        except Exception:
            pass
        # 删除 S3
        if self._s3:
            try:
                self._s3.delete(asset.stored_name)
            except Exception:
                pass
        preview_dir = self.settings.previews_dir / asset.id
        if preview_dir.parent == self.settings.previews_dir and preview_dir.is_dir():
            shutil.rmtree(preview_dir)
        return asset

    def requeue_failed_assets(self) -> None:
        """用户手动扫描时允许对可恢复的解析失败资产再次尝试。"""
        with self._lock:
            changed = False
            for asset_id, asset in list(self._assets.items()):
                if asset.status == "failed":
                    self._assets[asset_id] = replace(asset, status="queued", error="")
                    changed = True
            if changed:
                self._save_locked()

    def recover_interrupted_jobs(self) -> None:
        with self._lock:
            changed = False
            for asset_id, asset in list(self._assets.items()):
                if asset.status == "processing":
                    self._assets[asset_id] = replace(asset, status="queued", error="")
                    changed = True
            if changed:
                self._save_locked()

    def claim(self, asset_id: str) -> Optional[Asset]:
        with self._lock:
            asset = self._assets.get(asset_id)
            if asset is None or asset.status != "queued":
                return None
            claimed = replace(asset, status="processing", error="")
            self._assets[asset_id] = claimed
            self._save_locked()
            return claimed

    def register_existing_files(self) -> List[Asset]:
        """登记本地文件和 S3 中缺失元数据的安全对象。"""
        self.ensure_directories()
        with self._lock:
            known_names = {asset.stored_name for asset in self._assets.values()}
        created = []
        for path in self.settings.knowledge_dir.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if path.stat().st_size > self.settings.max_upload_bytes or path.name in known_names:
                continue
            created.append(self.create(path.name, path.name, "local-default"))
            known_names.add(path.name)

        if self._s3:
            for key in self._s3.list_keys():
                stored_name = str(key)
                safe_name = Path(stored_name).name
                if (
                    not safe_name
                    or safe_name != stored_name
                    or safe_name in known_names
                    or Path(safe_name).suffix.lower() not in SUPPORTED_SUFFIXES
                ):
                    continue
                original_name = re.sub(r"^[0-9a-fA-F]{8}_", "", safe_name) or safe_name
                created.append(self.create(original_name, safe_name, "local-default"))
                known_names.add(safe_name)
        return created

    def path_for(self, asset: Asset) -> Path:
        """从 S3 拉取到临时文件供处理使用，用完请调用 cleanup_local() 删除。

        如果 S3 未启用，回退到本地 knowledge_dir。"""
        if self._s3:
            data = self._s3.download(asset.stored_name)
            if data:
                tmp = Path(tempfile.gettempdir()) / "kh-knowledge" / asset.stored_name
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                return tmp
            raise FileNotFoundError(f"S3 中不存在: {asset.stored_name}")

        # 无 S3 时回退到本地
        local = self.settings.knowledge_dir / asset.stored_name
        if local.is_file():
            return local
        raise FileNotFoundError("资产文件不存在且未配置 S3")

    def read_bytes(self, asset: Asset) -> bytes:
        """直接从 S3 读取文件内容，不落盘。"""
        if self._s3:
            data = self._s3.download(asset.stored_name)
            if data:
                return data
        # 回退到本地
        local = self.settings.knowledge_dir / asset.stored_name
        if local.is_file():
            return local.read_bytes()
        raise FileNotFoundError(f"文件不存在: {asset.stored_name}")

    def cleanup_local(self, asset: Asset) -> None:
        """删除 path_for() 创建的临时文件。"""
        if self._s3:
            tmp = Path(tempfile.gettempdir()) / "kh-knowledge" / asset.stored_name
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def public(self, asset: Asset) -> Dict[str, object]:
        preview_url = None
        if asset.kind == "image":
            preview_url = f"/api/assets/{asset.id}/preview"
        elif asset.kind == "pdf" and asset.page_count:
            preview_url = f"/api/assets/{asset.id}/preview?page=1"
        return {
            "asset_id": asset.id,
            "project_id": asset.project_id,
            "name": asset.original_name,
            "kind": asset.kind,
            "media_type": asset.media_type,
            "status": asset.status,
            "page_count": asset.page_count,
            "chunk_count": asset.chunk_count,
            "size_bytes": asset.size_bytes,
            "error": asset.error,
            "tags": asset.tags,
            "description": asset.description,
            "vision_status": asset.vision_status,
            "version": {
                "group_id": asset.version_group_id or asset.id,
                "number": asset.version_no,
                "is_current": asset.is_current_version,
                "replaces_asset_id": asset.replaces_asset_id,
            },
            "download_url": f"/api/assets/{asset.id}/download",
            "preview_url": preview_url,
        }

    def _save_locked(self) -> None:
        self.ensure_directories()
        payload = {"version": 3, "assets": [asdict(asset) for asset in self._assets.values()]}
        temporary_path = self.settings.assets_path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(self.settings.assets_path)
        # 同步到 MySQL
        self._sync_to_mysql_locked()

    @staticmethod
    def _ensure_mysql_schema_locked(conn) -> None:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM knowledge_assets LIKE 'size_bytes'")
            if cur.fetchone() is None:
                cur.execute(
                    "ALTER TABLE knowledge_assets "
                    "ADD COLUMN size_bytes BIGINT NOT NULL DEFAULT 0"
                )

    def _sync_to_mysql_locked(self) -> None:
        conn = _get_mysql(self.settings)
        if conn is None:
            return
        try:
            self._ensure_mysql_schema_locked(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM knowledge_assets")
                for asset in self._assets.values():
                    cur.execute(
                        "INSERT INTO knowledge_assets (id, original_name, stored_name, media_type, status, "
                        "created_at, project_id, page_count, chunk_count, error, tags, description, "
                        "content_hash, version_group_id, version_no, is_current_version, "
                        "replaces_asset_id, vision_status, visual_segments, size_bytes) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            asset.id, asset.original_name, asset.stored_name, asset.media_type,
                            asset.status, asset.created_at, asset.project_id, asset.page_count,
                            asset.chunk_count, asset.error, json.dumps(asset.tags, ensure_ascii=False),
                            asset.description, asset.content_hash, asset.version_group_id or asset.id,
                            asset.version_no, int(asset.is_current_version),
                            asset.replaces_asset_id, asset.vision_status,
                            json.dumps(asset.visual_segments, ensure_ascii=False) if asset.visual_segments else "[]",
                            asset.size_bytes,
                        ),
                    )
        except Exception as exc:
            logger.warning("Asset MySQL 同步失败: %s", exc)

    def _load_from_mysql_locked(self) -> None:
        conn = _get_mysql(self.settings)
        if conn is None:
            return
        try:
            self._ensure_mysql_schema_locked(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, original_name, stored_name, media_type, status, created_at, "
                    "project_id, page_count, chunk_count, error, tags, description, content_hash, "
                    "version_group_id, version_no, is_current_version, replaces_asset_id, "
                    "vision_status, visual_segments, size_bytes FROM knowledge_assets"
                )
                rows = cur.fetchall()
                if not rows:
                    return
                self._assets = {}
                for row in rows:
                    tags = json.loads(row[10]) if isinstance(row[10], str) else (row[10] or [])
                    vs = json.loads(row[18]) if isinstance(row[18], str) else (row[18] or [])
                    asset = Asset(
                        id=row[0], original_name=row[1], stored_name=row[2],
                        media_type=row[3], status=row[4], created_at=row[5],
                        project_id=row[6], page_count=row[7] or 0, chunk_count=row[8] or 0,
                        error=row[9] or "", tags=tags if isinstance(tags, list) else [],
                        description=row[11] or "", content_hash=row[12] or "",
                        version_group_id=row[13] or row[0], version_no=row[14] or 1,
                        is_current_version=bool(row[15]), replaces_asset_id=row[16] or "",
                        vision_status=row[17] or "unavailable",
                        visual_segments=vs if isinstance(vs, list) else [],
                        size_bytes=max(0, int(row[19] or 0)),
                    )
                    self._assets[asset.id] = asset
                logger.info("Asset 从 MySQL 加载: %d 条", len(self._assets))
        except Exception as exc:
            logger.warning("Asset MySQL 加载失败: %s", exc)

    @staticmethod
    def _clean_name(value: object) -> str:
        cleaned = "".join(char for char in str(value) if char >= " " and char != "\x7f").strip()
        if not cleaned:
            raise ValueError("文件名称不能为空")
        return cleaned[:160]

    @staticmethod
    def _clean_description(value: object) -> str:
        cleaned = "".join(char for char in str(value) if char >= " " and char != "\x7f").strip()
        return cleaned[:800]

    @staticmethod
    def _clean_tags(values: object) -> List[str]:
        if not isinstance(values, list):
            raise ValueError("资料标签必须是列表")
        cleaned: List[str] = []
        seen: set[str] = set()
        for value in values:
            tag = "".join(char for char in str(value) if char >= " " and char != "\x7f").strip()[:40]
            if not tag or tag in seen:
                continue
            seen.add(tag)
            cleaned.append(tag)
        if len(cleaned) > 20:
            raise ValueError("资料标签不能超过 20 个")
        return cleaned

    @classmethod
    def _asset_from_payload(cls, payload: Dict[str, object]) -> Asset:
        asset_id = str(payload["id"])
        raw_segments = payload.get("visual_segments")
        visual_segments = raw_segments if isinstance(raw_segments, list) else []
        raw_tags = payload.get("tags", [])
        try:
            tags = cls._clean_tags(raw_tags)
        except ValueError:
            tags = []
        try:
            original_name = cls._clean_name(payload.get("original_name", ""))
        except ValueError:
            original_name = asset_id
        return Asset(
            id=asset_id,
            original_name=original_name,
            stored_name=str(payload.get("stored_name") or ""),
            media_type=str(payload.get("media_type") or "application/octet-stream"),
            status=str(payload.get("status") or "queued"),
            created_at=str(payload.get("created_at") or datetime.now(timezone.utc).isoformat()),
            project_id=str(payload.get("project_id") or "local-default"),
            page_count=max(0, int(payload.get("page_count") or 0)),
            chunk_count=max(0, int(payload.get("chunk_count") or 0)),
            size_bytes=max(0, int(payload.get("size_bytes") or 0)),
            error=str(payload.get("error") or ""),
            tags=tags,
            description=cls._clean_description(payload.get("description") or ""),
            content_hash=str(payload.get("content_hash") or ""),
            version_group_id=str(payload.get("version_group_id") or asset_id),
            version_no=max(1, int(payload.get("version_no") or 1)),
            is_current_version=bool(payload.get("is_current_version", True)),
            replaces_asset_id=str(payload.get("replaces_asset_id") or ""),
            vision_status=str(payload.get("vision_status") or "unavailable"),
            visual_segments=[segment for segment in visual_segments if isinstance(segment, dict)],
        )
