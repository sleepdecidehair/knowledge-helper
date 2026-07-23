import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("流式生成时发送键切换为 HeroUI 原生停止键", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /useRef<AbortController \| null>/);
  assert.match(source, /function stopGeneration\(\)[\s\S]*?\.abort\(\)/);
  assert.match(source, /status=\{busy \? "streaming" : "ready"\}/);
  assert.match(source, /onStop=\{stopGeneration\}/);
  assert.match(source, /isDisabled=\{busy \? false : !question\.trim\(\) && !pendingChatFiles\.length\}/);
});

test("执行过程对用户展示工具调用，不泄露 MCP 协议名", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /mcp_call:\s*"工具调用"/);
  assert.match(source, /event\.title\.replace\("工具调用：", ""\)/);
  assert.doesNotMatch(source, /MCP 调用/);
});

test("有消息的会话不再渲染冗余的大标题卡片", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.doesNotMatch(source, /<Card className="conversation-header">/);
});
