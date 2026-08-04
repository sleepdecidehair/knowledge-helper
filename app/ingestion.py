import threading
from dataclasses import replace
from pathlib import Path
from typing import Optional

import fitz
from PIL import Image

from app.assets import Asset, AssetStore
from app.config import Settings
from app.knowledge_base import KnowledgeBase
from app.vision import extract_visual_text


class AssetProcessor:
    """上传后的后台处理器：建立可预览资产，再把可检索内容写入本地索引。"""

    def __init__(self, app_settings: Settings, asset_store: AssetStore, knowledge_base: KnowledgeBase):
        self.settings = app_settings
        self.asset_store = asset_store
        self.knowledge_base = knowledge_base
        self._index_publish_lock = threading.RLock()

    def process(self, asset_id: str) -> None:
        asset = self.asset_store.claim(asset_id)
        if asset is None:
            return
        try:
            path = self.asset_store.path_for(asset)
            try:
                page_count = self._prepare_preview(asset, path)
                visual_segments = []
                vision_status = "unavailable"
                pipeline = self.knowledge_base.pipeline_status()
                if pipeline.get("vision_adapter") != "unconfigured":
                    self.asset_store.update(asset.id, vision_status="processing", visual_segments=[])
                    try:
                        visual_segments = self._extract_visual_segments(asset, path)
                        vision_status = "ready" if visual_segments else "empty"
                    except Exception:
                        vision_status = "failed"
                with self._index_publish_lock:
                    ready_candidate = replace(
                        asset,
                        status="ready",
                        page_count=page_count,
                        error="",
                        vision_status=vision_status,
                        visual_segments=visual_segments,
                    )
                    index_assets = [
                        candidate
                        for candidate in self.asset_store.ready_current_assets()
                        if candidate.id != ready_candidate.id
                    ]
                    if ready_candidate.is_current_version:
                        index_assets.append(ready_candidate)
                    self.knowledge_base.rebuild(index_assets, self.asset_store.path_for)
                    chunk_count = (
                        self.knowledge_base.count_for_asset(ready_candidate.id)
                        if ready_candidate.is_current_version
                        else 0
                    )
                    self.asset_store.update(
                        asset.id,
                        status="ready",
                        page_count=page_count,
                        chunk_count=chunk_count,
                        error="",
                        vision_status=vision_status,
                        visual_segments=visual_segments,
                    )
            finally:
                self.asset_store.cleanup_local(asset)
        except Exception:
            self.asset_store.update(asset.id, status="failed", error="文件解析或预览生成失败")

    def preview_path(self, asset: Asset, page: Optional[int] = None) -> Path:
        asset_dir = self.settings.previews_dir / asset.id
        if asset.kind == "pdf":
            if page is None:
                page = 1
            return asset_dir / f"page-{page}.png"
        return asset_dir / "cover.jpg"

    def ensure_preview(self, asset: Asset, page: Optional[int] = None) -> Path:
        path = self.preview_path(asset, page)
        if path.exists():
            return path
        source_path = self.asset_store.path_for(asset)
        if asset.kind == "pdf":
            selected_page = page or 1
            if selected_page < 1 or selected_page > asset.page_count:
                raise ValueError("PDF 页码不存在")
            self._render_pdf_page(source_path, path, selected_page)
        elif asset.kind == "image":
            self._render_image(source_path, path)
        else:
            raise ValueError("此文件类型没有预览")
        return path

    def _prepare_preview(self, asset: Asset, path: Path) -> int:
        if asset.kind == "pdf":
            with fitz.open(str(path)) as document:
                page_count = document.page_count
            if page_count:
                self._render_pdf_page(path, self.preview_path(asset, 1), 1)
            return page_count
        if asset.kind == "image":
            self._render_image(path, self.preview_path(asset))
        return 0

    def _extract_visual_segments(self, asset: Asset, path: Path) -> list[dict[str, object]]:
        pipeline = self.knowledge_base.pipeline_status()
        if pipeline.get("vision_adapter") != "local_openai_compatible":
            return []
        model = str(pipeline["vision_model"])
        base_url = str(pipeline["vision_base_url"])
        if asset.kind == "image":
            text = extract_visual_text(
                base_url=base_url,
                model=model,
                image_bytes=path.read_bytes(),
                mime_type=asset.media_type,
            )
            return [{"page": None, "text": text}] if text else []
        if asset.kind != "pdf":
            return []
        max_pages = int(pipeline["vision_max_pages"])
        segments: list[dict[str, object]] = []
        with fitz.open(str(path)) as document:
            for page_no in range(1, min(document.page_count, max_pages) + 1):
                pixmap = document.load_page(page_no - 1).get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
                text = extract_visual_text(
                    base_url=base_url,
                    model=model,
                    image_bytes=pixmap.tobytes("jpeg"),
                    mime_type="image/jpeg",
                )
                if text:
                    segments.append({"page": page_no, "text": text})
        return segments

    @staticmethod
    def _render_pdf_page(source_path: Path, target_path: Path, page: int) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(str(source_path)) as document:
            pixmap = document.load_page(page - 1).get_pixmap(matrix=fitz.Matrix(1.35, 1.35), alpha=False)
            pixmap.save(str(target_path))

    @staticmethod
    def _render_image(source_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source_path) as image:
            image.thumbnail((900, 900))
            image.convert("RGB").save(target_path, format="JPEG", quality=86, optimize=True)
