import re
import uuid
from pathlib import Path
from typing import Dict

from app.assets import AssetStore
from app.ingestion import AssetProcessor


class KnowledgeWriter:
    """应用拥有写权限；模型只能提交标题和正文，不能指定路径或文件名。"""

    MAX_TITLE_LENGTH = 100
    MAX_CONTENT_LENGTH = 12000

    def __init__(self, asset_store: AssetStore, processor: AssetProcessor):
        self.asset_store = asset_store
        self.processor = processor

    def write_note(self, title: str, content: str, project_id: str = "local-default") -> Dict[str, object]:
        title = self._clean_title(title)
        content = self._clean_content(content)
        stored_name = f"note_{uuid.uuid4().hex[:10]}_{self._safe_stem(title)}.md"
        destination = self.asset_store.settings.knowledge_dir / stored_name
        self.asset_store.settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
        asset = self.asset_store.create(f"{title}.md", stored_name, project_id)
        # 笔记体积有上限，当前同步入库可让工具调用的下一轮立即引用写入结果。
        self.processor.process(asset.id)
        processed = self.asset_store.get(asset.id)
        if not processed or processed.status != "ready":
            raise ValueError("笔记已创建，但索引处理失败")
        return self.asset_store.public(processed)

    def _clean_title(self, title: str) -> str:
        cleaned = re.sub(r"[\x00-\x1f]", " ", str(title)).strip()
        if not cleaned:
            raise ValueError("知识标题不能为空")
        return cleaned[: self.MAX_TITLE_LENGTH]

    def _clean_content(self, content: str) -> str:
        cleaned = str(content).replace("\x00", "").strip()
        if len(cleaned) < 4:
            raise ValueError("知识正文不足，无法写入")
        return cleaned[: self.MAX_CONTENT_LENGTH]

    @staticmethod
    def _safe_stem(title: str) -> str:
        stem = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", title).strip("_")
        return (stem or "knowledge_note")[:60]
