import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

const bag = new Map();
globalThis.sessionStorage = {
  getItem: (key) => (bag.has(key) ? bag.get(key) : null),
  setItem: (key, value) => bag.set(key, String(value)),
  removeItem: (key) => bag.delete(key),
  clear: () => bag.clear(),
};
globalThis.dispatchEvent = () => true;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.window = globalThis;

const TOKEN_KEY = 'colab-gpu-studio-token';
const RESTORED = 'restored-session-token';
const calls = [];

function jsonResponse(status, body) {
  const text = JSON.stringify(body);
  return { ok: status >= 200 && status < 300, status, text: async () => text, json: async () => JSON.parse(text) };
}

globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init });
  const path = String(url);
  if (path.endsWith('/api/auth/login')) {
    const password = JSON.parse(init.body).password;
    if (password !== 'right-password') {
      return jsonResponse(401, { code: 'INVALID_CREDENTIALS', message: '密码不正确或尚未初始化' });
    }
    return jsonResponse(200, { access_token: 'login-session-token1', token_type: 'bearer', expires_in: 43200 });
  }
  if (path.endsWith('/api/auth/logout')) return jsonResponse(200, { logged_out: true });
  if (path.endsWith('/api/auth/session')) {
    if (globalThis.fetch.sessionStatus) return jsonResponse(globalThis.fetch.sessionStatus, { code: 'UNAUTHENTICATED', message: '请先登录' });
    return jsonResponse(200, { authenticated: true, access_token: RESTORED, token_type: 'bearer', expires_in: 40000 });
  }
  if (path.endsWith('/api/gpu/projects') && init.method === 'POST') {
    const posts = calls.filter((call) => call.url.endsWith('/api/gpu/projects') && call.init.method === 'POST');
    if (posts.length === 1) return jsonResponse(401, { error: { code: 'UNAUTHENTICATED', message: '请先登录' } });
    return jsonResponse(201, { data: { id: 'p1', title: 'restored' }, error: null });
  }
  return jsonResponse(404, { message: 'missing' });
};

const api = await import('../src/lib/gpu-api.ts');

async function blank() {
  globalThis.fetch.sessionStatus = 0;
  await api.logout();
  bag.clear();
  calls.length = 0;
}

function authHeader(call) {
  return new Headers(call.init.headers).get('Authorization');
}

describe('browser session restoration', { concurrency: false }, () => {
test('refresh keeps a stored bearer and does not ask for the password', async () => {
  await blank();
  bag.set(TOKEN_KEY, 'refresh-session-token');
  assert.equal(await api.restoreSession(), true);
  assert.equal(api.isLoggedIn(), true);
  const probe = calls.find((call) => call.url.endsWith('/api/auth/session'));
  assert.equal(authHeader(probe), 'Bearer refresh-session-token');
  assert.equal(probe.init.credentials, 'include');
  assert.equal(calls.some((call) => call.url.endsWith('/api/auth/login')), false);
});

test('new tab restores a bearer from the cookie when sessionStorage is empty', async () => {
  await blank();
  assert.equal(api.isLoggedIn(), false);
  assert.equal(await api.restoreSession(), true);
  assert.equal(api.isLoggedIn(), true);
  assert.equal(bag.get(TOKEN_KEY), RESTORED);
  const probe = calls.find((call) => call.url.endsWith('/api/auth/session'));
  assert.equal(authHeader(probe), null);
  assert.equal(probe.init.credentials, 'include');
});

test('reopen after the document is discarded restores from the cookie', async () => {
  await blank();
  await api.login('right-password');
  assert.equal(api.isLoggedIn(), true);
  bag.clear();
  calls.length = 0;
  // Same JS realm still has the in-memory copy, matching a storage wipe without a reload.
  assert.equal(api.isLoggedIn(), true);
  await api.logout();
  bag.clear();
  calls.length = 0;
  // A reopened document has neither memory nor sessionStorage. The cookie travels with credentials.
  assert.equal(await api.restoreSession(), true);
  assert.equal(bag.get(TOKEN_KEY), RESTORED);
  assert.equal(authHeader(calls.find((call) => call.url.endsWith('/api/auth/session'))), null);
});

test('expired cookie restore shows logged out and does not keep a bearer', async () => {
  await blank();
  bag.set(TOKEN_KEY, 'expired-session-token');
  globalThis.fetch.sessionStatus = 401;
  assert.equal(await api.restoreSession(), false);
  assert.equal(api.isLoggedIn(), false);
  assert.equal(bag.has(TOKEN_KEY), false);
});

test('logout clears the bearer and posts to the server', async () => {
  await blank();
  await api.login('right-password');
  calls.length = 0;
  await api.logout();
  assert.equal(api.isLoggedIn(), false);
  const loggedOut = calls.find((call) => call.url.endsWith('/api/auth/logout'));
  assert.equal(loggedOut.init.method, 'POST');
  assert.equal(loggedOut.init.credentials, 'include');
  assert.equal(authHeader(loggedOut), 'Bearer login-session-token1');
});

test('a write without a bearer restores once and retries with the restored token', async () => {
  await blank();
  const project = await api.createProject('restored');
  assert.equal(project.id, 'p1');
  const posts = calls.filter((call) => call.url.endsWith('/api/gpu/projects'));
  assert.equal(posts.length, 2);
  assert.equal(authHeader(posts[0]), null);
  assert.equal(authHeader(posts[1]), `Bearer ${RESTORED}`);
  assert.equal(calls.some((call) => call.url.endsWith('/api/auth/login')), false);
});

test('a rejected password does not clear or revoke an existing session', async () => {
  await blank();
  bag.set(TOKEN_KEY, 'still-valid-token');
  await assert.rejects(() => api.login('wrong-password'));
  assert.equal(bag.get(TOKEN_KEY), 'still-valid-token');
  assert.equal(calls.some((call) => call.url.endsWith('/api/auth/logout')), false);
  assert.equal(calls.some((call) => call.url.endsWith('/api/auth/session')), false);
});
});
