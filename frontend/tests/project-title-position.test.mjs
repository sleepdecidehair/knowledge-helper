import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const stylePath = new URL("../src/App.css", import.meta.url);

test("当前项目名称固定在主内容区左上，而非受居中内容列约束", async () => {
  const [source, css] = await Promise.all([
    readFile(sourcePath, "utf8"),
    readFile(stylePath, "utf8"),
  ]);

  assert.match(source, /<header className="app-header">/);
  assert.match(source, /activeProject\.name/);
  assert.match(
    css,
    /\.workspace-content\s*\{[\s\S]*?max-width: 1180px;[\s\S]*?margin: 0 auto;/,
  );
  assert.match(source, /<div className="workspace-content">/);
});
