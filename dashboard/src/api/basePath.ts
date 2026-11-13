/**
 * 子路径前缀的**纯函数**（不依赖 `import.meta.env`，可被 node 测试直接 import）。
 *
 * 组合与运行时接线在 `src/api/base.ts`：那里读构建期注入的 `import.meta.env.BASE_URL`
 * 得到前缀常量，再把这些函数绑上去。拆开的原因见 `base.ts` 顶部说明——面板挂在反向代理的
 * 子路径下时（本部署为社区站同域 `/bot/`），浏览器发出的每个请求都必须带前缀，而面板代码里
 * 大量路径是硬编码的绝对路径。
 */

/** 规范化前缀：`''`（根路径）或 `'/bot'`（无尾斜杠）；`/`、`//bot//` 等一律收敛。 */
export function resolveBase(raw?: string | null): string {
  const value = (raw ?? '').trim();
  if (!value || value === '/') {
    return '';
  }
  return `/${value.replace(/^\/+|\/+$/g, '')}`;
}

/** 给绝对路径补前缀（幂等：已带前缀的路径原样返回；非绝对路径不动）。 */
export function applyBase(path: string, base: string): string {
  if (!path.startsWith('/')) {
    return path;
  }
  if (!base || path === base || path.startsWith(`${base}/`)) {
    return path;
  }
  return `${base}${path}`;
}

/** 去掉前缀，还原成面板后端视角的路径（用于与后端路径常量做比较）。 */
export function removeBase(path: string, base: string): string {
  if (!base) {
    return path;
  }
  if (path === base) {
    return '/';
  }
  if (path.startsWith(`${base}/`)) {
    return path.slice(base.length);
  }
  return path;
}
