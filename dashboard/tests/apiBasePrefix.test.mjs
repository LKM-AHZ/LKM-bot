/**
 * 子路径（base path）工具与「防回潮」门禁。
 *
 * 面板挂在社区站同域子路径 `/bot/` 下时，浏览器发出的**每个**请求都必须带前缀；改漏一处
 * 的表现是运行期 404（或被反代到社区站同域路径），静态很难发现。故这里既测纯函数的边界，
 * 也扫描源码把「裸的直连 URL」挡在提交前。
 *
 * 说明：axios 调用（httpClient / apiV1Client / 全局 axios）由 `http.ts` 的请求拦截器统一补
 * 前缀，故**不**在扫描范围内；只拦真正绕开 axios 的写法（fetch / WebSocket / EventSource /
 * iframe src / new URL）。
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { applyBase, removeBase, resolveBase } from "../src/api/basePath.ts";

const SRC = fileURLToPath(new URL("../src", import.meta.url));

test("resolveBase 收敛各种形态：空 / 根 / 缺前导斜杠 / 多斜杠", () => {
  assert.equal(resolveBase(undefined), "");
  assert.equal(resolveBase(null), "");
  assert.equal(resolveBase(""), "");
  assert.equal(resolveBase("  "), "");
  assert.equal(resolveBase("/"), "");
  for (const raw of ["/bot", "bot", "/bot/", "//bot//"]) {
    assert.equal(resolveBase(raw), "/bot", raw);
  }
});

test("applyBase：补前缀、幂等、不动非绝对路径", () => {
  assert.equal(applyBase("/api/v1/x", "/bot"), "/bot/api/v1/x");
  assert.equal(applyBase("/bot/api/v1/x", "/bot"), "/bot/api/v1/x"); // 幂等
  assert.equal(applyBase("/", "/bot"), "/bot/");
  assert.equal(applyBase("api/v1/x", "/bot"), "api/v1/x"); // 相对路径不动
  assert.equal(applyBase("https://x/y", "/bot"), "https://x/y"); // 绝对 URL 不动
  // 根路径部署：恒等
  assert.equal(applyBase("/api/v1/x", ""), "/api/v1/x");
  // 前缀相同时不能被误判成"已带前缀"（/bota 不是 /bot 下的路径）
  assert.equal(applyBase("/bota/x", "/bot"), "/bot/bota/x");
});

test("removeBase：还原后端视角路径（供与后端常量比较）", () => {
  assert.equal(removeBase("/bot/api/v1/x", "/bot"), "/api/v1/x");
  assert.equal(removeBase("/bot", "/bot"), "/");
  assert.equal(removeBase("/bot/", "/bot"), "/");
  assert.equal(removeBase("/api/v1/x", "/bot"), "/api/v1/x");
  assert.equal(removeBase("/bot", ""), "/bot");
});

/** 递归收集 src 下的源码文件（跳过生成物与 node_modules）。 */
function collectSources(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (entry === "node_modules" || entry === "generated") continue;
      out.push(...collectSources(path));
      continue;
    }
    if (/\.(ts|mts|js|mjs|vue)$/.test(entry)) out.push(path);
  }
  return out;
}

test("非 axios 的直连 URL 必须显式带前缀（apiUrl/withBase），防改漏回潮", () => {
  // 只拦真正绕开 axios 的写法：这些一旦漏加前缀，运行期直接 404 或打到别的同域路径。
  const RISKY = [
    { name: "fetch", pattern: /(?:^|[^.\w])fetch(?:WithAuth)?\(\s*["'`]\/api\// },
    { name: "new WebSocket", pattern: /new WebSocket\(\s*[`"'][^`"']*\/api\// },
    { name: "new EventSource", pattern: /new EventSource\(\s*["'`]\/api\// },
    { name: "new URL", pattern: /new URL\(\s*["'`]\/api\// },
    { name: "iframe/script src", pattern: /\bsrc=["'`]\/api\// },
    { name: "location.href", pattern: /location\.href\s*=\s*["'`]\/api\// },
  ];
  // 白名单：实现前缀机制本身。
  const ALLOWED = new Set(["api/base.ts", "api/basePath.ts", "api/http.ts"]);

  const offenders = [];
  for (const file of collectSources(SRC)) {
    const rel = relative(SRC, file);
    if (ALLOWED.has(rel)) continue;
    const lines = readFileSync(file, "utf8").split("\n");
    lines.forEach((line, index) => {
      for (const { name, pattern } of RISKY) {
        if (pattern.test(line)) {
          offenders.push(`${rel}:${index + 1} [${name}] ${line.trim()}`);
        }
      }
    });
  }
  assert.deepEqual(
    offenders,
    [],
    `以下直连 URL 绕开了 apiUrl()/withBase()，在子路径部署下会 404：\n${offenders.join("\n")}`,
  );
});

test("vite base 由 VITE_BASE_PATH 驱动，默认保持根路径", () => {
  const config = readFileSync(
    new URL("../vite.config.ts", import.meta.url),
    "utf8",
  );
  assert.match(config, /process\.env\.VITE_BASE_PATH\s*\|\|\s*'\/'/);
  assert.match(config, /base:\s*basePath/);
  // dev proxy 同样剥前缀，保证 dev 与线上路径语义一致
  assert.match(config, /rewrite:/);
});

test("构建脚本产出带版本的 dist（否则后端会去上游下载根 base 的 WebUI）", () => {
  const pkg = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf8"),
  );
  assert.match(pkg.scripts["build:subpath"], /vite build/);
  assert.match(pkg.scripts["build:subpath"], /write-dist-version\.mjs/);
  assert.doesNotMatch(pkg.scripts["build:subpath"], /vue-tsc/);
});
