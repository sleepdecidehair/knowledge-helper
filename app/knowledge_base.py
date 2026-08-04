import json
import logging
import math
import re
import threading
from collections import Counter
from contextlib import contextmanager

logger = logging.getLogger(__name__)
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from pypdf import PdfReader

from app.assets import Asset
from app.config import Settings


DEFAULT_CHUNK_SIZE = 900
DEFAULT_CHUNK_OVERLAP = 120
MIN_CHUNK_SIZE = 300
MAX_CHUNK_SIZE = 3000
MAX_CHUNK_OVERLAP = 1500
MIN_TOP_K = 1
MAX_TOP_K = 12
MAX_MINIMUM_SCORE = 10.0
BOUNDARY_MODES = {"natural", "fixed"}
PDF_CHUNK_SCOPES = {"page", "document"}
IMAGE_INDEX_MODES = {"attachment_only", "skip"}
MODEL_ADAPTERS = {"unconfigured", "local_openai_compatible"}
MAX_MODEL_NAME_LENGTH = 200
MAX_MODEL_ENDPOINT_LENGTH = 300
MAX_RERANK_TOP_N = 100
MIN_VISION_MAX_PAGES = 1
MAX_VISION_MAX_PAGES = 12


@dataclass
class Chunk:
    id: str
    asset_id: str
    source: str
    chunk_no: int
    page: Optional[int]
    text: str


def tokenize(text: str) -> List[str]:
    """兼顾英文术语与中文文本的本地无模型分词。"""
    normalized = text.lower()
    latin_terms = re.findall(r"[a-z0-9_./-]{2,}", normalized)
    chinese = re.findall(r"[\u4e00-\u9fff]", normalized)
    bigrams = ["".join(chinese[index : index + 2]) for index in range(len(chinese) - 1)]
    return latin_terms + chinese + bigrams


