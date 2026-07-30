# 流式终止恢复与资料弹窗预览 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让意外断开的 SSE 等待已保存回答而非误报失败，并把知识库文件预览保留在站内弹窗。

**Architecture:** Python runner 周期性产生可转发的 SSE 心跳；React 对没有 `done` 的 EOF 轮询同一会话但不重新调用模型。App 根组件拥有单一预览弹窗状态，消息来源和附件都复用同一个打开函数。

**Tech Stack:** FastAPI、Python pytest、React 19、TypeScript、HeroUI、Node 内置测试器、Vite。

## Global Constraints

- 不重试 `/api/chat/stream`，不产生第二次模型调用或重复历史。
- 显式 `error` 事件必须直接报错，取消生成不得进入恢复。
- 只能使用现有资产查看接口；不修改 S3、MySQL 和服务器配置。
- 只在本地运行测试与构建；不得部署、推送或执行服务器命令。

---

### Task 1: 在服务端转发流式心跳

**Files:**
- Modify: `app/agent.py:791-850,1136-1160`
- Modify: `tests/test_knowledge_base.py:1053-1093`

**Interfaces:**
- Produces: `SdkAgentRunner.stream()` 产生 `{"event": "heartbeat", "data": {}}`，间隔至少 15 秒。
- Consumes: `KnowledgeAgent.answer_stream()` 现有公共事件白名单。

- [ ] **Step 1: 写失败的测试**

在现有 `test_streamed_answer_records_one_complete_turn_and_forwards_public_events` 的 `FakeSdkRunner.stream()` 中，在 `trace` 前加入：

```python
yield {"event": "heartbeat", "data": {}}
```

并将断言改为：

```python
assert [event["event"] for event in events] == ["status", "heartbeat", "trace", "delta", "done"]
```

- [ ] **Step 2: 验证测试失败**

Run: `../../.venv/bin/pytest -q tests/test_knowledge_base.py -k streamed_answer_records`

Expected: FAIL，实际事件列表缺少 `heartbeat`。

- [ ] **Step 3: 实现最小转发与保活**

在 `SdkAgentRunner.stream()` 的 `while True` 前记录 `last_heartbeat = time.monotonic()`，每次 `select.select()` 等待后、继续下一轮前加入：

```python
if time.monotonic() - last_heartbeat >= 15:
    last_heartbeat = time.monotonic()
    yield {"event": "heartbeat", "data": {}}
```

在 `KnowledgeAgent.answer_stream()` 的公共事件集合中加入 `"heartbeat"`：

```python
elif isinstance(event_name, str) and event_name in {"trace", "delta", "status", "heartbeat"} and isinstance(data, dict):
    yield {"event": event_name, "data": data}
```

- [ ] **Step 4: 验证测试通过**

Run: `../../.venv/bin/pytest -q tests/test_knowledge_base.py -k streamed_answer_records`

Expected: PASS。

### Task 2: 轮询已保存会话恢复意外 EOF

**Files:**
- Modify: `frontend/src/App.tsx:263-331,1426-1436`
- Modify: `frontend/tests/streaming-terminal-recovery.test.mjs`

**Interfaces:**
- Produces: `waitForPersistedCompleteTurn(conversationId, initialMessageCount): Promise<Conversation | null>`。
- Consumes: `request<Conversation>()` 与 `hasPersistedCompleteTurn()`。

- [ ] **Step 1: 写失败的测试**

在 `streaming-terminal-recovery.test.mjs` 中增加：

```js
assert.match(source, /const STREAM_RECOVERY_TIMEOUT_MS = 95_000/);
assert.match(source, /async function waitForPersistedCompleteTurn\(/);
assert.match(source, /while \(Date\.now\(\) < deadline\)/);
assert.match(source, /await new Promise<void>\(\(resolve\) => window\.setTimeout\(resolve, STREAM_RECOVERY_POLL_INTERVAL_MS\)\)/);
assert.match(source, /正在同步已保存的回答/);
```

- [ ] **Step 2: 验证测试失败**

Run: `node --test frontend/tests/streaming-terminal-recovery.test.mjs`

Expected: FAIL，尚未定义恢复等待常量和函数。

- [ ] **Step 3: 实现最小轮询恢复**

在 `hasPersistedCompleteTurn()` 后定义 95 秒期限、1 秒间隔和函数：

