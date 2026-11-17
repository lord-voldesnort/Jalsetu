import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const targetFile = path.resolve(scriptDir, '../node_modules/rollup/dist/native.js');

if (fs.existsSync(targetFile)) {
  let content = fs.readFileSync(targetFile, 'utf8');
  if (content.includes("throw error;") && !content.includes("@rollup/wasm-node/dist/native.js")) {
    content = content.replace(
      "if (error instanceof Error && error.code === 'ERR_DLOPEN_FAILED') {\n\t\t\tthrow error;\n\t\t}",
      "if (error instanceof Error && error.code === 'ERR_DLOPEN_FAILED') {\n\t\t\ttry {\n\t\t\t\treturn require('@rollup/wasm-node/dist/native.js');\n\t\t\t} catch (_) {\n\t\t\t\tthrow error;\n\t\t\t}\n\t\t}"
    );
    fs.writeFileSync(targetFile, content, 'utf8');
    console.log('✔ Patched rollup native.js for Wasm fallback compatibility on Windows');
  }
}
