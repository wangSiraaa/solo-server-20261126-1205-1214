export async function api(path, options = {}) {
  const headers = { 'content-type': 'application/json', ...(options.headers ?? {}) };
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = new Error(data?.message ?? `请求失败 ${response.status}`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function $$(selector, root = document) {
  return [...root.querySelectorAll(selector)];
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

export function badge(status) {
  const map = {
    dispatched: 'blue', accepted: 'blue', active: 'blue',
    'awaiting-review': 'amber', submitted: 'amber',
    approved: 'green', 'approved-payable': 'green', paid: 'green',
    rejected: 'red', 'rejected-auto': 'red',
    'settlement-blocked': 'red',
  };
  return `<span class="badge ${map[status] ?? 'gray'}">${escapeHtml(status)}</span>`;
}

export function money(settlementOrAmount, currency) {
  const amount = typeof settlementOrAmount === 'number'
    ? settlementOrAmount
    : settlementOrAmount?.amount ?? 0;
  const unit = currency ?? settlementOrAmount?.currency ?? 'points';
  return `${amount} ${unit === 'points' ? '点' : unit}`;
}

export function getTesterId() {
  return localStorage.getItem('testerId') ?? '';
}
export function setTesterId(id) {
  localStorage.setItem('testerId', id);
}
export function getDeviceId() {
  let id = localStorage.getItem('deviceId');
  if (!id) {
    id = `dev-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem('deviceId', id);
  }
  return id;
}

export function showError(container, error) {
  const details = error.data?.details ? `<pre class="json">${escapeHtml(JSON.stringify(error.data.details, null, 2))}</pre>` : '';
  container.innerHTML = `<div class="notice err"><strong>${escapeHtml(error.data?.error ?? 'ERROR')}</strong>：${escapeHtml(error.message)}${details}</div>`;
}
