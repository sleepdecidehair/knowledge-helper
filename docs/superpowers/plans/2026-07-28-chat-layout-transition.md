# 新对话切换与固定会话视图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 首次提交问题时立即从欢迎页切换到固定尺寸的正式会话视图，并让会话内容在内部滚动。

**Architecture:** 保持 `conversation` 为唯一会话状态。首问创建会话并更新标题后不再先等待历史列表刷新，而是在写入用户消息和助手占位后显示正式会话；随后刷新侧栏历史并开始既有 SSE 请求。聊天工作区使用 CSS Grid：欢迎页将标题和输入框居中相邻放置，正式页将内容区和输入区放进稳定的视窗高度内。

**Tech Stack:** React 19、TypeScript、CSS Grid、Node.js 内置测试运行器。

## Global Constraints

- 只修改本地前端源代码与测试；不改后端 API、会话数据格式或 SSE 协议。
- 首问仍只创建一条会话；不重试模型调用，也不改变历史记录逻辑。
- 正式会话外框稳定，消息只能在 `conversation-scroll` 内部垂直滚动。
- 欢迎页输入框紧邻标题；正式会话输入框在固定外框底部。
- 不推送、不部署；构建成功后仅合并到本地 `dev`。

---

### Task 1: 首问即时切换为正式会话

**Files:**
- Modify: `frontend/src/App.tsx:1281-1344`
- Create: `frontend/tests/chat-layout-transition.test.mjs`

**Interfaces:**
- Consumes: `conversation`, `setConversation`, `refresh(projectId, query)`, `sendQuestion()`。
- Produces: 首次 `setConversation(...)` 发生在历史列表刷新和 `/api/chat/stream` 请求之前，令 `hasMessages` 立即渲染正式会话。

- [ ] **Step 1: 写失败的前端回归测试**

创建 `frontend/tests/chat-layout-transition.test.mjs`：

```js
import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const stylePath = new URL("../src/App.css", import.meta.url);

const functionBody = (source, name, nextMarker) => {
  const start = source.indexOf(`function ${name}`);
  const end = source.indexOf(nextMarker, start);
  return source.slice(start, end);
};

test("首问先切换到正式会话，再刷新历史和启动流式请求", async () => {
  const source = await readFile(sourcePath, "utf8");
  const sendQuestion = functionBody(source, "sendQuestion", "async function uploadFilesToProject");

  const transition = sendQuestion.indexOf("setConversation({");
  const refresh = sendQuestion.indexOf('await refresh(projectId, "")');
  const stream = sendQuestion.indexOf('fetch(getApiBase() + "/api/chat/stream"');

  assert.ok(transition >= 0, "首问应写入用户消息和助手占位");
  assert.ok(refresh >= 0, "首问后应刷新侧栏历史");
  assert.ok(stream >= 0, "应继续启动原有流式请求");
  assert.ok(transition < refresh, "正式会话应先于历史刷新显示");
  assert.ok(refresh < stream, "历史刷新仍应先于流式请求");
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test tests/chat-layout-transition.test.mjs`

Expected: FAIL，提示“正式会话应先于历史刷新显示”。

- [ ] **Step 3: 实现最小首问切换调整**

在 `sendQuestion()` 中将创建首会话分支里的：

```ts
setSearch("");
await refresh(projectId, "");
```

改为，并在 `let current = conversation;` 前定义 `let shouldRefreshConversationList = false;`：

```ts
setSearch("");
shouldRefreshConversationList = true;
```

在 `setConversation({...})`、`setQuestion("")` 后，开始 `/api/chat/stream` 前加入：

```ts
if (shouldRefreshConversationList) {
  await refresh(projectId, "");
}
```

将 `shouldRefreshConversationList` 定义为 `let` 并在首问分支设为 `true`，默认 `false`，以便已有会话不会刷新历史列表。

- [ ] **Step 4: 运行回归测试确认通过**

Run: `node --test tests/chat-layout-transition.test.mjs`

Expected: PASS，1 个测试通过。

- [ ] **Step 5: 提交任务**

```bash
git add frontend/src/App.tsx frontend/tests/chat-layout-transition.test.mjs
git commit -m "fix: enter chat view before first stream"
```

### Task 2: 固定会话外框并在内部滚动

**Files:**
- Modify: `frontend/src/App.css:169-205`
- Modify: `frontend/tests/chat-layout-transition.test.mjs`

