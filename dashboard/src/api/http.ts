import axios, {
  type AxiosError,
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from 'axios';

// 显式带 .ts 扩展名：本模块被 node 原生测试直接 import，node 的 ESM 解析不补扩展名。
import { BASE, stripBase, withBase } from './base.ts';

const AUTH_HEADER = 'Authorization';
const LOCALE_HEADER = 'Accept-Language';

let configured = false;
let originalFetch: typeof window.fetch | null = null;

export const httpClient = axios;
export const apiV1Client = axios.create({ baseURL: `${BASE}/api/v1` });

function getToken(): string | null {
  return localStorage.getItem('token');
}

function getLocale(): string | null {
  return localStorage.getItem('astrbot-locale');
}

function setAxiosHeader(
  headers: InternalAxiosRequestConfig['headers'],
  key: string,
  value: string,
) {
  if (typeof headers.set === 'function') {
    headers.set(key, value);
    return;
  }
  headers[key] = value;
}

/**
 * 子路径前缀补全：面板代码（含 `generate:api` 生成的 SDK）里写死了大量绝对路径
 * `/api/v1/...`，而 axios 的 `baseURL` 只对**相对** url 生效，故在此统一补前缀。
 * 幂等，因此对已带前缀的 url（如 `apiV1Client` 的相对路径解析结果）无副作用。
 */
function prefixBase(config: InternalAxiosRequestConfig) {
  if (typeof config.url === 'string' && config.url.startsWith('/')) {
    config.url = withBase(config.url);
  }
  return config;
}

function attachAxiosHeaders(config: InternalAxiosRequestConfig) {
  // Dashboard credentials must not be attached to third-party requests.
  try {
    const requestUrl = new URL(axios.getUri(config), window.location.href);
    if (requestUrl.origin !== window.location.origin) return config;
  } catch {
    return config;
  }

  const token = getToken();
  if (token) {
    setAxiosHeader(config.headers, AUTH_HEADER, `Bearer ${token}`);
  }

  const locale = getLocale();
  if (locale) {
    setAxiosHeader(config.headers, LOCALE_HEADER, locale);
  }

  return config;
}

function normalizeAxiosError(error: AxiosError) {
  if (error.response?.status === 401) {
    let requestPath = '';
    try {
      const url = error.config?.url || '';
      const baseURL = error.config?.baseURL;
      const resolvedUrl =
        url && baseURL && !/^([a-z][a-z\d+\-.]*:)?\/\//i.test(url)
          ? `${baseURL.replace(/\/+$/, '')}/${url.replace(/^\/+/, '')}`
          : url;
      const requestUrl = new URL(resolvedUrl || '/', window.location.origin);
      if (requestUrl.origin === window.location.origin) {
        // 去掉面板前缀再与后端路径常量比较（挂在 /bot/ 下时 pathname 会带前缀）。
        requestPath = stripBase(requestUrl.pathname);
      }
    } catch {
      requestPath = '';
    }

    const isAuthChallenge =
      [
        '/api/auth/login',
        '/api/auth/setup',
        '/api/auth/setup-status',
        '/api/v1/auth/login',
        '/api/v1/auth/setup',
        '/api/v1/auth/setup-status',
        // 会话水合：未登录时 401 是正常路径（守卫据此判定"未登录"），不该再触发跳转副作用。
        '/api/v1/auth/session',
      ].includes(requestPath) ||
      Boolean(
        (
          error.response.data as
            | { data?: { totp_required?: boolean } }
            | undefined
        )?.data?.totp_required,
      );

    if (requestPath.startsWith('/api/') && !isAuthChallenge) {
      [
        'user',
        'token',
        'change_pwd_hint',
        'md5_pwd_hint',
        'password_upgrade_required',
      ].forEach((key) => localStorage.removeItem(key));

      if (!window.location.hash.startsWith('#/auth/login')) {
        window.location.hash = '/auth/login';
      }
    }
  }

  if (error.response?.status === 429) {
    const data = error.response.data as { message?: string } | undefined;
    if (data?.message) {
      return Promise.reject(data.message);
    }
  }
  return Promise.reject(error);
}

function installAxiosInterceptors(instance: AxiosInstance) {
  instance.interceptors.request.use(prefixBase);
  instance.interceptors.request.use(attachAxiosHeaders);
  instance.interceptors.response.use((response) => response, normalizeAxiosError);
}

export function fetchWithAuth(input: RequestInfo | URL, init?: RequestInit) {
  const fetchImpl = originalFetch ?? window.fetch.bind(window);
  // The global fetch wrapper also handles public, cross-origin resources.
  try {
    const requestUrl = new URL(
      input instanceof Request ? input.url : input,
      window.location.href,
    );
    if (requestUrl.origin !== window.location.origin) {
      return fetchImpl(input, init);
    }
  } catch {
    return fetchImpl(input, init);
  }

  // 同源绝对路径补面板前缀（字符串形态覆盖面板全部调用点；Request 对象由调用方自行构造完整 URL）。
  if (typeof input === 'string') {
    input = withBase(input);
  }

  const token = getToken();
  const locale = getLocale();

  if (!token && !locale) {
    return fetchImpl(input, init);
  }

  const requestHeaders =
    typeof input !== 'string' && 'headers' in input
      ? (input as Request).headers
      : undefined;
  const headers = new Headers(init?.headers || requestHeaders);

  if (token && !headers.has(AUTH_HEADER)) {
    headers.set(AUTH_HEADER, `Bearer ${token}`);
  }
  if (locale && !headers.has(LOCALE_HEADER)) {
    headers.set(LOCALE_HEADER, locale);
  }

  return fetchImpl(input, { ...init, headers });
}

export function setupHttpClient() {
  if (configured) {
    return;
  }

  installAxiosInterceptors(axios);
  installAxiosInterceptors(apiV1Client);

  originalFetch = window.fetch.bind(window);
  window.fetch = fetchWithAuth;

  configured = true;
}
