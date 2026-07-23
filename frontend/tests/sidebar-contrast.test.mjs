import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const stylePath = new URL("../src/App.css", import.meta.url);
const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("左侧会话文字使用高对比度银灰", async () => {
  const css = await readFile(stylePath, "utf8");

  assert.match(css, /\.history-sidebar span,[\s\S]*?color: #d4d4d8;/);
  assert.match(css, /\.context-meter-label span[\s\S]*?color: #d4d4d8 !important;/);
  assert.match(css, /\.sidebar-search::placeholder[\s\S]*?color: #d4d4d8;/);
});

test("会话进度条以用户设定的主动压缩阈值计量", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /const threshold = Math\.max\(1, fallbackThreshold\);/);
  assert.doesNotMatch(source, /usage\?\.threshold_tokens \|\| fallbackThreshold/);
  assert.match(source, /主动压缩阈值：\$\{label\}/);
  assert.match(source, /SDK 自动压缩兜底阈值：\$\{formatTokens\(sdkThreshold\)\} tokens/);
});
