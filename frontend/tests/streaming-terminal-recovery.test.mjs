import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("缺少 done 时只恢复已保存的一问一答", async () => {
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
    /if \(!completed \|\| !activeConversationId\)[\s\S]*?request<Conversation>\(\s*`\/api\/conversations\/\$\{current\.id\}`,\s*\)/,
  );
});

test("显式 SSE error 仍直接报错，不进入恢复分支", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /if \(event === "error"\)[\s\S]*?throw new Error/);
});
