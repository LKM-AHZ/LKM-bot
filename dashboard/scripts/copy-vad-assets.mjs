// Copy the speech-detection (VAD) runtime out of node_modules into public/vendor/vad.
//
// The panel used to inject <script> tags pointing at cdn.jsdelivr.net for onnxruntime-web and
// @ricky0123/vad-web, so opening the microphone downloaded ~11 MB from a third-party CDN. Those
// files are already in node_modules at build time — copying them into the app's own origin removes
// the outbound dependency without changing the feature.
//
// The files are generated, not committed: `public/vendor` is gitignored and this script runs before
// dev and before every build.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const OUT_DIR = path.join(ROOT, 'public', 'vendor', 'vad');

/** [source path relative to node_modules, file name in public/vendor/vad] */
const FILES = [
  ['onnxruntime-web/dist/ort.wasm.min.js', 'ort.wasm.min.js'],
  ['onnxruntime-web/dist/ort-wasm-simd-threaded.mjs', 'ort-wasm-simd-threaded.mjs'],
  ['onnxruntime-web/dist/ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.wasm'],
  ['@ricky0123/vad-web/dist/bundle.min.js', 'bundle.min.js'],
  ['@ricky0123/vad-web/dist/silero_vad_v5.onnx', 'silero_vad_v5.onnx'],
  ['@ricky0123/vad-web/dist/silero_vad_legacy.onnx', 'silero_vad_legacy.onnx'],
  ['@ricky0123/vad-web/dist/vad.worklet.bundle.min.js', 'vad.worklet.bundle.min.js'],
];

export function copyVadAssets() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const copied = [];
  for (const [src, name] of FILES) {
    const from = path.join(ROOT, 'node_modules', src);
    if (!fs.existsSync(from)) {
      throw new Error(
        `[copy-vad-assets] missing ${src} — run "pnpm install" first (onnxruntime-web and ` +
          '@ricky0123/vad-web are devDependencies).'
      );
    }
    fs.copyFileSync(from, path.join(OUT_DIR, name));
    copied.push(name);
  }
  return copied;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const copied = copyVadAssets();
  console.log(`[copy-vad-assets] ${copied.length} files -> public/vendor/vad`);
}
