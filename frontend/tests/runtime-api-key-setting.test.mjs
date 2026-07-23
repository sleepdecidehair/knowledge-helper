import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("设置页允许仅提交新的 DeepSeek Key，且不会回显已保存密钥", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /const \[apiKeyDraft, setApiKeyDraft\] = useState\(""\);/);
  assert.match(source, /deepseek_api_key: apiKeyDraft\.trim\(\) \|\| undefined/);
  assert.match(source, /type="password"/);
  assert.match(source, /placeholder="粘贴新的 DeepSeek API Key"/);
  assert.doesNotMatch(source, /value=\{runtime\.deepseek_api_key\}/);
});
