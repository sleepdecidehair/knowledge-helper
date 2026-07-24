import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("回答来源合并为可展开的紧凑文件控件", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /const \[expandedSourceIds, setExpandedSourceIds\] = useState/);
  assert.match(source, /const displaySources =/);
  assert.match(source, /aria-expanded={isSourceExpanded}/);
  assert.match(source, /source-preview-card/);
  assert.match(source, /aria-label={`预览文件/);
  assert.match(source, /aria-label={`下载文件/);
  assert.match(source, /user && message\.attachments\?\.length/);
  assert.doesNotMatch(source, /!user && message\.attachments/);
  assert.doesNotMatch(source, /source\.score/);
  assert.match(source, /function RetrievalDiagnosticView/);
  assert.doesNotMatch(source, /const redact =/);
  assert.doesNotMatch(source, /redact\(message\.content\)/);
});
