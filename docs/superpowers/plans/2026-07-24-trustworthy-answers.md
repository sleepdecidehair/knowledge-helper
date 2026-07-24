# 可信回答第一期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每条知识库回答都能展示可核对的来源片段与检索诊断，同时不再按内容模式隐藏用户自己的文本。

**Architecture:** 检索层新增稳定的 `search_with_diagnostics()` 返回值，保留 `search()` 作为兼容包装。Agent SDK 将最后一次实际检索的结果和诊断回传，Python 会话层把它持久化在助手消息 `agent.retrieval` 中；React 从该字段渲染来源摘录和折叠诊断，不从模型回答中解析信息。

**Tech Stack:** Python 3.13、FastAPI、JSON 本地存储、TypeScript、React、HeroUI、Claude Agent SDK、Node test、pytest、oxlint、Vite。

## Global Constraints

- 不新增云端服务或密钥；检索、诊断、会话与索引均保存于本机。
- 普通问答仍必须经 `search_knowledge` 才可交付；明确写入仍只由当前用户消息触发的一次性授权控制。
- 诊断不展示模型私有思维链，只展示用户问题、实际工具查询、本地片段和分数。
- 不以密码、token、key 或其他内容类别筛选、替换或拒绝用户文本；工具权限的来源校验继续保留。
- 前端复用 HeroUI 的 `Card`、`Button`、`ChatSource` 和现有主题变量，不引入另一套 UI 库。

---

## 文件结构

- `app/knowledge_base.py`：计算检索分数、无结果原因和稳定诊断载荷。
- `app/agent.py`：将来源片段和检索诊断保存到助手消息，并向 SDK 内部工具端点返回它们。
- `agent_sdk/src/runner.ts`：保存最后一次实际工具查询及诊断，并回传给 Python 调用方。
- `frontend/src/App.tsx`：定义诊断类型、移除显示脱敏、渲染来源摘录与检索诊断。
- `frontend/src/App.css`：来源详情和诊断的紧凑银黑主题样式。
- `tests/test_knowledge_base.py`：检索诊断、会话持久化与原样文本回归测试。
- `frontend/tests/retrieval-evidence.test.mjs`：前端来源/诊断/无脱敏契约测试。
- `agent_sdk/tests/retrieval-diagnostic-contract.test.mjs`：SDK 返回最后检索诊断的静态契约测试。

## Task 1: 可解释的本地检索结果

**Files:**
- Modify: `app/knowledge_base.py:246-292`
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- Produces: `KnowledgeBase.search_with_diagnostics(question: str, limit: Optional[int] = None, allowed_asset_ids: Optional[set[str]] = None) -> Dict[str, object]`。
- 返回字典固定键：`query`、`query_terms`、`candidate_chunks`、`minimum_score`、`result_count`、`reason`、`results`。
- `KnowledgeBase.search()` 返回 `search_with_diagnostics(...)[\"results\"]`，保持现有调用方兼容。

- [ ] **Step 1: 写失败的检索诊断测试**

    def test_search_diagnostics_explain_hits_and_empty_results(tmp_path: Path):
        _, asset_store, knowledge_base, processor, _ = build_runtime(tmp_path)
        source = tmp_path / "travel.md"
        source.write_text("酒店住宿报销上限为五百元。", encoding="utf-8")
        asset = add_asset(asset_store, source, "差旅制度.md")
        processor.process(asset.id)

        hit = knowledge_base.search_with_diagnostics("住宿报销", limit=3)
        assert hit["query"] == "住宿报销"
        assert hit["reason"] == "matched"
        assert hit["candidate_chunks"] == 1
        assert hit["result_count"] == 1
        assert hit["results"][0]["score"] > 0

        empty = knowledge_base.search_with_diagnostics("不存在的词", limit=3)
        assert empty["reason"] == "no_matching_chunks"
        assert empty["results"] == []

- [ ] **Step 2: 验证测试确实失败**

Run: `.venv/bin/python -m pytest -q tests/test_knowledge_base.py -k search_diagnostics_explain`

Expected: FAIL，原因是 `KnowledgeBase` 尚无 `search_with_diagnostics`。

- [ ] **Step 3: 实现诊断入口并保留兼容 API**

