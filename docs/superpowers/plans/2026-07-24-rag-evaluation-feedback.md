# Local RAG Evaluation and Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a project owner create local test questions, run them as isolated batch evaluations, inspect sources and answers, and record useful/not-useful feedback without polluting chat history.

**Architecture:** A new local JSON-backed `QualityStore` owns cases, background jobs, immutable evaluation results and per-message feedback. Evaluation calls use the existing Agent SDK runner with a temporary session scope and no write grant, so the same local retrieval/tool contract is measured while normal conversation memory remains untouched. FastAPI provides project-scoped endpoints; the HeroUI knowledge workspace manages cases and jobs, while assistant messages render feedback and a safe re-retrieval shortcut.

**Tech Stack:** Python 3.13, FastAPI `BackgroundTasks`, local JSON, existing Claude Agent SDK runner, React/TypeScript, HeroUI, Lucide icons.

## Global Constraints

- All cases, expected criteria, results and feedback remain in `data/quality.json` on the local machine.
- Batch runs call the configured DeepSeek-backed Agent SDK only after the user presses the explicit run control; do not run user cases during tests or startup.
- Every evaluation is memory-free, project-scoped and has no write grant; it cannot create a knowledge note or appear in chat history.
- Evaluation answers are evidence for human review, not automatic proof of correctness; scores are limited to deterministic source/criteria availability indicators.
- Feedback accepts all user-authored content and does not redact or classify it.
- Preserve current conversation/session/Agent SDK behavior, asset isolation and source evidence UI.
- Use test-first red-green-refactor for behavior changes; do not stage, commit or publish application changes unless requested.

---

## File Structure

- Create: `app/quality.py` — local case/job/result/feedback store and deterministic result summary helpers.
- Modify: `app/config.py` — `quality_path` local storage setting.
- Modify: `app/agent.py` — persistent message IDs and an isolated, no-write evaluation invocation.
- Modify: `app/main.py` — evaluation case/job/feedback endpoints and conversation feedback decoration.
- Modify: `tests/test_knowledge_base.py` — unit tests for store behavior, message IDs and isolated evaluation input contract.
- Modify: `tests/test_status_runtime.py` — only if new status mocks require compatibility changes.
- Modify: `frontend/src/App.tsx` — evaluation data types, HeroUI case manager/job panel and assistant feedback/re-retrieval actions.
- Modify: `frontend/src/App.css` — black/silver evaluation and feedback layout.
- Create: `frontend/tests/evaluation-feedback.test.mjs` — static UI/API contract test.

### Task 1: Persist project-scoped quality records locally

**Files:**
- Create: `app/quality.py`
- Modify: `app/config.py`
- Modify: `tests/test_knowledge_base.py`

**Interfaces:**
- Produces `QualityStore(settings)` with `load()`, `list_cases(project_id)`, `create_case(project_id, question, expected_answer, expected_sources)`, `update_case(case_id, question=None, expected_answer=None, expected_sources=None)`, `delete_case(case_id)`, `create_job(project_id, case_ids)`, `begin_job(job_id)`, `record_job_result(job_id, case_id, result)`, `fail_job_case(job_id, case_id, message)`, `get_job(job_id)`, `latest_jobs(project_id)`, `record_feedback(conversation_id, message_id, project_id, rating, note)`, and `feedback_for_conversation(conversation_id)`.
- Case fields: `id`, `project_id`, `question`, `expected_answer`, `expected_sources`, `created_at`, `updated_at`.
- Job fields: `id`, `project_id`, `case_ids`, `status`, `total`, `completed`, `failed`, `created_at`, `started_at`, `finished_at`, `results`.

- [ ] **Step 1: Write failing QualityStore tests**

```python
def test_quality_store_keeps_cases_jobs_and_feedback_local_to_project(tmp_path: Path):
    settings, *_ = build_runtime(tmp_path)
    quality = QualityStore(settings)
    quality.load()
    case = quality.create_case("local-default", "住宿上限是多少？", "五百元", ["差旅制度.md"])
    job = quality.create_job("local-default", [case["id"]])
    quality.begin_job(job["id"])
    quality.record_job_result(job["id"], case["id"], {"answer": "五百元", "sources": [{"name": "差旅制度.md"}]})
    quality.record_feedback("conversation-1", "message-1", "local-default", "useful", "资料准确")

    assert quality.list_cases("local-default")[0]["expected_sources"] == ["差旅制度.md"]
    assert quality.get_job(job["id"])["status"] == "completed"
    assert quality.feedback_for_conversation("conversation-1")["message-1"]["rating"] == "useful"
    assert quality.list_cases("other-project") == []
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'quality_store_keeps' -q`

