"""Local, project-scoped RAG evaluation cases, jobs and human feedback."""

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.config import Settings


MAX_QUESTION_LENGTH = 4_000
MAX_EXPECTED_ANSWER_LENGTH = 4_000
MAX_EXPECTED_SOURCES = 20
MAX_SOURCE_NAME_LENGTH = 160
MAX_FEEDBACK_NOTE_LENGTH = 1_000
MAX_RESULT_ANSWER_LENGTH = 12_000
MAX_RESULT_ERROR_LENGTH = 500
MAX_JOB_CASES = 30
RATINGS = {"useful", "not_useful"}


def _copy(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _now() -> int:
    return int(time.time() * 1000)


def _clean_text(value: object, limit: int) -> str:
    return "".join(character for character in str(value or "") if character >= " " and character != "\x7f").strip()[:limit]


class QualityStore:
    """Atomic local store; it never invokes the model or accesses user files."""

    def __init__(self, app_settings: Settings):
        self.path: Path = app_settings.quality_path
        self._lock = threading.RLock()
        self._cases: Dict[str, Dict[str, object]] = {}
        self._jobs: Dict[str, Dict[str, object]] = {}
        self._feedback: Dict[str, Dict[str, object]] = {}

    def load(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                return
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                return
            if not isinstance(payload, dict):
                return
            raw_cases = payload.get("cases")
            raw_jobs = payload.get("jobs")
            raw_feedback = payload.get("feedback")
            self._cases = self._load_records(raw_cases, self._case_from_raw)
            self._jobs = self._load_records(raw_jobs, self._job_from_raw)
            self._feedback = self._load_records(raw_feedback, self._feedback_from_raw)

    def list_cases(self, project_id: str) -> List[Dict[str, object]]:
        with self._lock:
            rows = [case for case in self._cases.values() if case["project_id"] == project_id]
            return _copy(sorted(rows, key=lambda item: (int(item["updated_at"]), str(item["id"])), reverse=True))  # type: ignore[return-value]

    def get_case(self, case_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            case = self._cases.get(case_id)
            return _copy(case) if case else None  # type: ignore[return-value]

    def create_case(
        self,
        project_id: str,
        question: str,
        expected_answer: str = "",
        expected_sources: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        with self._lock:
            now = _now()
            case_id = uuid.uuid4().hex
            case = {
                "id": case_id,
                "project_id": self._clean_project_id(project_id),
                "question": self._clean_question(question),
                "expected_answer": _clean_text(expected_answer, MAX_EXPECTED_ANSWER_LENGTH),
                "expected_sources": self._clean_sources(expected_sources),
                "created_at": now,
                "updated_at": now,
            }
            self._cases[case_id] = case
            self._persist_locked()
            return _copy(case)  # type: ignore[return-value]

    def update_case(
        self,
        case_id: str,
        question: Optional[str] = None,
        expected_answer: Optional[str] = None,
        expected_sources: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        with self._lock:
            case = self._cases[case_id]
            if question is not None:
                case["question"] = self._clean_question(question)
            if expected_answer is not None:
                case["expected_answer"] = _clean_text(expected_answer, MAX_EXPECTED_ANSWER_LENGTH)
            if expected_sources is not None:
                case["expected_sources"] = self._clean_sources(expected_sources)
            case["updated_at"] = _now()
            self._persist_locked()
            return _copy(case)  # type: ignore[return-value]

    def delete_case(self, case_id: str) -> Dict[str, object]:
        with self._lock:
            case = self._cases.pop(case_id)
            self._persist_locked()
            return _copy(case)  # type: ignore[return-value]

    def create_job(self, project_id: str, case_ids: List[str]) -> Dict[str, object]:
        with self._lock:
            normalized_project_id = self._clean_project_id(project_id)
            normalized_ids = self._clean_case_ids(case_ids)
            cases = [self._cases.get(case_id) for case_id in normalized_ids]
            if any(case is None or case["project_id"] != normalized_project_id for case in cases):
                raise ValueError("评测题目必须属于当前项目")
            snapshots = {
                case_id: self._case_snapshot(case)
                for case_id, case in zip(normalized_ids, cases)
                if case is not None
            }
            now = _now()
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "project_id": normalized_project_id,
                "case_ids": normalized_ids,
                "case_snapshots": snapshots,
                "status": "queued",
                "total": len(normalized_ids),
                "completed": 0,
                "failed": 0,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "results": [],
            }
            self._jobs[job_id] = job
            self._persist_locked()
            return _copy(job)  # type: ignore[return-value]

    def begin_job(self, job_id: str) -> Dict[str, object]:
        with self._lock:
            job = self._jobs[job_id]
            if job["status"] == "queued":
                job["status"] = "running"
                job["started_at"] = _now()
                self._persist_locked()
            return _copy(job)  # type: ignore[return-value]

    def record_job_result(self, job_id: str, case_id: str, result: Dict[str, object]) -> Dict[str, object]:
        with self._lock:
            job = self._jobs[job_id]
            case = self._case_for_job_locked(job, case_id)
            if case is None:
                raise ValueError("评测题目不存在或不属于此任务")
            payload = self._result_payload(case, result)
            self._put_job_result_locked(job, payload)
            self._persist_locked()
            return _copy(job)  # type: ignore[return-value]

    def fail_job_case(self, job_id: str, case_id: str, message: str) -> Dict[str, object]:
        with self._lock:
            job = self._jobs[job_id]
            case = self._case_for_job_locked(job, case_id)
            if case is None:
                raise ValueError("评测题目不存在或不属于此任务")
            self._put_job_result_locked(
                job,
                {
                    "case_id": case_id,
                    "question": case["question"],
                    "expected_answer": case["expected_answer"],
                    "expected_sources": case["expected_sources"],
                    "error": _clean_text(message, MAX_RESULT_ERROR_LENGTH) or "评测执行失败",
                    "created_at": _now(),
                },
            )
            self._persist_locked()
            return _copy(job)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return _copy(job) if job else None  # type: ignore[return-value]

    def case_for_job(self, job_id: str, case_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            case = self._case_for_job_locked(job, case_id)
            return _copy(case) if case else None  # type: ignore[return-value]

    def recover_interrupted_jobs(self) -> int:
        """Finish persisted queued/running jobs without silently triggering new model calls."""
        with self._lock:
            recovered = 0
            for job in self._jobs.values():
                if job.get("status") not in {"queued", "running"}:
                    continue
                case_ids = job.get("case_ids")
                if not isinstance(case_ids, list):
                    continue
                completed_case_ids = {
                    item.get("case_id")
                    for item in job.get("results", [])
                    if isinstance(item, dict) and isinstance(item.get("case_id"), str)
                }
                for case_id in case_ids:
                    if not isinstance(case_id, str) or case_id in completed_case_ids:
                        continue
                    case = self._case_for_job_locked(job, case_id)
                    if case is None:
                        continue
                    self._put_job_result_locked(
                        job,
                        {
                            "case_id": case_id,
                            "question": case["question"],
                            "expected_answer": case["expected_answer"],
                            "expected_sources": case["expected_sources"],
                            "error": "服务重启导致评测中断，请重新运行。",
                            "created_at": _now(),
                        },
                    )
                if job.get("status") != "completed":
                    job["status"] = "completed"
                    job["finished_at"] = _now()
                recovered += 1
            if recovered:
                self._persist_locked()
            return recovered

    def latest_jobs(self, project_id: str, limit: int = 10) -> List[Dict[str, object]]:
        with self._lock:
            rows = [job for job in self._jobs.values() if job["project_id"] == project_id]
            return _copy(sorted(rows, key=lambda item: (int(item["created_at"]), str(item["id"])), reverse=True)[:limit])  # type: ignore[return-value]

    def has_project_records(self, project_id: str) -> bool:
        with self._lock:
            return any(
                record.get("project_id") == project_id
                for collection in (self._cases, self._jobs, self._feedback)
                for record in collection.values()
            )

    def has_active_jobs(self, project_id: str) -> bool:
        with self._lock:
            return any(
                job.get("project_id") == project_id and job.get("status") in {"queued", "running"}
                for job in self._jobs.values()
            )

    def delete_project_records(self, project_id: str) -> Dict[str, int]:
        """Remove local quality records only after the user explicitly deletes a project."""
        with self._lock:
            removed = {
                "cases": self._delete_records_for_project_locked(self._cases, project_id),
                "jobs": self._delete_records_for_project_locked(self._jobs, project_id),
                "feedback": self._delete_records_for_project_locked(self._feedback, project_id),
            }
            if any(removed.values()):
                self._persist_locked()
            return removed

    def delete_feedback_for_conversation(self, conversation_id: str) -> int:
        with self._lock:
            keys = [
                key
                for key, record in self._feedback.items()
                if record.get("conversation_id") == conversation_id
            ]
            for key in keys:
                del self._feedback[key]
            if keys:
                self._persist_locked()
            return len(keys)

    def record_feedback(
        self,
        conversation_id: str,
        message_id: str,
        project_id: str,
        rating: str,
        note: str = "",
    ) -> Dict[str, object]:
        if rating not in RATINGS:
            raise ValueError("反馈类型必须是 useful 或 not_useful")
        with self._lock:
            key = f"{conversation_id}:{message_id}"
            record = {
                "conversation_id": str(conversation_id),
                "message_id": str(message_id),
                "project_id": self._clean_project_id(project_id),
                "rating": rating,
                "note": _clean_text(note, MAX_FEEDBACK_NOTE_LENGTH),
                "updated_at": _now(),
            }
            self._feedback[key] = record
            self._persist_locked()
            return _copy(record)  # type: ignore[return-value]

    def feedback_for_conversation(self, conversation_id: str) -> Dict[str, Dict[str, object]]:
        with self._lock:
            return {
                str(record["message_id"]): _copy(record)  # type: ignore[dict-item]
                for record in self._feedback.values()
                if record["conversation_id"] == conversation_id
            }

    def _put_job_result_locked(self, job: Dict[str, object], result: Dict[str, object]) -> None:
        results = job["results"]
        assert isinstance(results, list)
        case_id = result["case_id"]
        results[:] = [item for item in results if not isinstance(item, dict) or item.get("case_id") != case_id]
        results.append(result)
        job["completed"] = len(results)
        job["failed"] = sum(1 for item in results if isinstance(item, dict) and item.get("error"))
        if int(job["completed"]) >= int(job["total"]):
            job["status"] = "completed"
            job["finished_at"] = _now()

    @staticmethod
    def _delete_records_for_project_locked(
        collection: Dict[str, Dict[str, object]],
        project_id: str,
    ) -> int:
        keys = [key for key, record in collection.items() if record.get("project_id") == project_id]
        for key in keys:
            del collection[key]
        return len(keys)

    def _case_for_job_locked(self, job: Dict[str, object], case_id: str) -> Optional[Dict[str, object]]:
        case_ids = job.get("case_ids")
        if not isinstance(case_ids, list) or case_id not in case_ids:
            return None
        snapshots = job.get("case_snapshots")
        snapshot = snapshots.get(case_id) if isinstance(snapshots, dict) else None
        if isinstance(snapshot, dict):
            return snapshot
        return self._cases.get(case_id)

    @classmethod
    def _load_records(
        cls,
        value: object,
        parser: Callable[[str, Dict[str, object]], Dict[str, object]],
    ) -> Dict[str, Dict[str, object]]:
        """Skip individual corrupt local records instead of blocking application startup."""
        records = value if isinstance(value, dict) else {}
        loaded: Dict[str, Dict[str, object]] = {}
        for record_id, raw in records.items():
            if not isinstance(record_id, str) or not isinstance(raw, dict):
                continue
            try:
                parsed = parser(record_id, raw)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                loaded[record_id] = parsed
        return loaded

    def _result_payload(self, case: Dict[str, object], result: Dict[str, object]) -> Dict[str, object]:
        sources = result.get("sources")
        source_rows = [item for item in sources if isinstance(item, dict)][:12] if isinstance(sources, list) else []
        source_names = {str(item.get("name") or item.get("source") or "") for item in source_rows}
        expected_sources = case["expected_sources"]
        assert isinstance(expected_sources, list)
        return {
            "case_id": case["id"],
            "question": case["question"],
            "expected_answer": case["expected_answer"],
            "expected_sources": expected_sources,
            "answer": _clean_text(result.get("answer"), MAX_RESULT_ANSWER_LENGTH),
            "sources": _copy(source_rows),
            "source_match": None if not expected_sources else all(str(source) in source_names for source in expected_sources),
            "retrieval": _copy(result.get("agent", {}).get("retrieval", {})) if isinstance(result.get("agent"), dict) else {},
            "latency_ms": result.get("latency_ms") if isinstance(result.get("latency_ms"), int) and result.get("latency_ms") >= 0 else 0,
            "created_at": _now(),
        }

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps({"version": 1, "cases": self._cases, "jobs": self._jobs, "feedback": self._feedback}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self.path)

    @staticmethod
    def _clean_project_id(value: object) -> str:
        project_id = _clean_text(value, 64)
        if not project_id:
            raise ValueError("项目标识不能为空")
        return project_id

    @staticmethod
    def _clean_question(value: object) -> str:
        question = _clean_text(value, MAX_QUESTION_LENGTH)
        if not question:
            raise ValueError("评测问题不能为空")
        return question

    @staticmethod
    def _clean_sources(values: Optional[List[str]]) -> List[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("预期来源必须是列表")
        cleaned: List[str] = []
        seen: set[str] = set()
        for value in values:
            source = _clean_text(value, MAX_SOURCE_NAME_LENGTH)
            if source and source not in seen:
                cleaned.append(source)
                seen.add(source)
        if len(cleaned) > MAX_EXPECTED_SOURCES:
            raise ValueError(f"预期来源不能超过 {MAX_EXPECTED_SOURCES} 个")
        return cleaned

    @staticmethod
    def _clean_case_ids(values: List[str]) -> List[str]:
        if not isinstance(values, list):
            raise ValueError("评测题目必须是列表")
        cleaned: List[str] = []
        seen: set[str] = set()
        for value in values:
            case_id = _clean_text(value, 64)
            if case_id and case_id not in seen:
                cleaned.append(case_id)
                seen.add(case_id)
        if not cleaned:
            raise ValueError("至少选择一道评测题")
        if len(cleaned) > MAX_JOB_CASES:
            raise ValueError(f"单次评测不能超过 {MAX_JOB_CASES} 题")
        return cleaned

    @classmethod
    def _case_from_raw(cls, case_id: str, raw: Dict[str, object]) -> Dict[str, object]:
        now = _now()
        return {
            "id": case_id,
            "project_id": cls._clean_project_id(raw.get("project_id") or "local-default"),
            "question": cls._clean_question(raw.get("question") or ""),
            "expected_answer": _clean_text(raw.get("expected_answer"), MAX_EXPECTED_ANSWER_LENGTH),
            "expected_sources": cls._clean_sources(raw.get("expected_sources") if isinstance(raw.get("expected_sources"), list) else []),
            "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), int) else now,
            "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), int) else now,
        }

    @staticmethod
    def _case_snapshot(case: Dict[str, object]) -> Dict[str, object]:
        expected_sources = case.get("expected_sources")
        return {
            "id": case["id"],
            "project_id": case["project_id"],
            "question": case["question"],
            "expected_answer": case["expected_answer"],
            "expected_sources": list(expected_sources) if isinstance(expected_sources, list) else [],
        }

    @classmethod
    def _job_from_raw(cls, job_id: str, raw: Dict[str, object]) -> Dict[str, object]:
        status = raw.get("status") if raw.get("status") in {"queued", "running", "completed"} else "completed"
        results = raw.get("results") if isinstance(raw.get("results"), list) else []
        now = _now()
        case_ids = cls._clean_case_ids(raw.get("case_ids") if isinstance(raw.get("case_ids"), list) else [job_id])
        raw_snapshots = raw.get("case_snapshots") if isinstance(raw.get("case_snapshots"), dict) else {}
        snapshots: Dict[str, Dict[str, object]] = {}
        for case_id in case_ids:
            snapshot = raw_snapshots.get(case_id)
            if not isinstance(snapshot, dict):
                continue
            try:
                snapshots[case_id] = cls._case_snapshot(cls._case_from_raw(case_id, snapshot))
            except (TypeError, ValueError):
                continue
        return {
            "id": job_id,
            "project_id": cls._clean_project_id(raw.get("project_id") or "local-default"),
            "case_ids": case_ids,
            "case_snapshots": snapshots,
            "status": status,
            "total": raw.get("total") if isinstance(raw.get("total"), int) and raw.get("total") >= 1 else 1,
            "completed": raw.get("completed") if isinstance(raw.get("completed"), int) and raw.get("completed") >= 0 else len(results),
            "failed": raw.get("failed") if isinstance(raw.get("failed"), int) and raw.get("failed") >= 0 else 0,
            "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), int) else now,
            "started_at": raw.get("started_at") if isinstance(raw.get("started_at"), int) else None,
            "finished_at": raw.get("finished_at") if isinstance(raw.get("finished_at"), int) else None,
            "results": [item for item in results if isinstance(item, dict)],
        }

    @classmethod
    def _feedback_from_raw(cls, key: str, raw: Dict[str, object]) -> Dict[str, object]:
        rating = raw.get("rating") if raw.get("rating") in RATINGS else "not_useful"
        return {
            "conversation_id": _clean_text(raw.get("conversation_id"), 64),
            "message_id": _clean_text(raw.get("message_id"), 64) or key.rsplit(":", 1)[-1],
            "project_id": cls._clean_project_id(raw.get("project_id") or "local-default"),
            "rating": rating,
            "note": _clean_text(raw.get("note"), MAX_FEEDBACK_NOTE_LENGTH),
            "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), int) else _now(),
        }