```ts
async function waitForPersistedCompleteTurn(conversationId: string, initialMessageCount: number): Promise<Conversation | null> {
  const deadline = Date.now() + STREAM_RECOVERY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const persisted = await request<Conversation>(`/api/conversations/${conversationId}`);
    if (hasPersistedCompleteTurn(persisted, initialMessageCount)) return persisted;
    await new Promise<void>((resolve) => window.setTimeout(resolve, STREAM_RECOVERY_POLL_INTERVAL_MS));
  }
  return null;
}
```

将单次 `request<Conversation>()` 替换为先 `setNotice("流连接中断，正在同步已保存的回答…")` 后调用此函数；空结果仍抛出现有错误。

- [ ] **Step 4: 验证测试通过**

Run: `node --test frontend/tests/streaming-terminal-recovery.test.mjs`

Expected: PASS。

### Task 3: 以站内弹窗预览资料

**Files:**
- Modify: `frontend/src/App.tsx:620-820,1026-1068,2898-2969`
- Modify: `frontend/src/App.css:450-505`
- Modify: `frontend/tests/compact-source-actions.test.mjs` (create if absent)

**Interfaces:**
- Produces: `FilePreview` state and `openFilePreview(asset)` callback owned by `App`。
- Consumes: 现有 `/api/assets/{asset_id}/view`、`Modal` 和 `getApiBase()`。

- [ ] **Step 1: 写失败的测试**

创建静态前端测试，要求源码有：

```js
assert.match(source, /const \[previewAsset, setPreviewAsset\] = useState<Asset \| null>\(null\)/);
assert.match(source, /<Modal state=\{previewDialog\}>/);
assert.match(source, /aria-label=\{`预览文件 \$\{source\.name\}`\}/);
assert.doesNotMatch(source, /href=\{previewUrl\}[\s\S]{0,160}target="_blank"/);
```

- [ ] **Step 2: 验证测试失败**

Run: `node --test frontend/tests/compact-source-actions.test.mjs`

Expected: FAIL，未定义预览状态与对话框。

- [ ] **Step 3: 实现最小弹窗**

将 `MessageView` 的眼睛链接和用户附件链接改为 `button`，通过新增 `onPreview(asset)` 调用父组件。App 使用 `previewDialog = useOverlayState()`、`previewAsset` 和 `openFilePreview()` 管理状态；在根部渲染：

```tsx
<Modal state={previewDialog}>
  <Modal.Backdrop><Modal.Container size="lg"><Modal.Dialog>
    <Modal.Header><Modal.Heading>{previewAsset.name}</Modal.Heading><Modal.CloseTrigger /></Modal.Header>
    <Modal.Body>{previewAsset.kind === "image" ? <img ... /> : <iframe title={`预览文件 ${previewAsset.name}`} src={`${getApiBase()}/api/assets/${previewAsset.asset_id}/view`} />}</Modal.Body>
  </Modal.Dialog></Modal.Container></Modal.Backdrop>
</Modal>
```

为按钮和嵌入内容添加局部深色主题 CSS，预览区以视口高度为限并允许内容滚动。

- [ ] **Step 4: 验证测试通过**

Run: `node --test frontend/tests/compact-source-actions.test.mjs`

Expected: PASS。

### Task 4: 集成验证和本地构建

**Files:**
- Modify: `frontend/dist/index.html` 与 `frontend/dist/assets/*`（Vite 生成）

- [ ] **Step 1: 运行前端完整回归**

Run: `node --test frontend/tests/*.test.mjs`

Expected: PASS。

- [ ] **Step 2: 类型检查与构建**

Run: `(cd frontend && npm ci && npm run build)`

Expected: TypeScript 与 Vite 均成功，预构建资源更新。

- [ ] **Step 3: 运行后端回归**

Run: `../../.venv/bin/pytest -q`

Expected: PASS。

- [ ] **Step 4: 本地浏览器验证**

Run: `uvicorn app.main:app --host 127.0.0.1 --port 8002`，然后在隔离浏览器中打开 `http://127.0.0.1:8002`。

Expected: 一次真实问答最终显示回答；点击来源眼睛图标后弹窗显示文件且没有新增浏览器标签。

- [ ] **Step 5: 提交本地修复**

```bash
git add app/agent.py tests/test_knowledge_base.py frontend/src/App.tsx frontend/src/App.css frontend/tests frontend/dist docs/superpowers
git commit -m "fix: recover interrupted streams and preview files in dialog"
```
