import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("用户提问使用 HeroUI 头像组件", async () => {
  const source = await readFile(sourcePath, "utf8");
  const userMessage = source.match(
    /<ChatMessage\.User[\s\S]*?<\/ChatMessage\.User>/,
  )?.[0];

  assert.ok(userMessage, "应存在用户消息渲染分支");
  assert.match(userMessage, /<ChatMessage\.Avatar/);
  assert.match(userMessage, /className="user-avatar"/);
  assert.match(userMessage, /src=\{userAvatarUrl\}/);
});