将现有 `search()` 的评分循环移入新方法；无可用查询词、无可检索片段、候选均低于阈值、无词项匹配和命中分别返回 `no_query_terms`、`no_ready_documents`、`below_minimum_score`、`no_matching_chunks`、`matched`。结果 `text` 保留完整文本，供来源组件在本地截取。

    def search(self, question: str, limit: Optional[int] = None, allowed_asset_ids: Optional[set[str]] = None) -> List[Dict[str, object]]:
        return self.search_with_diagnostics(question, limit, allowed_asset_ids)["results"]  # type: ignore[return-value]

    def search_with_diagnostics(self, question: str, limit: Optional[int] = None, allowed_asset_ids: Optional[set[str]] = None) -> Dict[str, object]:
        query_terms = tokenize(question)
        # 读取受锁保护的 chunks、effective_limit 与 minimum_score，评分后构造固定诊断字段。

- [ ] **Step 4: 验证绿灯与旧行为**

Run: `.venv/bin/python -m pytest -q tests/test_knowledge_base.py -k 'search_diagnostics_explain or markdown_is_indexed or pdf_has_page_level'`

Expected: 3 tests PASS。

- [ ] **Step 5: Commit**

    git add app/knowledge_base.py tests/test_knowledge_base.py
    git commit -m "feat: add retrieval diagnostics"

## Task 2: 将真实检索证据贯穿 SDK 与会话

**Files:**
- Modify: `app/agent.py:176-220, 714-750, 790-860, 870-900`
- Modify: `agent_sdk/src/runner.ts:24-28, 195-235, 490-500`
- Create: `agent_sdk/tests/retrieval-diagnostic-contract.test.mjs`
- Test: `tests/test_knowledge_base.py`

**Interfaces:**
- `KnowledgeAgent.search_knowledge()` 返回 `{\"chunks\": ..., \"sources\": ..., \"attachments\": ..., \"diagnostic\": ...}`。
- SDK `SearchResponse` 增加 `diagnostic: JsonRecord` 和 `query: string`；最终 payload 增加 `retrieval: {\"query\": str, \"diagnostic\": JsonRecord}`。
- 助手消息 `agent` 增加 `retrieval`，其 `query` 是 SDK 工具实际调用的搜索词。
- `sources` 每项保留 `asset_id`、`name`、`page`、`chunk_no`、`score`、`excerpt`、`preview_url`、`download_url`。

- [ ] **Step 1: 写失败的会话持久化与 SDK 契约测试**

    def test_answer_persists_retrieval_query_diagnostics_and_source_excerpt(tmp_path: Path):
        result = agent.answer("住宿标准", conversation_id)
        message = agent.get_conversation(conversation_id)["messages"][-1]
        assert result["agent"]["retrieval"]["query"] == "酒店住宿标准"
        assert message["agent"]["retrieval"]["diagnostic"]["reason"] == "matched"
        assert message["sources"][0]["excerpt"] == "酒店住宿报销上限为五百元。"

    test("SDK 将最后实际检索的查询和诊断返回给页面", () => {
      assert.match(source, /diagnostic: JsonRecord/);
      assert.match(source, /retrieval: \{ query: lastSearch\.query, diagnostic: lastSearch\.diagnostic \}/);
    });

- [ ] **Step 2: 验证两个测试失败**

Run: `.venv/bin/python -m pytest -q tests/test_knowledge_base.py -k persists_retrieval`

Expected: FAIL，`agent.retrieval` 尚未存在。

Run: `node --test agent_sdk/tests/retrieval-diagnostic-contract.test.mjs`

Expected: FAIL，`SearchResponse` 尚未包含诊断。

- [ ] **Step 3: 实现来源映射与诊断回传**

`KnowledgeAgent.search_knowledge()` 调用 `knowledge_base.search_with_diagnostics()`。每个展示来源的 `excerpt` 取去除首尾空白后的前 280 个字符，超过时追加省略号；`score` 使用检索层已四舍五入的数值。SDK 的工具回调保存服务端返回的 `diagnostic` 和实际 `searchQuery`。

Python 的普通与流式分支均将下列结构传给 `record_turn()`：

    agent_state = {
        "result_subtype": payload.get("result_subtype"),
        "num_turns": payload.get("num_turns"),
        "compacted": bool(payload.get("compacted")),
        "retrieval_repaired": bool(payload.get("retrieval_repaired")),
        "trace": payload.get("trace") if isinstance(payload.get("trace"), list) else [],
        "retrieval": payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {},
    }

SDK 成功返回结构追加：

    retrieval: {
      query: String(lastSearch.query ?? ""),
      diagnostic: lastSearch.diagnostic ?? {},
    },

- [ ] **Step 4: 验证绿灯、类型检查与 SDK 构建**

Run: `.venv/bin/python -m pytest -q tests/test_knowledge_base.py -k 'persists_retrieval or sdk_session_mapping'`

Expected: PASS。