Expected: FAIL with `ImportError` because `app.quality` does not exist.

- [ ] **Step 3: Implement atomic local QualityStore persistence**

```python
class QualityStore:
    def create_case(self, project_id: str, question: str, expected_answer: str = "", expected_sources: Optional[List[str]] = None) -> Dict[str, object]:
        now = now_millis()
        case = {
            "id": uuid.uuid4().hex,
            "project_id": project_id,
            "question": clean_question(question),
            "expected_answer": clean_expected_answer(expected_answer),
            "expected_sources": clean_sources(expected_sources),
            "created_at": now,
            "updated_at": now,
        }
        self._cases[case["id"]] = case
        self._persist_locked()
        return copy_value(case)
```

Keep strings bounded (`question` 4,000 chars, expected answer 4,000, source names 20×160, feedback note 1,000). Jobs transition only `queued → running → completed`; a per-case failure increments `failed` and still permits completion of later cases. Persist each transition using a temporary JSON file replacement.

- [ ] **Step 4: Run the focused and all backend tests**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -q`

Expected: PASS.

### Task 2: Run evaluation cases without conversation memory or writes

**Files:**
- Modify: `app/agent.py`
- Modify: `app/quality.py`
- Modify: `tests/test_knowledge_base.py`

**Interfaces:**
- `KnowledgeAgent.evaluate(question: str, project_id: str) -> Dict[str, object]` returns `answer`, `sources`, `attachments`, `agent`, `context_usage`, `latency_ms` and does not call `ConversationStore.create`, `record_user_message` or `record_turn`.
- `run_evaluation_job(job_id: str, quality_store: QualityStore, agent: KnowledgeAgent) -> None` handles one job's cases sequentially and stores each outcome.
- Conversation message records include a durable UUID `id`; old messages receive one during local normalization.

- [ ] **Step 1: Write failing isolated-evaluation tests**

```python
def test_evaluation_uses_runner_without_creating_chat_history(tmp_path: Path):
    settings, store, kb, processor, writer = build_runtime(tmp_path, api_key="sk-test")
    runner = FakeRunner(answer="五百元")
    agent = KnowledgeAgent(settings, kb, store, writer, runner=runner)
    agent.initialize()

    result = agent.evaluate("住宿上限是多少？", "local-default")

    assert result["answer"] == "五百元"
    assert agent.list_conversations("local-default") == []
    assert runner.calls[0]["write_grant"] is None
    assert runner.calls[0]["session_id"] is None
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'evaluation_uses_runner' -q`

Expected: FAIL with `AttributeError` because `KnowledgeAgent.evaluate` does not exist.

- [ ] **Step 3: Implement isolated runner call and message ID migration**

```python
def evaluate(self, question: str, project_id: str) -> Dict[str, object]:
    evaluation_id = str(uuid.uuid4())
    started = time.monotonic()
    payload = self.runner.run(
        question=question.strip(), conversation_id=evaluation_id, session_id=None,
        write_grant=None, project_id=project_id, agent_profile=profile,
        runtime_settings=self.runtime_settings.agent_values(), session_scope_id=evaluation_id,
        fork_session=False, context_usage=None,
    )
    return normalize_result(payload, latency_ms=round((time.monotonic() - started) * 1000))
```

After `runner.run` returns or raises, remove only the exact temporary `agent_sessions/<UUID>` directory. Normalize every conversation message to include an `id` but do not change its content, sources or timestamps.

- [ ] **Step 4: Run focused evaluation and conversation tests**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'evaluation_uses_runner or conversation' -q`

Expected: PASS.

