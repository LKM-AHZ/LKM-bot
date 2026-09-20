/**
 * 构建后写入 `dist/assets/version`。
 *
 * 面板后端（`astrbot/core/dashboard_assets.py`）以此文件判定 dist 是否与当前 Core 版本兼容：
 * 缺失或不匹配时 `AstrBotUpdater.ensure_dashboard()` 会**去上游 registry 下载** WebUI 覆盖它。
 * 本仓库自带构建（子路径 base）一旦被下载覆盖，`/bot/` 子路径下的资源与 API 前缀就会失配，
 * 故自建 dist 必须带上正确的版本标记。
 *
 * 版本来源与 Core 同源：`astrbot/__init__.py` 的 `__version__`（`config.default.VERSION` 由它派生）。
 */
import { mkdirSync, readFileSync, writeFileSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';

const here = dirname(fileURLToPath(import.meta.url));
const dashboardRoot = resolve(here, '..');
const packageRoot = resolve(dashboardRoot, '..');

const initFile = resolve(packageRoot, 'astrbot', '__init__.py');
const match = readFileSync(initFile, 'utf8').match(
  /^__version__\s*=\s*["']([^"']+)["']/m,
);
if (!match) {
  console.error(`[write-dist-version] Cannot read __version__ from ${initFile}`);
  process.exit(1);
}

const versionDir = resolve(dashboardRoot, 'dist', 'assets');
mkdirSync(versionDir, { recursive: true });
writeFileSync(resolve(versionDir, 'version'), match[1], 'utf8');
console.log(`[write-dist-version] dist version = ${match[1]}`);
