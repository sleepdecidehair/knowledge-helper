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
  const sendQuestion = functionBody(
    source,
    "sendQuestion",
    "async function uploadFilesToProject",
  );

  const transition = sendQuestion.indexOf("setConversation({");
  const refresh = sendQuestion.indexOf('() => refresh(requestProjectId, "")');
  const stream = sendQuestion.indexOf('fetch(getApiBase() + "/api/chat/stream"');

  assert.ok(transition >= 0, "首问应写入用户消息和助手占位");
  assert.ok(refresh >= 0, "首问后应刷新侧栏历史");
  assert.ok(stream >= 0, "应继续启动原有流式请求");
  assert.ok(transition < refresh, "正式会话应先于历史刷新显示");
  assert.ok(refresh < stream, "历史刷新仍应先于流式请求");
});

test("新建会话的首问不等待服务端创建便进入正式聊天页", async () => {
  const source = await readFile(sourcePath, "utf8");
  const sendQuestion = functionBody(
    source,
    "sendQuestion",
    "async function uploadFilesToProject",
  );

  const transition = sendQuestion.indexOf("setConversation({");
  const createRequest = sendQuestion.indexOf("const createdStep = await continueProjectWorkflow");

  assert.ok(transition >= 0, "首问应先建立带消息的本地会话占位");
  assert.ok(createRequest >= 0, "首问仍应创建服务端会话");
  assert.ok(
    transition < createRequest,
    "正式聊天页的状态切换不得等待服务端创建会话",
  );
});

test("欢迎页输入框相邻展示，正式会话外框固定且内容内部滚动", async () => {
  const css = await readFile(stylePath, "utf8");

  assert.match(
    css,
    /\.chat-workspace\s*\{[\s\S]*?height:\s*calc\(100dvh - 132px\)/,
  );
  assert.match(
    css,
    /\.empty-chat-workspace\s*\{[\s\S]*?grid-template-rows:\s*minmax\(0, 1fr\) auto auto minmax\(0, 1fr\)/,
  );
  assert.match(
    css,
    /\.conversation-scroll\s*\{[\s\S]*?overflow-y:\s*auto !important/,
  );
  assert.match(
    css,
    /\.composer-dock\s*\{[\s\S]*?position:\s*relative/,
  );
});

test("正式会话首帧占满固定区域，并且只滚动会话框内部", async () => {
  const [source, css] = await Promise.all([
    readFile(sourcePath, "utf8"),
    readFile(stylePath, "utf8"),
  ]);
  const scrollHelperStart = source.indexOf("const scrollPageToBottom");
  const scrollHelperEnd = source.indexOf("const traceLabel", scrollHelperStart);
  const scrollHelper = source.slice(scrollHelperStart, scrollHelperEnd);

  assert.match(scrollHelper, /querySelector<HTMLElement>\("\.conversation-scroll"\)/);
  assert.match(scrollHelper, /container\?\.scrollTo\(\{ top: container\.scrollHeight, behavior \}\)/);
  assert.doesNotMatch(scrollHelper, /window\.scrollTo/);
  assert.match(
    css,
    /\.workspace-content\s*\{[\s\S]*?width:\s*100%[\s\S]*?min-width:\s*0/,
  );
  assert.match(
    css,
    /\.conversation-scroll\s*\{[\s\S]*?width:\s*100% !important[\s\S]*?max-width:\s*none !important[\s\S]*?box-sizing:\s*border-box/,
  );
});