### Task 3: Expose local quality APIs and background jobs

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_knowledge_base.py`

**Interfaces:**
- `GET /api/evaluations` with `project_id` query returns `{cases, jobs}`.
- `POST/PATCH/DELETE /api/evaluation-cases` manages cases.
- `POST /api/evaluations/run` creates a job and queues `run_evaluation_job` with FastAPI `BackgroundTasks`.
- `GET /api/evaluations/jobs/{job_id}` returns job progress/results.
- `POST /api/conversations/{conversation_id}/messages/{message_id}/feedback` records `rating` (`useful` or `not_useful`) and optional note.
- `GET /api/conversations/{conversation_id}` decorates assistant messages with the feedback record from `QualityStore`.

- [ ] **Step 1: Write failing request-model and API helper tests**

```python
def test_quality_request_models_validate_project_scoped_inputs():
    from app.main import EvaluationCaseRequest, FeedbackRequest
    case = EvaluationCaseRequest(project_id="local-default", question="制度中的住宿标准？", expected_sources=["差旅制度.md"])
    feedback = FeedbackRequest(rating="not_useful", note="没有引用正确资料")

    assert case.expected_sources == ["差旅制度.md"]
    assert feedback.rating == "not_useful"
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base.py -k 'quality_request_models' -q`

Expected: FAIL with `ImportError` because the request models are absent.

- [ ] **Step 3: Implement request models, APIs and feedback decoration**

```python
@app.post("/api/evaluations/run")
def run_evaluations(request: EvaluationRunRequest, background_tasks: BackgroundTasks) -> Dict[str, object]:
    require_project(request.project_id)
    job = quality_store.create_job(request.project_id, request.case_ids)
    background_tasks.add_task(run_evaluation_job, str(job["id"]), quality_store, agent)
    return job
```

Use the exact case's project for every run and reject a case ID from another project. When feedback targets a non-assistant/nonexistent message return 404; a second feedback call updates only that message's local feedback entry. Do not expose local paths, API keys, SDK private chain-of-thought or temporary session directories.

- [ ] **Step 4: Run backend test suite and route-load check**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -c 'from app.main import app; print(len(app.routes))'`

Expected: tests pass and only a positive route count is printed.

### Task 4: Build HeroUI case manager, job inspector and answer feedback

**Files:**
- Create: `frontend/tests/evaluation-feedback.test.mjs`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`

**Interfaces:**
- `Message` consumes optional `feedback`; `MessageView` receives `onFeedback` and `onRetry`.
- `QualitySnapshot` consumes `{cases, jobs}`; `EvaluationCase` and `EvaluationJob` match Task 1 public serialization.
- The knowledge workspace sends the Task 3 requests and polls an active job every 1 second while status is `queued` or `running`.

- [ ] **Step 1: Write failing frontend contract test**

```js
test("界面提供本地评测、回答反馈与重新检索入口", async () => {
  const source = await readFile(sourcePath, "utf8");
  assert.match(source, /\/api\/evaluation-cases/);
  assert.match(source, /\/api\/evaluations\/run/);
  assert.match(source, /\/feedback/);
  assert.match(source, /重新检索/);
  assert.match(source, /评测题集/);
});
```

- [ ] **Step 2: Run the contract test and verify failure**

Run: `node --test frontend/tests/evaluation-feedback.test.mjs`

Expected: FAIL because the local quality controls do not exist.

- [ ] **Step 3: Implement concise black/silver HeroUI controls**

```tsx
<ChatMessage.Action aria-label="这条回答有用" tooltip="有用" onPress={() => onFeedback("useful")}>
  <ThumbsUp size={15} />
</ChatMessage.Action>
<ChatMessage.Action aria-label="这条回答无用" tooltip="无用" onPress={() => onFeedback("not_useful")}>
  <ThumbsDown size={15} />
</ChatMessage.Action>
<ChatMessage.Action aria-label="使用原问题重新检索" tooltip="重新检索" onPress={onRetry}>
  <RefreshCw size={15} />
</ChatMessage.Action>
```

Keep feedback available only for persisted assistant messages. On “重新检索”, place the original preceding user question into the composer and focus it; the user still explicitly sends the new turn. In the knowledge page use HeroUI `Card`, `Input`, `TextArea`, `Button`, `Chip` and existing source attachment cards; show job progress, answer, expected criteria and returned source names. No automatic model judging, no automatic batch run and no sensitive-content filtering.

- [ ] **Step 4: Run frontend and full-project checks**

Run: `node --test frontend/tests/*.test.mjs && (cd frontend && npm run lint && npm run build) && git diff --check && .venv/bin/python -m pytest -q && node --test agent_sdk/tests/*.test.mjs`

Expected: all commands pass; Vite's existing chunk-size advisory remains non-fatal.

## Plan Self-Review

- Coverage: Task 1 implements local project-scoped records; Task 2 isolates Agent SDK evaluation from chat history/writes; Task 3 exposes jobs, cases and feedback; Task 4 provides the requested review loop in the HeroUI interface.
- Placeholder scan: this plan contains no deferred behavior markers.
- Type consistency: Task 1's case/job/feedback records feed Task 3 endpoints and Task 4 types; Task 2's normalized evaluation result is stored by Task 1 and displayed by Task 4.
