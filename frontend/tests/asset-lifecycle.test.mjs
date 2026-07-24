import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("资料库展示版本、元数据、重处理和本机视觉配置", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /\/api\/assets\/\$\{asset\.asset_id\}\/reprocess/);
  assert.match(source, /\/api\/assets\/\$\{asset\.asset_id\}\/replace/);
  assert.match(source, /\/api\/assets\/\$\{asset\.asset_id\}\/restore/);
  assert.match(source, /版本 v\{asset\.version\.number\}/);
  assert.match(source, /资料标签/);
  assert.match(source, /视觉服务/);
  assert.match(source, /vision_max_pages/);
});
