import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("界面提供本地评测、回答反馈与重新检索入口", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /\/api\/evaluation-cases/);
  assert.match(source, /\/api\/evaluations\/run/);
  assert.match(source, /\/feedback/);
  assert.match(source, /重新检索/);
  assert.match(source, /评测题集/);
});
