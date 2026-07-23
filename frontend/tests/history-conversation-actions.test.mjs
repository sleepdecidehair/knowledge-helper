import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("每条历史会话都使用三点下拉菜单承载操作", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /<Dropdown\.Trigger[\s\S]*?<Ellipsis size=\{16\}/);
  assert.match(source, /<Dropdown\.Item\s+id="rename"/);
  assert.match(source, /<Dropdown\.Item\s+id="pin"/);
  assert.match(source, /<Dropdown\.Item\s+id="export"/);
  assert.match(source, /<Dropdown\.Item\s+id="delete"/);
  assert.doesNotMatch(source, /history-current-actions/);
});
