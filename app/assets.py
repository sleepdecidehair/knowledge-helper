import json
import mimetypes
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.config import Settings


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
    error: str = ""

    @property
    def kind(self) -> str:
        return asset_kind(Path(self.stored_name).suffix)


class AssetStore:
    """本地资产登记簿。模型永远只接触 asset_id 和展示名称，不接触本地路径。"""

    def __init__(self, app_settings: Settings):
        self.settings = app_settings
        self._assets: Dict[str, Asset] = {}
        self._lock = threading.RLock()

    def ensure_directories(self) -> None:
        self.settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.previews_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        self.ensure_directories()
        if not self.settings.assets_path.exists():
            return
        try:
            payload = json.loads(self.settings.assets_path.read_text(encoding="utf-8"))
            self._assets = {
                item["id"]: Asset(**{**item, "project_id": item.get("project_id") or "local-default"})
                for item in payload.get("assets", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        except (OSError, ValueError, TypeError, KeyError):
            self._assets = {}

    def create(self, original_name: str, stored_name: str, project_id: str = "local-default") -> Asset:
        suffix = Path(stored_name).suffix.lower()
        asset = Asset(
            id=uuid.uuid4().hex,
            original_name=original_name,
            stored_name=stored_name,
            media_type=asset_media_type(suffix),
            status="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
            project_id=project_id,
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

    def all_assets(self, project_id: Optional[str] = None) -> List[Asset]:
        with self._lock:
            assets = self._assets.values()
            if project_id:
                assets = (asset for asset in assets if asset.project_id == project_id)
            return sorted(assets, key=lambda item: item.created_at, reverse=True)

    def ready_assets(self, project_id: Optional[str] = None) -> List[Asset]:
        return [asset for asset in self.all_assets(project_id) if asset.status == "ready"]

    def ready_asset_ids(self, project_id: str) -> set[str]:
        return {asset.id for asset in self.ready_assets(project_id)}

    def queued_assets(self, project_id: Optional[str] = None) -> List[Asset]:
        return [asset for asset in self.all_assets(project_id) if asset.status == "queued"]

    def delete(self, asset_id: str) -> Asset:
        """删除单个已登记资产及其预览；仅由用户确认后的管理接口调用。"""
        with self._lock:
            asset = self._assets.get(asset_id)
            if asset is None:
                raise KeyError("文件不存在")
            del self._assets[asset_id]
            self._save_locked()
        try:
            path = self.path_for(asset)
            path.unlink(missing_ok=True)
        except FileNotFoundError:
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
        """把手动放入 knowledge/ 的安全文件纳入资产表，保持旧使用方式可用。"""
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
        return created

    def path_for(self, asset: Asset) -> Path:
        candidate = self.settings.knowledge_dir / asset.stored_name
        if candidate.parent != self.settings.knowledge_dir or candidate.is_symlink() or not candidate.is_file():
            raise FileNotFoundError("资产文件不存在或不安全")
        return candidate

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
            "error": asset.error,
            "download_url": f"/api/assets/{asset.id}/download",
            "preview_url": preview_url,
        }

    def _save_locked(self) -> None:
        self.ensure_directories()
        payload = {"version": 2, "assets": [asdict(asset) for asset in self._assets.values()]}
        temporary_path = self.settings.assets_path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(self.settings.assets_path)
