import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/runner.ts", import.meta.url), "utf8");

test("写入授权独立于旧智能体配置，纯写入不强制检索", () => {
  assert.doesNotMatch(source, /request\.agent_profile\?\.allow_write/);
  assert.match(source, /const isWriteRequest = Boolean\(request\.write_grant\)/);
  assert.match(source, /if \(isWriteRequest\) \{[\s\S]{0,240}saveKnowledgeNote/);
  assert.match(source, /if \(isWriteRequest && !knowledgeWrite\)/);
  assert.match(source, /if \(!isWriteRequest && !didSearch\)/);
});
