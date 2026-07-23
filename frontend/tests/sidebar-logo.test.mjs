import assert from "node:assert/strict";
import test from "node:test";
import { access, readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const stylePath = new URL("../src/App.css", import.meta.url);
const logoPath = new URL("../src/assets/knowledge-helper-logo.svg", import.meta.url);

test("侧栏使用提供的知识库图标，并以紧凑尺寸展示", async () => {
  const [source, css] = await Promise.all([
    readFile(sourcePath, "utf8"),
    readFile(stylePath, "utf8"),
  ]);

  await assert.doesNotReject(access(logoPath));
  assert.match(source, /import brandLogoUrl from "\.\/assets\/knowledge-helper-logo\.svg";/);
  assert.match(
    source,
    /<img\s+className="brand-mark"\s+src=\{brandLogoUrl\}\s+alt="知识库助手"\s*\/>/,
  );
  assert.match(css, /\.brand-mark\s*\{[\s\S]*?width: 32px;[\s\S]*?height: 32px;/);
  assert.match(css, /\.brand-mark\s*\{[\s\S]*?object-fit: contain;/);
});
