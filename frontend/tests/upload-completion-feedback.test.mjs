import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const stylePath = new URL("../src/App.css", import.meta.url);

test("知识库上传反馈以服务端 ready 作为 100% 和可用条件", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /type UploadFeedback = \{/);
  assert.match(source, /size_bytes: number/);
  assert.match(source, /function formatFileSize\(sizeBytes: number\)/);
  assert.match(source, /progress: 8/);
  assert.match(source, /Math\.min\(item\.assetId \? 92 : 68, item\.progress \+ 4\)/);
  assert.match(source, /status: "available", progress: 100/);
  assert.match(source, /waitForUploadedAssets\(\[uploadedAsset\], uploadProjectId\)/);
  assert.match(source, /上传中/);
  assert.match(source, /可用/);
  assert.match(source, /formatFileSize\(asset\.size_bytes\)/);
});

test("上传记录仅使用短 transform opacity 动效并支持减弱动效", async () => {
  const css = await readFile(stylePath, "utf8");

  assert.match(css, /\.upload-feedback-item[\s\S]*?animation: upload-feedback-enter 160ms ease-out/);
  assert.match(css, /\.upload-feedback-item\.is-exiting[\s\S]*?opacity: 0;[\s\S]*?transform: translateY\(-6px\)/);
  assert.match(css, /\.upload-feedback-progress > span[\s\S]*?transition: transform 180ms ease-out/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /\.upload-feedback-item[\s\S]*?\.asset-card\.is-new[\s\S]*?animation: none !important/);
  assert.match(css, /transform: none !important/);
});
