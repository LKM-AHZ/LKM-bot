/**
 * 面板子路径（base path）工具。
 *
 * 背景：面板可被反向代理挂在子路径下（本部署为社区站同域 `/bot/`，网关 `proxy-rewrite`
 * 剥掉前缀后再转发到面板进程）。此时 **浏览器** 发出的每个请求都必须带前缀，而面板代码里
 * 大量路径是硬编码的绝对路径（`/api/v1/...`、`/api/stat/...`、`/t2i/...`），axios 的
 * `baseURL` 对以 `/` 开头的 url 又不生效。
 *
 * 处理方式：
 * - axios 请求：由 `http.ts` 的请求拦截器统一补前缀（一处生效，含 `pnpm generate:api` 生成的
 *   `sdk.gen.ts`，故重生成代码不会回潮）；
 * - 非 axios 的直连 URL（WebSocket / EventSource / iframe / fetch / `<script src>`）：
 *   显式用 `apiUrl()` 包装——`tests/apiBasePrefix.test.mjs` 会扫描这类裸写法防回潮。
 *
 * 前缀来自构建期注入的 `import.meta.env.BASE_URL`（由 `vite.config.ts` 的 `base` 决定，
 * 构建命令传 `--base=/bot/`）。默认构建为 `/`，此时所有函数退化为恒等变换，单机部署行为不变。
 *
 * 纯函数在 `basePath.ts`（不引 `import.meta.env`，便于 node 测试直接 import）。
 */
// 显式带 .ts 扩展名：本模块链（http.ts → base.ts → basePath.ts）会被 node 原生测试
// （tests/httpAuth.test.mjs）直接 import，而 node 的 ESM 解析不做扩展名补全。
import { applyBase, removeBase, resolveBase } from './basePath.ts';

/**
 * 规范化后的前缀：`''`（根路径）或 `'/bot'`（无尾斜杠）。
 *
 * 用可选链读 `import.meta.env`：Vite 在浏览器端注入该对象（构建期 base 决定 `BASE_URL`），
 * 而 node 原生测试（`tests/httpAuth.test.mjs` 直接 import 本模块链）下它是 undefined ——
 * 此时退化为根路径，恰好也是纯函数测试要的语义。
 */
export const BASE = resolveBase(import.meta.env?.BASE_URL);

/** 给面板内的绝对路径补上前缀（幂等）。 */
export function withBase(path: string): string {
  return applyBase(path, BASE);
}

/** 去掉前缀，还原成面板后端视角的路径（用于与后端路径常量做比较）。 */
export function stripBase(path: string): string {
  return removeBase(path, BASE);
}

/** 直连 URL（WS/SSE/iframe/下载）专用别名：语义上强调"这是给浏览器用的地址"。 */
export function apiUrl(path: string): string {
  return applyBase(path, BASE);
}
