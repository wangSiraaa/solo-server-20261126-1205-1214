const scenarioOutput = document.querySelector('#scenario-output');
const apiOutput = document.querySelector('#api-output');
const stateOutput = document.querySelector('#state-output');
const summary = document.querySelector('#summary');
const health = document.querySelector('#health');

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { json = text; }
  if (!response.ok) {
    const error = new Error(json?.message ?? `HTTP ${response.status}`);
    error.payload = json;
    error.status = response.status;
    throw error;
  }
  return json;
}

function show(output, value) {
  output.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

async function quiet(method, path, body) {
  try { return await api(method, path, body); }
  catch (error) { return { __error: error.message, ...(error.payload ?? {}) }; }
}

async function seed(verbose = true) {
  await api('POST', '/api/admin/reset', {});
  await api('POST', '/api/staff', { id: 'lin', name: '林师傅', canUnroll: true });
  await api('POST', '/api/chairs', { id: 'chair1', name: '烫卷椅' });
  await api('POST', '/api/chairs', { id: 'chair2', name: '刘海椅' });
  const booked = await api('POST', '/api/appointments', {
    id: 'perm',
    customerName: '第一位客人',
    signature: '第一位客人',
    mode: 'principal',
    principalStylistId: 'lin',
    sections: [{ id: 'last', name: '最后一区' }],
    plannedRollCompleteAt: 0,
    unroll: { durationMinutes: 2, minWaitMinutes: 3, maxWaitMinutes: 4 },
  });
  await api('POST', '/api/appointments/perm/check-in', { at: 0, chairId: 'chair1' });
  await api('POST', '/api/appointments/perm/sections/last/complete', { at: 0, stylistId: 'lin' });
  if (verbose) show(scenarioOutput, { seeded: true, bookingFeasibility: booked.feasibility });
  await refreshState();
  return booked;
}

const actions = {
  async seed() { await seed(); },
  async quote() {
    const result = await quiet('POST', '/api/quotes/insert', {
      stylistId: 'lin', durationMinutes: 3, availableFrom: 0, availableUntil: 3, chairId: 'chair2',
    });
    show(scenarioOutput, result);
  },
  async confirm() {
    const result = await quiet('POST', '/api/insertions', {
      customerName: '刘海客', signature: '刘海客', stylistId: 'lin', durationMinutes: 3,
      availableFrom: 0, availableUntil: 3, chairId: 'chair2', targetAppointmentId: 'perm',
    });
    show(scenarioOutput, result);
    await refreshState();
  },
  async 'restart-check'() {
    const receipt = await quiet('GET', '/api/appointments/perm/receipt');
    show(scenarioOutput, {
      note: '这是进程重启后从 data/salon.json 重新读出的客单；真实重启：Ctrl+C 后 npm start。',
      realized: receipt.segments?.filter((s) => s.state === 'realized').map((s) => s.segment),
      receipt,
    });
  },
  async early() {
    await seed(false);
    await quiet('POST', '/api/appointments/perm/sections/last/complete', { at: -2, stylistId: 'lin' });
    const quote = await quiet('POST', '/api/quotes/insert', {
      stylistId: 'lin', durationMinutes: 3, availableFrom: 0, availableUntil: 3, chairId: 'chair2',
    });
    show(scenarioOutput, { changed: '卷齐时间 = -2；拆卷窗 = [1,2]，最晚第 2 分必须回来', quote });
  },
  async chair() {
    await seed(false);
    await quiet('PATCH', '/api/chairs/chair2', { unavailable: [{ start: 0, end: 2, reason: '第 2 分才空' }] });
    const quote = await quiet('POST', '/api/quotes/insert', {
      stylistId: 'lin', durationMinutes: 3, availableFrom: 0, availableUntil: 3, chairId: 'chair2',
    });
    show(scenarioOutput, { changed: '工序回条恢复为 0；chair2 封闭 [0,2)', quote });
  },
  async zhou() {
    const result = await quiet('POST', '/api/staff', { id: 'zhou', name: '周师傅', canUnroll: true });
    show(scenarioOutput, result);
    await refreshState();
  },
  async recover() {
    const result = await quiet('POST', '/api/schedule/recover', { at: 3 });
    show(scenarioOutput, result);
  },
  async handoff() {
    const result = await quiet('POST', '/api/appointments/perm/handoffs', {
      at: 3, stylistId: 'zhou', confirmState: true, acceptCompletedSections: true,
      chairId: 'chair1', customerConsent: true, consentSignature: '第一位客人',
    });
    show(scenarioOutput, result);
  },
  async unroll() {
    const steps = [];
    steps.push(await quiet('POST', '/api/appointments/perm/unroll/start', { at: 3, stylistId: 'zhou' }));
    steps.push(await quiet('POST', '/api/appointments/perm/unroll/complete', { at: 5, stylistId: 'zhou' }));
    steps.push(await quiet('POST', '/api/appointments/perm/checkout', { at: 6, amount: 680, method: 'card' }));
    show(scenarioOutput, steps);
    await refreshState();
  },
  async 'refresh-state'() { await refreshState(); },
  async receipt() {
    const result = await quiet('GET', '/api/appointments/perm/receipt');
    show(stateOutput, result);
  },
};

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const action = button.dataset.action;
      if (actions[action]) await actions[action]();
    } catch (error) {
      show(scenarioOutput, { error: error.message, details: error.payload ?? null });
    } finally {
      button.disabled = false;
    }
  });
});

document.querySelector('#api-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const method = form.get('method');
  const path = form.get('path');
  try {
    const parsedBody = form.get('body') ? JSON.parse(form.get('body')) : undefined;
    const body = method === 'GET' || method === 'HEAD' ? undefined : parsedBody;
    show(apiOutput, await api(method, path, body));
  } catch (error) {
    show(apiOutput, { status: error.status, error: error.payload ?? error.message });
  }
});

async function refreshState() {
  const state = await quiet('GET', '/api/state');
  if (state.__error) {
    health.textContent = '服务不可用';
    return;
  }
  health.textContent = `正常 · ${state.staff.length} 位员工 · ${state.appointments.length} 张客单`;
  const active = state.appointments.filter((a) => a.status !== 'checked_out' && a.status !== 'cancelled');
  summary.innerHTML = `
    <div class="stat"><b>${state.staff.length}</b><span>员工</span></div>
    <div class="stat"><b>${state.chairs.length}</b><span>椅位</span></div>
    <div class="stat"><b>${state.appointments.length}</b><span>全部客单</span></div>
    <div class="stat"><b>${active.length}</b><span>在店/未结</span></div>`;
  show(stateOutput, state);
}

api('GET', '/api/health').then((h) => {
  health.textContent = `正常 · ${h.timeUnit}`;
}).catch(() => { health.textContent = '服务不可用'; });
refreshState();