def split_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    boundary_mode: str = "natural",
) -> Iterable[str]:
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    start = 0
    while start < len(cleaned):
        end = min(start + chunk_size, len(cleaned))
        if boundary_mode == "natural" and end < len(cleaned):
            natural_break = max(cleaned.rfind("\n", start + chunk_size // 2, end), cleaned.rfind("。", start + chunk_size // 2, end))
            if natural_break > start:
                end = natural_break + 1
        chunk = cleaned[start:end].strip()
        if chunk:
            yield chunk
        if end >= len(cleaned):
            break
        start = max(end - chunk_overlap, start + 1)


class KnowledgeBase:
    def __init__(self, app_settings: Settings):
        self.settings = app_settings
        self.chunks: List[Chunk] = []
        self._lock = threading.RLock()
        self._rebuild_lock = threading.RLock()
        self._mysql = None
        self._apply_pipeline(
            self._validated_pipeline(
                {
                    "chunk_size": app_settings.chunk_size,
                    "chunk_overlap": app_settings.chunk_overlap,
                    "boundary_mode": app_settings.chunk_boundary_mode,
                    "pdf_chunk_scope": app_settings.pdf_chunk_scope,
                    "image_index_mode": app_settings.image_index_mode,
                    "top_k": app_settings.top_k,
                    "minimum_score": app_settings.minimum_retrieval_score,
                    "embedding_adapter": "unconfigured",
                    "embedding_model": "",
                    "embedding_base_url": "",
                    "vector_weight": 0.3,
                    "reranker_adapter": "unconfigured",
                    "reranker_model": "",
                    "reranker_base_url": "",
                    "rerank_top_n": 20,
                    "vision_adapter": "unconfigured",
                    "vision_model": "",
                    "vision_base_url": "",
                    "vision_max_pages": 4,
                }
            )
        )

    def _get_mysql(self):
        if not self.settings.mysql_enabled:
            return None
        if self._mysql is None or not self._mysql.open:
            try:
                import pymysql
                self._mysql = pymysql.connect(
                    host=self.settings.mysql_host, port=self.settings.mysql_port,
                    user=self.settings.mysql_user, password=self.settings.mysql_password,
                    database=self.settings.mysql_database,
                    charset="utf8mb4", autocommit=True, connect_timeout=5,
                )
            except Exception:
                return None
        return self._mysql

    def ensure_directories(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        self.ensure_directories()
        self._load_pipeline_settings()
        # 优先从 MySQL 加载 chunks
        mysql_loaded = False
        if self.settings.mysql_enabled:
            conn = self._get_mysql()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT id, asset_id, source, chunk_no, page, text FROM knowledge_chunks")
                        rows = cur.fetchall()
                        if rows:
                            self.chunks = [Chunk(id=r[0], asset_id=r[1], source=r[2] or "", chunk_no=r[3] or 0, page=r[4], text=r[5] or "") for r in rows]
                            mysql_loaded = True
                except Exception:
                    pass
        if mysql_loaded:
            return
        # 回退本地 JSON
        if not self.settings.index_path.exists():
            return
        try:
            payload = json.loads(self.settings.index_path.read_text(encoding="utf-8"))
            self.chunks = [Chunk(**item) for item in payload.get("chunks", [])]
        except (json.JSONDecodeError, OSError, TypeError):
            self.chunks = []

    @contextmanager
    def rebuild_transaction(self) -> Iterator[None]:
        with self._rebuild_lock:
            yield

    def rebuild(self, assets: List[Asset], path_for: Callable[[Asset], Path]) -> Dict[str, int]:
        with self._rebuild_lock:
            with self._lock:
                pipeline = self._pipeline_values()
            rebuilt: List[Chunk] = []
            for asset in assets:
                try:
                    segments = list(self._asset_segments(asset, path_for(asset), pipeline["pdf_chunk_scope"], pipeline["image_index_mode"]))
                except (OSError, ValueError):
                    continue
                chunk_no = 0
                for page, source_text in segments:
                    for chunk_text in split_text(
                        source_text,
                        pipeline["chunk_size"],
                        pipeline["chunk_overlap"],
                        pipeline["boundary_mode"],
                    ):
                        chunk_no += 1
                        digest = sha256(f"{asset.id}:{page}:{chunk_no}:{chunk_text}".encode("utf-8")).hexdigest()[:16]
                        rebuilt.append(Chunk(digest, asset.id, asset.original_name, chunk_no, page, chunk_text))

            with self._lock:
                self.ensure_directories()
                payload = {
                    "version": 4,
                    "pipeline": pipeline,
                    "chunks": [asdict(chunk) for chunk in rebuilt],
                }
                self.settings.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                self.chunks = rebuilt
                # 同步到 MySQL
                self._sync_chunks_to_mysql_locked(rebuilt)
            return {"documents": len(assets), "chunks": len(rebuilt)}

    def _sync_chunks_to_mysql_locked(self, chunks: List[Chunk]) -> None:
        conn = self._get_mysql()
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM knowledge_chunks")
                for chunk in chunks:
                    cur.execute(
                        "INSERT INTO knowledge_chunks (id, asset_id, source, chunk_no, page, text) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (chunk.id, chunk.asset_id, chunk.source, chunk.chunk_no, chunk.page, chunk.text),
                    )
        except Exception as exc:
            logger.warning("Chunks MySQL 同步失败: %s", exc)

    def status(self) -> Dict[str, int]:
        with self._lock:
            return {"documents": len({chunk.asset_id for chunk in self.chunks}), "chunks": len(self.chunks)}

    def chunk_counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(Counter(chunk.asset_id for chunk in self.chunks))

    def chunking_status(self) -> Dict[str, int]:
        with self._lock:
            return {"chunk_size": self.chunk_size, "chunk_overlap": self.chunk_overlap}

    def pipeline_status(self) -> Dict[str, object]:
        with self._lock:
            return {
                **self._pipeline_values(),
                "retrieval_engine": "keyword_tfidf",
                "vector_index": "configured_pending" if self.embedding_adapter != "unconfigured" else "unconfigured",
                "reranker": "configured_pending" if self.reranker_adapter != "unconfigured" else "unconfigured",
                "vision_index": "configured" if self.vision_adapter != "unconfigured" else "unconfigured",
            }

    def update_chunking(self, chunk_size: int, chunk_overlap: int) -> Dict[str, int]:
        updated = self.update_pipeline(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        return {"chunk_size": int(updated["chunk_size"]), "chunk_overlap": int(updated["chunk_overlap"])}

    def update_pipeline(self, **changes: object) -> Dict[str, object]:
        with self._lock:
            candidate = {**self._pipeline_values(), **changes}
            validated = self._validated_pipeline(candidate)
            self._apply_pipeline(validated)
            self.ensure_directories()
            temporary_path = self.settings.pipeline_settings_path.with_suffix(".json.tmp")
            temporary_path.write_text(
                json.dumps({"version": 1, **validated}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(self.settings.pipeline_settings_path)
            return self.pipeline_status()

    def _load_pipeline_settings(self) -> None:
        source_path = self.settings.pipeline_settings_path
        is_legacy_chunking = False
        if not source_path.exists() and self.settings.chunking_path.exists():
            source_path = self.settings.chunking_path
            is_legacy_chunking = True
        if not source_path.exists():
            return
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
            payload = {**self._pipeline_values(), **payload}
            validated = self._validated_pipeline(payload)
        except (OSError, ValueError, TypeError, KeyError):
            return
        with self._lock:
            self._apply_pipeline(validated)

    @staticmethod
    def _validated_pipeline(values: Dict[str, object]) -> Dict[str, object]:
        chunk_size = values.get("chunk_size")
        chunk_overlap = values.get("chunk_overlap")
        if not isinstance(chunk_size, int) or not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError(f"切片长度必须在 {MIN_CHUNK_SIZE} 到 {MAX_CHUNK_SIZE} 个字符之间。")
        if not isinstance(chunk_overlap, int) or not 0 <= chunk_overlap <= MAX_CHUNK_OVERLAP:
            raise ValueError(f"切片重叠必须在 0 到 {MAX_CHUNK_OVERLAP} 个字符之间。")
        if chunk_overlap >= chunk_size:
            raise ValueError("切片重叠必须小于切片长度。")
        boundary_mode = values.get("boundary_mode")
        if boundary_mode not in BOUNDARY_MODES:
            raise ValueError("切片边界模式必须是 natural 或 fixed。")
        pdf_chunk_scope = values.get("pdf_chunk_scope")
        if pdf_chunk_scope not in PDF_CHUNK_SCOPES:
            raise ValueError("PDF 切片范围必须是 page 或 document。")
        image_index_mode = values.get("image_index_mode")
        if image_index_mode not in IMAGE_INDEX_MODES:
            raise ValueError("图片索引模式必须是 attachment_only 或 skip。")
        top_k = values.get("top_k")
        if not isinstance(top_k, int) or not MIN_TOP_K <= top_k <= MAX_TOP_K:
            raise ValueError(f"召回数量必须在 {MIN_TOP_K} 到 {MAX_TOP_K} 之间。")
        minimum_score = values.get("minimum_score")
        if not isinstance(minimum_score, (int, float)) or not 0 <= float(minimum_score) <= MAX_MINIMUM_SCORE:
            raise ValueError(f"最低关键词得分必须在 0 到 {MAX_MINIMUM_SCORE} 之间。")
        embedding_adapter, embedding_model, embedding_base_url = KnowledgeBase._validated_model_adapter(
            "向量嵌入", values.get("embedding_adapter"), values.get("embedding_model"), values.get("embedding_base_url")
        )
        reranker_adapter, reranker_model, reranker_base_url = KnowledgeBase._validated_model_adapter(
            "重排", values.get("reranker_adapter"), values.get("reranker_model"), values.get("reranker_base_url")
        )
        vision_adapter, vision_model, vision_base_url = KnowledgeBase._validated_model_adapter(
            "视觉", values.get("vision_adapter"), values.get("vision_model"), values.get("vision_base_url")
        )
        vector_weight = values.get("vector_weight")
        if not isinstance(vector_weight, (int, float)) or not 0 <= float(vector_weight) <= 1:
            raise ValueError("向量权重必须在 0 到 1 之间。")
        rerank_top_n = values.get("rerank_top_n")
        if not isinstance(rerank_top_n, int) or not 1 <= rerank_top_n <= MAX_RERANK_TOP_N:
            raise ValueError(f"重排候选数必须在 1 到 {MAX_RERANK_TOP_N} 之间。")
        vision_max_pages = values.get("vision_max_pages")
        if not isinstance(vision_max_pages, int) or not MIN_VISION_MAX_PAGES <= vision_max_pages <= MAX_VISION_MAX_PAGES:
            raise ValueError(f"视觉识别页数必须在 {MIN_VISION_MAX_PAGES} 到 {MAX_VISION_MAX_PAGES} 之间。")
        return {
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "boundary_mode": boundary_mode,
            "pdf_chunk_scope": pdf_chunk_scope,
            "image_index_mode": image_index_mode,
            "top_k": top_k,
            "minimum_score": round(float(minimum_score), 4),
            "embedding_adapter": embedding_adapter,
            "embedding_model": embedding_model,
            "embedding_base_url": embedding_base_url,
            "vector_weight": round(float(vector_weight), 4),
            "reranker_adapter": reranker_adapter,
            "reranker_model": reranker_model,
            "reranker_base_url": reranker_base_url,
            "rerank_top_n": rerank_top_n,
            "vision_adapter": vision_adapter,
            "vision_model": vision_model,
            "vision_base_url": vision_base_url,
            "vision_max_pages": vision_max_pages,
        }

    @staticmethod
    def _validated_model_adapter(label: str, adapter: object, model: object, base_url: object) -> Tuple[str, str, str]:
        if adapter not in MODEL_ADAPTERS:
            raise ValueError(f"{label}适配器必须是 unconfigured 或 local_openai_compatible。")
        model = str(model or "").strip()
        base_url = str(base_url or "").strip().rstrip("/")
        if len(model) > MAX_MODEL_NAME_LENGTH or len(base_url) > MAX_MODEL_ENDPOINT_LENGTH:
            raise ValueError(f"{label}模型名或地址过长。")
        if adapter == "unconfigured":
            return "unconfigured", "", ""
        if not model or not base_url:
            raise ValueError(f"配置{label}服务时必须填写模型名和本机地址。")
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"{label}服务地址只能是本机 HTTP 地址（localhost 或 127.0.0.1）。")
        return str(adapter), model, base_url

    def _apply_pipeline(self, values: Dict[str, object]) -> None:
        self.chunk_size = int(values["chunk_size"])
        self.chunk_overlap = int(values["chunk_overlap"])
        self.boundary_mode = str(values["boundary_mode"])
        self.pdf_chunk_scope = str(values["pdf_chunk_scope"])
        self.image_index_mode = str(values["image_index_mode"])
        self.top_k = int(values["top_k"])
        self.minimum_score = float(values["minimum_score"])
        self.embedding_adapter = str(values["embedding_adapter"])
        self.embedding_model = str(values["embedding_model"])
        self.embedding_base_url = str(values["embedding_base_url"])
        self.vector_weight = float(values["vector_weight"])
        self.reranker_adapter = str(values["reranker_adapter"])
        self.reranker_model = str(values["reranker_model"])
        self.reranker_base_url = str(values["reranker_base_url"])
        self.rerank_top_n = int(values["rerank_top_n"])
        self.vision_adapter = str(values["vision_adapter"])
        self.vision_model = str(values["vision_model"])
        self.vision_base_url = str(values["vision_base_url"])
        self.vision_max_pages = int(values["vision_max_pages"])

    def _pipeline_values(self) -> Dict[str, object]:
        return {
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "boundary_mode": self.boundary_mode,
            "pdf_chunk_scope": self.pdf_chunk_scope,
            "image_index_mode": self.image_index_mode,
            "top_k": self.top_k,
            "minimum_score": self.minimum_score,
            "embedding_adapter": self.embedding_adapter,
            "embedding_model": self.embedding_model,
            "embedding_base_url": self.embedding_base_url,
            "vector_weight": self.vector_weight,
            "reranker_adapter": self.reranker_adapter,
            "reranker_model": self.reranker_model,
            "reranker_base_url": self.reranker_base_url,
            "rerank_top_n": self.rerank_top_n,
            "vision_adapter": self.vision_adapter,
            "vision_model": self.vision_model,
            "vision_base_url": self.vision_base_url,
            "vision_max_pages": self.vision_max_pages,
        }

    def count_for_asset(self, asset_id: str) -> int:
        with self._lock:
            return sum(chunk.asset_id == asset_id for chunk in self.chunks)

    def search(
        self,
        question: str,
        limit: Optional[int] = None,
        allowed_asset_ids: Optional[set[str]] = None,
    ) -> List[Dict[str, object]]:
        diagnostics = self.search_with_diagnostics(question, limit, allowed_asset_ids)
        results = diagnostics["results"]
        return results if isinstance(results, list) else []

    def search_with_diagnostics(
        self,
        question: str,
        limit: Optional[int] = None,
        allowed_asset_ids: Optional[set[str]] = None,
    ) -> Dict[str, object]:
        normalized_question = question.strip()
        query_terms = tokenize(normalized_question)
        with self._lock:
            chunks = [chunk for chunk in self.chunks if allowed_asset_ids is None or chunk.asset_id in allowed_asset_ids]
            effective_limit = self.top_k if limit is None else limit
            minimum_score = self.minimum_score
        diagnostics: Dict[str, object] = {
            "query": normalized_question,
            "query_terms": query_terms,
            "candidate_chunks": len(chunks),
            "minimum_score": minimum_score,
            "result_count": 0,
            "reason": "matched",
            "results": [],
        }
        if not query_terms:
            diagnostics["reason"] = "no_query_terms"
            return diagnostics
        if not chunks:
            diagnostics["reason"] = "no_ready_documents"
            return diagnostics

        document_frequency: Counter = Counter()
        chunk_terms: List[Counter] = []
        for chunk in chunks:
            terms = Counter(tokenize(chunk.text))
            chunk_terms.append(terms)
            document_frequency.update(terms.keys())

        query_counter = Counter(query_terms)
        scored = []
        has_term_match = False
        for chunk, term_counter in zip(chunks, chunk_terms):
            length_norm = math.sqrt(sum(value * value for value in term_counter.values())) or 1.0
            score = 0.0
            for term, query_count in query_counter.items():
                if term in term_counter:
                    idf = math.log((len(chunks) + 1) / (document_frequency[term] + 1)) + 1
                    score += (term_counter[term] / length_norm) * idf * query_count
            if score > 0:
                has_term_match = True
            if score >= minimum_score and score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        results = [
            {
                "id": chunk.id,
                "asset_id": chunk.asset_id,
                "source": chunk.source,
                "chunk_no": chunk.chunk_no,
                "page": chunk.page,
                "text": chunk.text,
                "score": round(score, 4),
            }
            for score, chunk in scored[:effective_limit]
        ]
        diagnostics["results"] = results
        diagnostics["result_count"] = len(results)
        if results:
            return diagnostics
        diagnostics["reason"] = "below_minimum_score" if has_term_match else "no_matching_chunks"
        return diagnostics

    @staticmethod
    def _asset_segments(asset: Asset, path: Path, pdf_chunk_scope: str, image_index_mode: str) -> Iterable[Tuple[Optional[int], str]]:
        visual_by_page = KnowledgeBase._visual_text_by_page(asset)
        if asset.kind == "pdf":
            try:
                reader = PdfReader(str(path))
                extracted_pages: List[Tuple[int, str]] = []
                for page_no, page in enumerate(reader.pages, start=1):
                    text = (page.extract_text() or "").strip()
                    visual_text = visual_by_page.get(page_no, "")
                    if visual_text:
                        text = f"{text}\n\n视觉提取：\n{visual_text}".strip()
                    if text:
                        extracted_pages.append((page_no, text))
                if not extracted_pages:
                    yield None, f"PDF 文档：{asset.original_name}。文本不可提取，但该文件可作为附件下载。"
                elif pdf_chunk_scope == "document":
                    merged_text = "\n\n".join(f"第 {page_no} 页：\n{text}" for page_no, text in extracted_pages)
                    yield None, f"文档：{asset.original_name}。\n{merged_text}"
                else:
                    for page_no, text in extracted_pages:
                        yield page_no, f"文档：{asset.original_name}；第 {page_no} 页。\n{text}"
            except Exception:
                yield None, f"PDF 文档：{asset.original_name}。文本不可提取，但该文件可作为附件下载。"
        elif asset.kind == "image":
            if image_index_mode == "skip":
                return
            visual_text = visual_by_page.get(None, "")
            if visual_text:
                yield None, f"图片资料：{asset.original_name}。视觉提取：\n{visual_text}"
            else:
                # 未接入视觉模型时只索引文件身份，避免把不存在的图像内容编造进知识库。
                yield None, f"图片资料：{asset.original_name}。该图片可作为回答附件返回；尚未进行 OCR 或视觉内容识别。"
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                text = ""
            if text:
                yield None, f"文档：{asset.original_name}。\n{text}"

    @staticmethod
    def _visual_text_by_page(asset: Asset) -> Dict[Optional[int], str]:
        values: Dict[Optional[int], str] = {}
        for segment in asset.visual_segments:
            if not isinstance(segment, dict):
                continue
            raw_page = segment.get("page")
            page = raw_page if isinstance(raw_page, int) and raw_page > 0 else None
            text = str(segment.get("text") or "").strip()
            if text:
                values[page] = text
        return values
