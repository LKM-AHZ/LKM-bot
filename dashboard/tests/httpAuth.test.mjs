import assert from 'node:assert/strict'
import { test } from 'node:test'

test('logged-in dashboard requests keep credentials on their own origin', async (t) => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const originalStorage = Object.getOwnPropertyDescriptor(
    globalThis,
    'localStorage'
  )
  const nativeFetch = globalThis.fetch
  t.after(() => {
    globalThis.fetch = nativeFetch
    if (originalWindow)
      Object.defineProperty(globalThis, 'window', originalWindow)
    else delete globalThis.window
    if (originalStorage)
      Object.defineProperty(globalThis, 'localStorage', originalStorage)
    else delete globalThis.localStorage
  })
  const storage = new Map([
    ['token', 'test-dashboard-token'],
    ['lkmbot-locale', 'zh-CN']
  ])
  const requests = []
  const fetchStub = async (input, init) => {
    const headers = new Headers(
      init?.headers || (input instanceof Request ? input.headers : undefined)
    )
    requests.push({ input, init, headers })
    return Response.json({})
  }
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    writable: true,
    value: {
      location: new URL('http://localhost:3000/#/providers'),
      fetch: fetchStub
    }
  })
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    writable: true,
    value: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key)
    }
  })
  const { setupHttpClient, httpClient, apiV1Client } = await import(
    '../src/api/http.ts'
  )
  setupHttpClient()
  globalThis.fetch = window.fetch

  await t.test(
    'fetch URL, Request, protocol-relative and different-port destinations do not receive automatic auth',
    async () => {
      for (const input of [
        'https://example.com/api',
        '//example.com/api',
        'http://localhost:3001/api',
        new URL('https://example.com/api'),
        new Request('https://example.com/api')
      ]) {
        await window.fetch(input)
        assert.equal(requests.at(-1).headers.has('Authorization'), false)
        assert.equal(requests.at(-1).headers.has('Accept-Language'), false)
      }
      await window.fetch('https://example.com/api', {
        headers: { Authorization: 'Bearer explicit-third-party-token' }
      })
      assert.equal(
        requests.at(-1).headers.get('Authorization'),
        'Bearer explicit-third-party-token'
      )
    }
  )
  await t.test(
    'same-origin fetch and streaming requests retain auth, locale, and explicit headers',
    async () => {
      for (const input of [
        '/api/v1/config',
        new URL('http://localhost:3000/api/v1/config'),
        new Request('http://localhost:3000/api/v1/chat/send')
      ]) {
        await window.fetch(input)
        assert.equal(
          requests.at(-1).headers.get('Authorization'),
          'Bearer test-dashboard-token'
        )
        assert.equal(requests.at(-1).headers.get('Accept-Language'), 'zh-CN')
      }
      await window.fetch('/api/v1/chat/send', {
        headers: {
          Authorization: 'Bearer explicit-local-token',
          'Accept-Language': 'en-US'
        }
      })
      assert.equal(
        requests.at(-1).headers.get('Authorization'),
        'Bearer explicit-local-token'
      )
      assert.equal(requests.at(-1).headers.get('Accept-Language'), 'en-US')
    }
  )
  await t.test(
    'Axios resolves relative URLs and base URLs before attaching credentials',
    async () => {
      const adapter = async (config) => ({
        status: 200,
        statusText: 'OK',
        headers: {},
        data: config.headers,
        config
      })
      for (const config of [
        { url: 'https://example.com/catalog' },
        { url: '//example.com/catalog' },
        { url: '/catalog', baseURL: 'https://example.com' },
        { url: 'http://localhost:3001/api' }
      ]) {
        const response = await httpClient.request({ ...config, adapter })
        assert.equal(response.data.has('Authorization'), false)
        assert.equal(response.data.has('Accept-Language'), false)
      }
      for (const client of [httpClient, apiV1Client]) {
        const response = await client.get('/config', { adapter })
        assert.equal(
          response.data.get('Authorization'),
          'Bearer test-dashboard-token'
        )
        assert.equal(response.data.get('Accept-Language'), 'zh-CN')
      }
      const response = await httpClient.get('https://example.com/api', {
        adapter,
        headers: { Authorization: 'Bearer explicit-third-party-token' }
      })
      assert.equal(
        response.data.get('Authorization'),
        'Bearer explicit-third-party-token'
      )
    }
  )
})