**Interfaces:**
- Consumes: `.chat-workspace.has-messages`、`.empty-chat-workspace`、`.conversation-scroll`、`.composer-dock`。
- Produces: 以 `100dvh` 为基准的稳定聊天工作区，正式消息区滚动、输入区保持在外框底部；欢迎页标题和输入区相邻居中。

- [ ] **Step 1: 补充失败的 CSS 回归测试**

追加到 `frontend/tests/chat-layout-transition.test.mjs`：

```js
test("欢迎页输入框相邻展示，正式会话外框固定且内容内部滚动", async () => {
  const css = await readFile(stylePath, "utf8");

  assert.match(css, /\.chat-workspace\s*\{[\s\S]*?height:\s*calc\(100dvh - 132px\)/);
  assert.match(css, /\.empty-chat-workspace\s*\{[\s\S]*?grid-template-rows:\s*minmax\(0, 1fr\) auto auto minmax\(0, 1fr\)/);
  assert.match(css, /\.conversation-scroll\s*\{[\s\S]*?overflow-y:\s*auto !important/);
  assert.match(css, /\.composer-dock\s*\{[\s\S]*?position:\s*relative/);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test tests/chat-layout-transition.test.mjs`

Expected: FAIL，提示缺少固定高度、内部滚动或相对定位输入区。

- [ ] **Step 3: 实现稳定的 CSS Grid 布局**

将 `frontend/src/App.css` 中聊天布局替换为以下关键约束：

```css
.chat-workspace {
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
  height: calc(100dvh - 132px);
  min-height: 0;
  overflow: hidden;
  padding-bottom: 0;
}
.empty-chat-workspace {
  grid-template-rows: minmax(0, 1fr) auto auto minmax(0, 1fr);
}
.empty-chat-landing { grid-row: 2; margin: 0 auto 18px; }
.empty-chat-workspace .composer-dock { grid-row: 3; }
.conversation-scroll {
  height: 100%;
  min-height: 0;
  overflow-y: auto !important;
}
.composer-dock {
  position: relative;
  bottom: auto;
  left: auto;
  width: min(820px, 100%);
  margin: 12px auto;
  transform: none;
}
```

保留现有视觉样式（背景渐变、输入框网格与移动端宽度），但将移动端 `.composer-dock` 的 `left` 和 `width: calc(100vw - 32px)` 覆盖改为该稳定容器的 `width: 100%`。

- [ ] **Step 4: 运行 CSS 回归测试确认通过**

Run: `node --test tests/chat-layout-transition.test.mjs`

Expected: PASS，2 个测试通过。

- [ ] **Step 5: 提交任务**

```bash
git add frontend/src/App.css frontend/tests/chat-layout-transition.test.mjs
git commit -m "fix: stabilize chat workspace layout"
```

### Task 3: 完整验证与本地合并准备

**Files:**
- Verify: `frontend/tests/*.test.mjs`
- Verify: `tests/*.py`

**Interfaces:**
- Consumes: Task 1 的首问状态顺序和 Task 2 的布局约束。
- Produces: 可复核的本地测试结果；不生成或部署前端产物，除非现有 Pro 组件授权问题已单独解决。

- [ ] **Step 1: 运行全部前端静态测试**

Run: `node --test tests/*.test.mjs`

Expected: 新增两项测试通过；已知的输入框高度与旧流请求字面量断言失败单独报告，不在本任务修改。

- [ ] **Step 2: 运行后端全量测试**

Run: `../../.venv/bin/pytest -q`

Expected: PASS，51 个测试通过。

- [ ] **Step 3: 尝试前端构建并记录结果**

Run: `npm run build`

Expected: 当前缺失 HeroUI Pro 授权组件时，明确报告模块解析失败；本任务不通过替换付费组件绕过授权。

- [ ] **Step 4: 检查本地分支状态**

Run: `git status --short && git log -3 --oneline`

Expected: 仅包含本地提交；不执行 `git push`、SSH、Docker 或服务器命令。

## 自检

- 目标的三项交互分别由 Task 1（首问切换）、Task 2（欢迎页与固定外框）覆盖。
- 计划没有后端、模型、知识库或部署变更。
- 测试在修改前均有明确的预期失败条件；CSS 属性、函数名和命令与当前仓库一致。
