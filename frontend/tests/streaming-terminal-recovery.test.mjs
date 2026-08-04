import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const streamEventsPath = new URL("../src/streamEvents.ts", import.meta.url);

async function loadStreamEventsModule() {
  const source = await readFile(streamEventsPath, "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: "streamEvents.ts",
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "SSE packet 模块应能由 TypeScript 转译");
  return import(
    `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`
  );
}

test("缺少 done 时轮询恢复已保存的一问一答", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(
    source,
    /function hasPersistedCompleteTurn\(\s*conversation: Conversation,\s*initialMessageCount: number,\s*\)/,
  );
  assert.match(source, /conversation\.messages\.length < initialMessageCount \+ 2/);
  assert.match(
    source,
    /lastMessage\.role !== "assistant"\s*\|\|\s*!lastMessage\.content\.trim\(\)/,
  );
  assert.match(
    source,
    /const STREAM_RECOVERY_TIMEOUT_MS = 95_000/,
  );
  assert.match(source, /async function waitForPersistedCompleteTurn\(/);
  assert.match(source, /while \(Date\.now\(\) < deadline\)/);
  assert.match(
    source,
    /await new Promise<void>\(\(resolve\) => window\.setTimeout\(resolve, STREAM_RECOVERY_POLL_INTERVAL_MS\)\)/,
  );
  assert.match(source, /正在同步已保存的回答/);
  assert.match(
    source,
    /if \(!completed \|\| !activeConversationId\)[\s\S]*?waitForPersistedCompleteTurn\(\s*current\.id,\s*current\.messages\.length,\s*requestController\.signal,\s*\)/,
  );
});

test("显式 SSE error 仍直接报错，不进入恢复分支", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /if \(event === "error"\)[\s\S]*?throw new Error/);
});

test("SSE packet 只忽略 JSON 解析错误且业务回调异常会传播", async () => {
  const { dispatchServerEventPacket } = await loadStreamEventsModule();
  const events = [];

  assert.doesNotThrow(() =>
    dispatchServerEventPacket("event: delta\ndata: {invalid", (event, data) => {
      events.push([event, data]);
    }),
  );
  assert.deepEqual(events, []);

  dispatchServerEventPacket(
    'event: delta\ndata: {"text":"hello"}',
    (event, data) => events.push([event, data]),
  );
  assert.deepEqual(events, [["delta", { text: "hello" }]]);

  assert.throws(
    () => dispatchServerEventPacket(
      'event: error\ndata: {"message":"stream failed"}',
      () => {
        throw new Error("stream failed");
      },
    ),
    /stream failed/,
  );
});
