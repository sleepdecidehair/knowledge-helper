import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

const functionBody = (source, name, nextMarker) => {
  const start = source.indexOf(`function ${name}`);
  const end = source.indexOf(nextMarker, start);
  return source.slice(start, end);
};

test("新建对话只清空页面，不创建历史记录", async () => {
  const source = await readFile(sourcePath, "utf8");
  const newConversation = functionBody(source, "newConversation", "useEffect(() => {");

  assert.ok(newConversation, "应存在新建对话处理函数");
  assert.doesNotMatch(newConversation, /\/api\/conversations/);
  assert.match(newConversation, /setConversation\(null\)/);
});

test("首次发送问题时才创建会话", async () => {
  const source = await readFile(sourcePath, "utf8");
  const sendQuestion = functionBody(source, "sendQuestion", "async function uploadFilesToProject");

  assert.match(sendQuestion, /let current = conversation;[\s\S]*?if \(!current\)[\s\S]*?method:\s*"POST"/);
  assert.match(sendQuestion, /\/api\/conversations/);
});

test("首条问题发送后立即写入标题并刷新历史列表", async () => {
  const source = await readFile(sourcePath, "utf8");
  const sendQuestion = functionBody(source, "sendQuestion", "async function uploadFilesToProject");

  assert.match(sendQuestion, /let current = conversation/);
  assert.match(sendQuestion, /method:\s*"PATCH"/);
  assert.match(sendQuestion, /await refresh\(projectId,\s*""\)/);
  assert.ok(
    sendQuestion.indexOf('await refresh(projectId, "")') <
      sendQuestion.indexOf('fetch(getApiBase() + "/api/chat/stream"'),
    "历史列表应在启动流式回答前刷新",
  );
});
