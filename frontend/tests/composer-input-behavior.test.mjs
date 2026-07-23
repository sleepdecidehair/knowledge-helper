import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const stylePath = new URL("../src/App.css", import.meta.url);

test("输入框允许 HeroUI 根据内容自动增高，并设置合理上限", async () => {
  const [source, css] = await Promise.all([
    readFile(sourcePath, "utf8"),
    readFile(stylePath, "utf8"),
  ]);
  const textarea = source.match(/<PromptInput\.TextArea[\s\S]*?\/>/)?.[0] || "";
  const textareaStyle =
    css.match(/\.composer-inline-textarea\[data-slot="prompt-input-textarea"\][\s\S]*?\n\}/)?.[0] || "";

  assert.doesNotMatch(textarea, /disableAutosize/);
  assert.match(source, /maxHeight=\{240\}/);
  assert.match(textareaStyle, /min-height: 40px !important;/);
  assert.match(textareaStyle, /max-height: 240px !important;/);
  assert.doesNotMatch(textareaStyle, /(?:^|\n)\s*height: 40px !important;/);
});

test("超长粘贴会转成待发送的 UTF-8 文本附件", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /const LONG_PASTE_THRESHOLD = 8_000;/);
  assert.match(source, /function handleQuestionPaste\(event: ClipboardEvent<HTMLTextAreaElement>\)/);
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /new File\(\s*\[pastedText\],[\s\S]*?type: "text\/plain;charset=utf-8"/);
  assert.match(source, /queueChatFiles\(\[pastedFile\]\)/);
  assert.match(source, /onPaste=\{handleQuestionPaste\}/);
});
