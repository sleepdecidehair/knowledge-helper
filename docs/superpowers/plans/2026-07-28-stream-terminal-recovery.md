# 流式回答终止事件恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 SSE 自然结束但末尾 `done` 事件丢失时，从已落库的同一会话恢复本轮回答，而不把成功回答误报为流式失败。

**Architecture:** 保持后端 SSE 协议不变。前端在发起请求前记住会话原始消息数；只有在流结束而未收到 `done` 时，读取同一会话，并确认新增了完整的一问一答后才恢复成功状态。显式 `error` 事件和未落库的终止继续报错，避免掩盖失败或重复调用模型。

**Tech Stack:** React 19、TypeScript、Vite、Node.js 内置测试运行器、Python pytest。

## Global Constraints

- 仅修改本地代码、测试与预构建前端产物；不得部署服务器。
- 不重试 `/api/chat/stream`，避免重复模型调用、重复历史记录或重复写入。
- 服务端 `error` 事件必须继续直接进入既有错误路径。
- 恢复条件固定为：同一会话比开始前至少多两条消息，最后一条是含非空正文的助手消息。

---

### Task 1: 终止事件缺失时恢复已保存会话

**Files:**
- Modify: `frontend/src/App.tsx:273-311,1253-1410`
- Create: `frontend/tests/streaming-terminal-recovery.test.mjs`

**Interfaces:**
- Consumes: `request<T>(url, init?)`, `Conversation.messages`, `readServerEvents(response, onEvent)`。
- Produces: `hasPersistedCompleteTurn(conversation: Conversation, initialMessageCount: number): boolean`，供 `sendQuestion()` 的自然 EOF 恢复分支使用。

- [ ] **Step 1: 写失败的前端回归测试**

在 `frontend/tests/streaming-terminal-recovery.test.mjs` 新建静态回归测试，先要求恢复辅助函数、会话读取和严格判定条件存在：

```js
import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("缺少 done 时只恢复已保存的一问一答", async () => {
  const source = await readFile(sourcePath, "utf8");
  assert.match(source, /function hasPersistedCompleteTurn\(conversation: Conversation, initialMessageCount: number\)/);
  assert.match(source, /conversation\.messages\.length < initialMessageCount \+ 2/);
  assert.match(source, /lastMessage\.role !== "assistant" \|\| !lastMessage\.content\.trim\(\)/);
  assert.match(source, /if \(!completed \|\| !activeConversationId\)[\s\S]*?request<Conversation>\(`\/api\/conversations\/\$\{current\.id\}`\)/);
});

test("显式 SSE error 仍直接报错，不进入恢复分支", async () => {
  const source = await readFile(sourcePath, "utf8");
  assert.match(source, /if \(event === "error"\)[\s\S]*?throw new Error/);
});
```

- [ ] **Step 2: 验证测试确实失败**

Run: `node --test tests/streaming-terminal-recovery.test.mjs`

Expected: FAIL，提示 `streaming-terminal-recovery.test.mjs` 不存在或 `hasPersistedCompleteTurn` 不存在。

- [ ] **Step 3: 实现最小恢复判定与读取逻辑**

在 `frontend/src/App.tsx` 的 `readServerEvents()` 后加入：

```ts
function hasPersistedCompleteTurn(
  conversation: Conversation,
  initialMessageCount: number,
): boolean {
  if (conversation.messages.length < initialMessageCount + 2) return false;
  const lastMessage = conversation.messages.at(-1);
  return Boolean(
    lastMessage &&
      lastMessage.role === "assistant" &&
      lastMessage.content.trim(),
  );
}
```

在 `sendQuestion()` 中、`await readServerEvents(...)` 后保留现有 `done` 处理；将现有的终止判断替换为：

```ts
if (!completed || !activeConversationId) {
  const persisted = await request<Conversation>(`/api/conversations/${current.id}`);
  if (!hasPersistedCompleteTurn(persisted, current.messages.length)) {
    throw new Error("流式回答未正常结束。");
  }
  completed = true;
  activeConversationId = persisted.id;
}
```

不要修改 `event === "error"` 分支；该分支会先抛出异常，因此不会进入自然 EOF 的恢复逻辑。

- [ ] **Step 4: 验证回归测试通过**

Run: `node --test tests/streaming-terminal-recovery.test.mjs`

Expected: PASS，2 个测试通过。

- [ ] **Step 5: 提交任务**

```bash
git add frontend/src/App.tsx frontend/tests/streaming-terminal-recovery.test.mjs
git commit -m "fix: recover persisted turn after missing stream done"
```

### Task 2: 本地前端产物与完整回归验证

**Files:**
- Modify: `frontend/dist/index.html`
- Modify: `frontend/dist/assets/*`（由 Vite 生成的带哈希产物）
- Verify: `frontend/tests/*.test.mjs`, `tests/*.py`

**Interfaces:**
- Consumes: Task 1 的 `hasPersistedCompleteTurn()` 和 `sendQuestion()` 恢复逻辑。
- Produces: 与 TypeScript 源码一致的 `frontend/dist/`，供现有 Dockerfile 前端镜像复制。

- [ ] **Step 1: 运行全部前端静态回归测试**

Run: `node --test tests/*.test.mjs`

Expected: PASS，包含新增的终止恢复测试，且无失败。

- [ ] **Step 2: 构建预构建前端产物**

Run: `npm run build`

Expected: TypeScript 检查和 Vite 构建均成功；`frontend/dist/` 更新为当前源码。

- [ ] **Step 3: 运行后端全量回归测试**

Run: `../.venv/bin/pytest -q`

Expected: PASS，当前完整套件全部通过。

- [ ] **Step 4: 本机确认服务未部署**

Run: `git status --short && git log -1 --oneline`

Expected: 仅显示本地构建产物的预期变更（若有）；不执行 `git push`、SSH、Docker Compose 或服务器命令。

- [ ] **Step 5: 提交构建产物**

```bash
git add frontend/dist
git commit -m "build: refresh frontend stream recovery assets"
```

## Self-Review

- 设计中的自然 EOF 恢复、显式错误保留、无重试和本地不部署分别由 Task 1 与 Task 2 覆盖。
- 计划没有待补内容、占位符或未定义接口；恢复函数名称和参数在测试、实现和构建任务中一致。
- 本计划只涉及前端恢复与构建，不触及后端 SSE 协议、MySQL 或服务器配置。
