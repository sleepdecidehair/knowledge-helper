import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const packagePath = new URL("../package.json", import.meta.url);

test("前端锁定安全的 tar 覆盖版本", async () => {
  const packageJson = JSON.parse(await readFile(packagePath, "utf8"));

  assert.equal(packageJson.overrides?.tar, "7.5.21");
});
