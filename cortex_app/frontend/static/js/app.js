/* Shared utilities: auth, API client, toasts */

const API = '/';

function getToken() { return localStorage.getItem('access_token'); }
function getRefreshToken() { return localStorage.getItem('refresh_token'); }
function setTokens(access, refresh) {
  localStorage.setItem('access_token', access);
  localStorage.setItem('refresh_token', refresh);
}
function clearTokens() {
  localStorage.removeItem('access_token');
  localStorage.removeItem('refresh_token');
}

function isLoggedIn() { return !!getToken(); }

function redirectToAuth() { window.location.href = '/auth.html'; }
function redirectHome() { window.location.href = '/index.html'; }

// Auto-redirect if not logged in (call on protected pages)
function requireAuth() { if (!isLoggedIn()) redirectToAuth(); }

async function apiFetch(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const resp = await fetch(API + path, { ...options, headers });

  if (resp.status === 401) {
    const refreshed = await _tryRefresh();
    if (refreshed) return apiFetch(path, options);
    clearTokens();
    redirectToAuth();
    return null;
  }
  return resp;
}

async function _tryRefresh() {
  const rt = getRefreshToken();
  if (!rt) return false;
  try {
    const resp = await fetch(API + 'auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    if (data.data) { setTokens(data.data.access_token, data.data.refresh_token); return true; }
    return false;
  } catch { return false; }
}

async function apiGet(path) {
  const resp = await apiFetch(path);
  if (!resp) return null;
  const data = await resp.json();
  if (data.status !== 'ok') throw new Error(data.message || 'Request failed');
  return data.data;
}

async function apiPost(path, body) {
  const resp = await apiFetch(path, { method: 'POST', body: JSON.stringify(body) });
  if (!resp) return null;
  const data = await resp.json();
  if (data.status !== 'ok') throw new Error(data.message || 'Request failed');
  return data.data;
}

async function apiPut(path, body) {
  const resp = await apiFetch(path, { method: 'PUT', body: JSON.stringify(body) });
  if (!resp) return null;
  const data = await resp.json();
  if (data.status !== 'ok') throw new Error(data.message || 'Request failed');
  return data.data;
}

async function apiDelete(path) {
  const resp = await apiFetch(path, { method: 'DELETE' });
  if (!resp) return null;
  return resp.json();
}

// ---- Toast notifications ----
function toast(msg, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  const el = document.createElement('div');
  el.className = `toast${type === 'error' ? ' error' : ''}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ---- SSE fetch (POST-based for auth headers) ----
async function* sseStream(path, body) {
  const token = getToken();
  const resp = await fetch(API + path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });

  if (!resp.ok) { throw new Error(`SSE failed: ${resp.status}`); }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split('\n\n');
    buffer = parts.pop();

    for (const chunk of parts) {
      if (!chunk.trim()) continue;
      const lines = chunk.split('\n');
      let event = null, data = null;
      for (const line of lines) {
        if (line.startsWith('event: ')) event = line.slice(7).trim();
        if (line.startsWith('data: ')) data = line.slice(6).trim();
      }
      if (data !== null) {
        try { yield { event, data: JSON.parse(data) }; }
        catch { yield { event, data }; }
      }
    }
  }
}

// ---- Modal helpers ----
function showModal(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hideModal(id) { document.getElementById(id)?.classList.add('hidden'); }
