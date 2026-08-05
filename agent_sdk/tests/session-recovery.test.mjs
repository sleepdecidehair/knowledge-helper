import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildRecoveryPrompt,
  isMissingSessionError,
  normalizeRecoveryHistory,
  resolveCompactRun,
  runQuestionWithSessionRecovery,
  withoutSessionResume,
} from "../dist/session-recovery.js";

const missingSessionId = "49eeb2dc-728f-44dd-84a9-1d446dd6f985";

test("只识别当前 resume ID 对应的会话缺失错误", () => {
  assert.equal(
    isMissingSessionError(
      `No conversation found with session ID: ${missingSessionId}`,
      missingSessionId,
    ),
    true,
  );
  assert.equal(
    isMissingSessionError("DeepSeek endpoint timed out", missingSessionId),
    false,
  );
  assert.equal(
    isMissingSessionError(
      "No conversation found with session ID: another-session",
      missingSessionId,
    ),
    false,
  );
});

test("恢复时仅移除 resume 与 forkSession，保留其他 SDK 选项", () => {
  const recovered = withoutSessionResume({
    cwd: "/tmp/session",
    model: "deepseek-v4-flash",
    resume: missingSessionId,
    forkSession: true,
    maxTurns: 6,
  });

  assert.deepEqual(recovered, {
    cwd: "/tmp/session",
    model: "deepseek-v4-flash",
    maxTurns: 6,
  });
});

test("问题恢复只重试一次，并返回新会话结果", async () => {
  const calls = [];
  let recoveryCount = 0;
  const options = {
    cwd: "/tmp/session",
    model: "deepseek-v4-flash",
    resume: missingSessionId,
    forkSession: true,
    maxTurns: 6,
  };
  const result = await runQuestionWithSessionRecovery(
    "测试问题",
    buildRecoveryPrompt("测试问题", [
      { role: "user", content: "上一轮讨论的是北极星项目。" },
      { role: "assistant", content: "已根据知识库说明北极星项目。" },
    ]),
    options,
    missingSessionId,
    async (prompt, activeOptions) => {
      calls.push({ prompt, options: activeOptions });
      if (calls.length === 1) {
        return { streamError: `No conversation found with session ID: ${missingSessionId}` };
      }
      return { terminal: { subtype: "success", session_id: "replacement-session" } };
    },
    () => { recoveryCount += 1; },
  );

  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.resume, missingSessionId);
  assert.equal(calls[0].options.forkSession, true);
  assert.equal(calls[0].prompt, "测试问题");
  assert.equal("resume" in calls[1].options, false);
  assert.equal("forkSession" in calls[1].options, false);
  assert.match(calls[1].prompt, /北极星项目/);
  assert.match(calls[1].prompt, /测试问题/);
  assert.equal(result.run.terminal.session_id, "replacement-session");
  assert.equal(result.recovered, true);
  assert.equal(recoveryCount, 1);
});

test("非会话缺失错误不会触发重试", async () => {
  let callCount = 0;
  const result = await runQuestionWithSessionRecovery(
    "测试问题",
    "不应使用的恢复提示",
    { resume: missingSessionId, forkSession: true },
    missingSessionId,
    async () => {
      callCount += 1;
      return { streamError: "DeepSeek endpoint timed out" };
    },
  );

  assert.equal(callCount, 1);
  assert.equal(result.recovered, false);
  assert.equal(result.run.streamError, "DeepSeek endpoint timed out");
});

test("恢复历史只保留有界的最近用户与助手消息", () => {
  const raw = [
    { role: "system", content: "不可注入的系统消息" },
    ...Array.from({ length: 30 }, (_, index) => ({
      role: index % 2 === 0 ? "user" : "assistant",
      content: `history-${index}`,
    })),
    { role: "assistant", content: "x".repeat(5000) },
  ];

  const normalized = normalizeRecoveryHistory(raw);

  assert.equal(normalized.length, 24);
  assert.equal(normalized.some((item) => item.content.includes("不可注入")), false);
  assert.equal(normalized.at(-1).content.length, 4000);
  assert.equal(normalized.at(-1).role, "assistant");
  assert.equal(normalized.some((item) => item.content === "history-29"), true);

  const aggregateLimited = normalizeRecoveryHistory(
    Array.from({ length: 24 }, (_, index) => ({
      role: index % 2 === 0 ? "user" : "assistant",
      content: "y".repeat(4000),
    })),
  );
  assert.equal(
    aggregateLimited.reduce((total, item) => total + item.content.length, 0) <= 24000,
    true,
  );
});

test("compact 已确认 session 缺失时，首次新查询直接携带恢复历史", async () => {
  const recoveryPrompt = buildRecoveryPrompt("继续说明", [
    { role: "user", content: "项目代号是北极星。" },
    { role: "assistant", content: "已记录该项目代号。" },
  ]);
  const compact = resolveCompactRun(
    { streamError: `No conversation found with session ID: ${missingSessionId}` },
    { resume: missingSessionId, forkSession: true },
    missingSessionId,
  );
  assert.equal(compact.kind, "recovered");
  const calls = [];

  const result = await runQuestionWithSessionRecovery(
    "继续说明",
    recoveryPrompt,
    compact.options,
    undefined,
    async (prompt, options) => {
      calls.push({ prompt, options });
      return { terminal: { subtype: "success", session_id: "replacement-session" } };
    },
    () => assert.fail("compact 路径不应重复标记恢复"),
    true,
  );

  assert.equal(calls.length, 1);
  assert.match(calls[0].prompt, /北极星/);
  assert.equal("resume" in calls[0].options, false);
  assert.equal(result.recovered, true);
});

test("compact 将普通错误原样返回，只对缺失 session 重建", () => {
  const options = { resume: missingSessionId, forkSession: true, maxTurns: 6 };

  assert.deepEqual(
    resolveCompactRun({ streamError: "DeepSeek endpoint timed out" }, options, missingSessionId),
    { kind: "failed", error: "DeepSeek endpoint timed out", options },
  );
  const recovered = resolveCompactRun(
    { streamError: `No conversation found with session ID: ${missingSessionId}` },
    options,
    missingSessionId,
  );
  assert.equal(recovered.kind, "recovered");
  assert.equal("resume" in recovered.options, false);
  assert.equal("forkSession" in recovered.options, false);
});

test("runner 使用可测试的会话恢复编排", () => {
  const source = readFileSync(new URL("../src/runner.ts", import.meta.url), "utf8");

  assert.match(source, /resolveCompactRun\(compactRun, options, request\.session_id\)/);
  assert.match(source, /runQuestionWithSessionRecovery\(/);
  assert.match(source, /markMissingSessionRecovered/);
});
