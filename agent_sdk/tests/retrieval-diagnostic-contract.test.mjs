import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/runner.ts", import.meta.url), "utf8");

test("SDK 将最后实际检索的查询和诊断返回给页面", () => {
  assert.match(source, /diagnostic: JsonRecord/);
  assert.match(source, /query: string/);
  assert.match(
    source,
    /retrieval: \{\s*query: String\(lastSearch\.query \?\? ""\),\s*diagnostic: lastSearch\.diagnostic \?\? \{\}/,
  );
});