Run: `node --test agent_sdk/tests/retrieval-diagnostic-contract.test.mjs && (cd agent_sdk && npm run check && npm run build)`

Expected: 1 test PASS，TypeScript 编译成功。

- [ ] **Step 5: Commit**

    git add app/agent.py agent_sdk/src/runner.ts tests/test_knowledge_base.py agent_sdk/tests/retrieval-diagnostic-contract.test.mjs
    git commit -m "feat: persist retrieval evidence"

## Task 3: 在聊天页展示可核对来源和诊断

**Files:**
- Modify: `frontend/src/App.tsx:58-95, 202-230, 460-600`
- Modify: `frontend/src/App.css`
- Create: `frontend/tests/retrieval-evidence.test.mjs`

**Interfaces:**
- 前端 `Source` 增加 `score?: number` 与 `excerpt?: string`。
- `AgentState` 增加 `retrieval?: { query?: string; diagnostic?: RetrievalDiagnostic }`。
- `RetrievalDiagnostic` 包含 Task 1 字段与 `results?: Source[]`。
- `MessageView` 通过 `SourceEvidence` 和 `RetrievalDiagnosticView` 渲染服务端数据。

- [ ] **Step 1: 写失败的前端契约测试**

    test("回答展示来源摘录、得分和检索诊断，且不按内容模式隐藏文本", async () => {
      const source = await readFile(sourcePath, "utf8");
      assert.match(source, /function SourceEvidence/);
      assert.match(source, /来源摘录/);
      assert.match(source, /检索诊断/);
      assert.match(source, /source\.score/);
      assert.doesNotMatch(source, /const redact =/);
      assert.doesNotMatch(source, /redact\(message\.content\)/);
    });

- [ ] **Step 2: 验证测试失败**

Run: `node --test frontend/tests/retrieval-evidence.test.mjs`

Expected: FAIL，`SourceEvidence` 与 `RetrievalDiagnosticView` 尚不存在，且 `redact` 仍存在。

- [ ] **Step 3: 实现消息证据组件并移除显示脱敏**

删除 `redact()`，让 `MessageView`、`ExecutionTrace` 和复制操作直接使用原始字符串。新增 `SourceEvidence`：使用 HeroUI `Card` 显示文件名、页码、片段号、得分、摘录和打开原文的 `Button`。新增 `RetrievalDiagnosticView`：使用原生 `details` 包住 HeroUI `Card`，默认收起；当 `reason !== \"matched\"` 时映射显示无结果原因。不得展示 SDK 提示词、系统指令或隐藏推理。

- [ ] **Step 4: 验证前端测试、检查和构建**

Run: `node --test frontend/tests/*.test.mjs && (cd frontend && npm run lint && npm run build)`

Expected: 所有前端测试通过，lint 无错误，Vite 构建成功。

- [ ] **Step 5: Commit**

    git add frontend/src/App.tsx frontend/src/App.css frontend/tests/retrieval-evidence.test.mjs
    git commit -m "feat: show source evidence and retrieval diagnostics"

## Task 4: 第一阶段端到端回归与人工验收准备

**Files:**
- Modify: `README.md:53-81, 127-155`

**Interfaces:**
- README 中文和英文各说明“来源摘录、检索诊断和无命中说明均为本机检索数据”。
- `/api/status` 继续不暴露本机密钥和本地文件路径。

- [ ] **Step 1: 更新双语 README**

README 明确来源摘录和诊断来自本地检索，不代表模型私有推理；无命中时可以查看阈值和原因。不要记录或展示真实用户资料、会话文本或 API key。

- [ ] **Step 2: 完整验证和服务重启**

Run:

    .venv/bin/python -m pytest -q
    node --test agent_sdk/tests/*.test.mjs
    node --test frontend/tests/*.test.mjs
    (cd agent_sdk && npm run check && npm run build)
    (cd frontend && npm run lint && npm run build)
    git diff --check

Expected: 所有命令退出码为 0。随后重启 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，以 `GET /api/status` 验证 `agent_sdk_ready` 为 `true`。

- [ ] **Step 3: Commit**

    git add README.md
    git commit -m "docs: explain retrieval evidence"

## 计划自查

- 设计中的第一阶段四项要求均有对应任务：来源详情（Task 2、3）、检索诊断（Task 1、2、3）、无内容显示替换（Task 3）、验证与使用说明（Task 4）。
- `search_with_diagnostics`、`retrieval`、`SourceEvidence` 和 `RetrievalDiagnosticView` 在所有任务中使用相同名称。
- 第二、三阶段在独立计划中实施，避免把资料元数据、视觉处理和评测存储与本期检索契约耦合。
