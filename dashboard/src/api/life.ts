/**
 * Life layer API for the dashboard.
 *
 * The life endpoints are plain dashboard routes under `/api/v1/life`, so this
 * module talks to them through the shared axios instance (which prefixes the
 * deployment base path) instead of the generated SDK, whose base URL handling
 * would double the prefix for absolute paths.
 */
import { httpClient } from './http';

export interface ApiEnvelope<T> {
  status: 'ok' | 'error';
  message?: string | null;
  data: T;
}

async function get<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  const response = await httpClient.get<ApiEnvelope<T>>(url, { params });
  return response.data.data;
}

async function send<T>(
  method: 'post' | 'put' | 'patch',
  url: string,
  body?: unknown,
  params?: Record<string, unknown>,
): Promise<T> {
  const response = await httpClient.request<ApiEnvelope<T>>({
    method,
    url,
    data: body,
    params,
  });
  return response.data.data;
}

async function remove<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  const response = await httpClient.delete<ApiEnvelope<T>>(url, { params });
  return response.data.data;
}

export const lifeApi = {
  status() {
    return get<Record<string, unknown>>('/api/v1/life/status');
  },
  memory(params: { limit?: number; kind?: string }) {
    return get<Record<string, unknown>>('/api/v1/life/memory', params);
  },
  memorySearch(params: { q: string; umo: string; mode?: string; limit?: number }) {
    return get<Record<string, unknown>[]>('/api/v1/life/memory/search', params);
  },
  memoryDelete(params: { umo?: string; ids?: string }) {
    return remove<{ deleted: number }>('/api/v1/life/memory', params);
  },
  memoryExport(umo?: string) {
    return get<Record<string, unknown>[]>('/api/v1/life/memory/export', { umo });
  },
  people(params: { umo: string; limit?: number }) {
    return get<Record<string, unknown>[]>('/api/v1/life/people', params);
  },
  personProfile(params: { umo: string; person: string; refresh?: boolean }) {
    return get<Record<string, unknown>>('/api/v1/life/people/profile', params);
  },
  peopleDelete(params: { umo: string; person: string; fact_id?: number }) {
    return remove<Record<string, number>>('/api/v1/life/people', params);
  },
  factResolve(payload: { fact_id: number; keep: boolean }) {
    return send<{ changed: number }>('patch', '/api/v1/life/people/fact', payload);
  },
  expressions(params: { umo: string; limit?: number }) {
    return get<Record<string, unknown>[]>('/api/v1/life/expressions', params);
  },
  expressionDelete(params: { umo?: string; ids?: string }) {
    return remove<{ deleted: number }>('/api/v1/life/expressions', params);
  },
  jargon(params: { umo: string; status?: string; limit?: number }) {
    return get<Record<string, unknown>[]>('/api/v1/life/jargon', params);
  },
  jargonReview(payload: { id: number; status: string; meaning?: string }) {
    return send<{ updated: number }>('patch', '/api/v1/life/jargon', payload);
  },
  jargonDelete(params: { umo?: string; ids?: string }) {
    return remove<{ deleted: number }>('/api/v1/life/jargon', params);
  },
  emojis(params: { umo: string; status?: string; limit?: number }) {
    return get<Record<string, unknown>[]>('/api/v1/life/emojis', params);
  },
  emojiUpdate(payload: { id: number; tags?: string[]; status?: string; caption?: string }) {
    return send<{ updated: number }>('patch', '/api/v1/life/emojis', payload);
  },
  emojiDelete(params: { umo?: string; ids?: string }) {
    return remove<{ deleted: number }>('/api/v1/life/emojis', params);
  },
  focus(umo?: string) {
    return get<Record<string, unknown>>('/api/v1/life/focus', { umo });
  },
  focusDelete(umo: string) {
    return remove<{ deleted: number }>('/api/v1/life/focus', { umo });
  },
  monitor(params: { session?: string; limit?: number }) {
    return get<Record<string, unknown>>('/api/v1/life/monitor', params);
  },
  attention(umo?: string) {
    return get<Record<string, unknown>>('/api/v1/life/attention', { umo });
  },
  config() {
    return get<Record<string, unknown>>('/api/v1/life/config');
  },
  saveConfig(payload: Record<string, unknown>) {
    return send<Record<string, unknown>>('put', '/api/v1/life/config', payload);
  },
  rollout(days = 14) {
    return get<Record<string, unknown>>('/api/v1/life/rollout', { days });
  },
  migrationRuns(limit = 20) {
    return get<{ enabled: boolean; runs: Record<string, unknown>[] }>(
      '/api/v1/life/migration/runs',
      { limit },
    );
  },
  migrationPreview(payload: {
    source: string;
    memory_source?: string;
    datasets?: string[];
    umos?: string[];
    limit?: number;
  }) {
    return send<Record<string, unknown>>('post', '/api/v1/life/migration/preview', payload);
  },
  migrationRollback(batch: string) {
    return send<Record<string, unknown>>('post', '/api/v1/life/migration/rollback', {
      batch,
    });
  },
};
