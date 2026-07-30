import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const sourcePath = new URL("../src/App.tsx", import.meta.url);

test("资料预览在当前页面的弹窗中打开", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(
    source,
    /const \[previewFile, setPreviewFile\] = useState<FilePreview \| null>\(null\)/,
  );
  assert.match(source, /const previewDialog = useOverlayState\(\)/);
  assert.match(source, /<Modal state=\{previewDialog\}>/);
  assert.match(source, /aria-label=\{`预览文件 \$\{source\.name\}`\}/);
  assert.match(source, /<iframe[\s\S]*?title=\{`预览文件 \$\{previewFile\.name\}`\}/);
  assert.doesNotMatch(
    source,
    /href=\{previewUrl\}[\s\S]{0,160}target="_blank"/,
  );
  assert.match(
    source,
    /message\.attachments\?\.length[\s\S]*?<button[\s\S]*?className="attachment-link"[\s\S]*?onClick=\{\(\) => onPreview\?\.\(\{ assetId: asset\.asset_id, name: asset\.name \}\)\}/,
  );
});
