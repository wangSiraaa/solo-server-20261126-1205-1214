const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const terminalId = localStorage.getItem('stitchTerminal') || `terminal-${Math.random().toString(36).slice(2, 8)}`;
localStorage.setItem('stitchTerminal', terminalId);
$('#terminalBadge').textContent = `终端：${terminalId}`;

let orders = [];
let currentOrder = null;
let currentId = localStorage.getItem('stitchOrderId') || null;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

async function request(path, options = {}) {
  const headers = { 'content-type': 'application/json', ...(options.headers || {}) };
  if (options.requestId) headers['x-request-id'] = options.requestId;
  const res = await fetch(path, { ...options, headers });
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { raw: text }; }
  if (!res.ok || body?.ok === false) {
    const err = new Error(body?.message || `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function showMessage(text, kind = 'info', timeout = 7000) {
  const el = $('#message');
  el.hidden = false;
  el.className = `message ${kind}`;
  el.textContent = text;
  clearTimeout(showMessage.timer);
  if (timeout) showMessage.timer = setTimeout(() => { el.hidden = true; }, timeout);
}

function errText(err) {
  return `${err.message}\n${err.body ? JSON.stringify(err.body.violations || err.body.details || err.body, null, 2) : ''}`;
}

async function checkHealth() {
  try {
    await request('/api/health');
    $('#health').className = 'badge ok';
    $('#health').textContent = '服务在线 · 可断线重连';
  } catch {
    $('#health').className = 'badge bad';
    $('#health').textContent = '连不上服务';
  }
}

async function loadOrders(selectId) {
  const res = await request('/api/orders');
  orders = res.orders || [];
  renderOrderSelect();
  const wanted = selectId || currentId;
  if (wanted && orders.some((o) => o.id === wanted)) {
    await loadOrder(wanted);
  } else if (orders[0]) {
    await loadOrder(orders[0].id);
  } else {
    currentOrder = null;
    render();
  }
}

function renderOrderSelect() {
  $('#orderSelect').innerHTML = orders.map((o) =>
    `<option value="${esc(o.id)}" ${o.id === currentId ? 'selected' : ''}>${esc(o.name)}（${esc(o.id)}）</option>`
  ).join('');
  $('#orderList').innerHTML = orders.map((o) => `
    <button class="order-item ${o.id === currentId ? 'active' : ''}" data-order="${esc(o.id)}">
      <b>${esc(o.name)}</b>
      <small>${esc(o.id)} · v${o.designVersion} · ${o.completable ? '可完' : '不可完'} · ${o.complete ? '已完成' : '进行中'}</small>
    </button>`).join('');
  $$('[data-order]').forEach((btn) => btn.addEventListener('click', () => loadOrder(btn.dataset.order)));
}

async function loadOrder(id) {
  currentId = id;
  localStorage.setItem('stitchOrderId', id);
  currentOrder = await request(`/api/orders/${encodeURIComponent(id)}`);
  renderOrderSelect();
  render();
}

function pointKey(p) { return p ? `${p.row},${p.col}` : ''; }

function localConstraintPoints(points, seg) {
  return (points || []).map((p) => {
    if (p.segmentIndex !== undefined && p.segmentIndex !== seg.index && p.segmentIndex !== seg.id) return null;
    const col = p.col >= seg.offset && p.col < seg.offset + seg.cols ? p.col - seg.offset : p.col;
    if (col < 0 || col >= seg.cols || p.row < 0 || p.row >= seg.rows) {
      if (p.segmentIndex === undefined && p.col >= seg.offset && p.col < seg.offset + seg.cols) {
        return { row: p.row, col: p.col - seg.offset };
      }
      if (p.segmentIndex !== undefined) return { row: p.row, col: p.col };
      return null;
    }
    return { row: p.row, col };
  }).filter(Boolean);
}

function selectedLocalPath(order, segIndex) {
  const planSeg = order.plan?.segments?.[segIndex];
  if (planSeg?.localPaths?.red) return planSeg.localPaths.red;
  const view = order.segmentViews?.[segIndex];
  const ticket = order.tickets?.find((t) => t.id === view?.ticketId);
  if (ticket?.diagram?.localOrderedCells) return ticket.diagram.localOrderedCells;
  return order.board?.segments?.[segIndex]?.options?.[0]?.paths?.red || [];
}

function renderBoard() {
  const board = $('#board');
  if (!currentOrder) {
    board.innerHTML = '<p class="hint">请选择或载入一个绣单。</p>';
    return;
  }
  const o = currentOrder;
  board.innerHTML = (o.board?.segments || []).map((seg) => {
    const path = selectedLocalPath(o, seg.index);
    const pathIndex = new Map(path.map((p, i) => [pointKey(p), i]));
    const view = o.segmentViews?.[seg.index];
    const ticket = o.tickets?.find((t) => t.id === view?.ticketId);
    const stitched = ticket?.threads?.red?.stitched || [];
    const stitchedKeys = new Set(stitched.map((p) => `${p.localRow},${p.localCol}`));
    const nextLocal = ticket?.nextCell?.local;
    const forbidden = new Set(localConstraintPoints(o.forbidden, seg).map(pointKey));
    const marks = new Set(localConstraintPoints(o.marks, seg).map(pointKey));
    const leftRow = o.left?.red?.row ?? o.left?.['*']?.row;
    const rightRow = o.right?.red?.row ?? o.right?.['*']?.row;

    const rowsHtml = Array.from({ length: seg.rows }, (_, r) => {
      const cells = Array.from({ length: seg.cols }, (_, c) => {
        const key = `${r},${c}`;
        const idx = pathIndex.get(key);
        const classes = ['cell'];
        if (idx !== undefined) classes.push('path-red');
        if (stitchedKeys.has(key)) classes.push('stitched');
        if (nextLocal && pointKey(nextLocal) === key) classes.push('next');
        if (forbidden.has(key)) classes.push('forbidden');
        if (marks.has(key)) classes.push('mark');
        if ((c === 0 && leftRow === r) || (c === seg.cols - 1 && rightRow === r)) classes.push('endpoint');
        const label = forbidden.has(key) ? '×' : (idx !== undefined ? idx + 1 : '');
        return `<div class="${classes.join(' ')}" title="row ${r}, col ${c}">
          <span class="coord">${r},${c}</span>${esc(label)}
        </div>`;
      }).join('');
      return `<div class="grid-row">${cells}</div>`;
    }).join('');

    return `<div class="segment-board">
      <div class="segment-title"><b>#${seg.index + 1} ${esc(seg.id)}</b><span>v${view?.currentVersion ?? '?'} / 签${view?.issuedVersion ?? '—'}</span></div>
      ${rowsHtml}
    </div>`;
  }).join('');

  $('#seams').innerHTML = (o.seams || []).map((s) =>
    `<span class="badge ${s.compatible ? 'ok' : 'bad'}">接缝 ${s.between[0] + 1}→${s.between[1] + 2}：${esc(s.color)} 第${s.row + 1}行 ${s.compatible ? '相容' : '错位'}</span>`
  ).join('');
}

function statusBadge(status) {
  const map = {
    issued: ['info', '已签发'], in_progress: ['warn', '刺绣中'], completed: ['ok', '已到出口'],
    returned: ['bad', '已交回改样'], superseded: ['muted', '已换发'], unissued: ['muted', '未签发']
  };
  const [cls, label] = map[status] || ['muted', status];
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderCards() {
  const wrap = $('#segmentCards');
  if (!currentOrder) { wrap.innerHTML = ''; return; }
  const o = currentOrder;
  wrap.innerHTML = (o.segmentViews || []).map((sv) => {
    const ticket = o.tickets?.find((t) => t.id === sv.ticketId) || null;
    const next = ticket?.nextCell;
    const actions = [];
    if (!ticket || ticket.status === 'superseded') {
      actions.push(`<button data-act="claim" data-i="${sv.index}" data-route="">领取针单</button>`);
      actions.push(`<button class="secondary" data-act="claim" data-i="${sv.index}" data-route="straight">只签直走</button>`);
      actions.push(`<button class="secondary" data-act="claim" data-i="${sv.index}" data-route="switch">只签换行</button>`);
    } else if (ticket.status === 'returned') {
      actions.push(`<button data-act="claim" data-i="${sv.index}" data-route="">接回旧针并换发</button>`);
    } else if (ticket.status === 'issued' || ticket.status === 'in_progress') {
      actions.push(`<button data-act="report" data-t="${ticket.id}">确认下一格</button>`);
      actions.push(`<button class="danger-ghost" data-act="return" data-t="${ticket.id}">交回此格改样</button>`);
    }
    return `<article class="card">
      <h4><span>#${sv.index + 1} ${esc(sv.segmentId)}</span>${statusBadge(sv.status)}</h4>
      <dl class="kv">
        <dt>工单</dt><dd>${esc(sv.ticketId || '—')}</dd>
        <dt>领绷人</dt><dd>${esc(sv.worker || '—')}</dd>
        <dt>设计版本</dt><dd>段 v${sv.currentVersion}；签发段版本 ${sv.issuedSegmentVersion ?? '—'}</dd>
        <dt>签发版本</dt><dd>${sv.issuedVersion ? `第 ${sv.issuedVersion} 单 / ticket v${ticket?.version}` : '未领取'}</dd>
        <dt>落针</dt><dd>${sv.stitchedCount}/${sv.totalCells}${next ? `；下一格 (${next.row},${next.col})` : ''}</dd>
        <dt>针路</dt><dd>${esc(sv.optionKey || '—')}</dd>
      </dl>
      <div class="card-actions">${actions.join('')}</div>
    </article>`;
  }).join('');

  $$('[data-act]', wrap).forEach((btn) => btn.addEventListener('click', onCardAction));
  renderTargetSelects();
}

function renderTargetSelects() {
  if (!currentOrder) return;
  const target = $('#changeTarget');
  const previousTarget = target.value;
  const options = (currentOrder.segmentViews || []).map((s) =>
    `<option value="${s.index}">第${s.index + 1}段 · ${esc(s.segmentId)}（${s.status}）</option>`).join('');
  target.innerHTML = options;
  const wantedTarget = previousTarget && [...target.options].some((o) => o.value === previousTarget)
    ? previousTarget
    : String((currentOrder.segmentViews?.length || 1) - 1);
  target.value = wantedTarget;

  const race = $('#raceTicketSelect');
  const previousRace = race.value;
  const active = (currentOrder.tickets || []).filter((t) => ['issued', 'in_progress'].includes(t.status));
  race.innerHTML = active.map((t) =>
    `<option value="${t.id}">第${t.segmentIndex + 1}段 · ${t.id} · v${t.version}</option>`).join('') ||
    '<option value="">没有进行中的针单</option>';
  if (previousRace && [...race.options].some((o) => o.value === previousRace)) race.value = previousRace;
}

function renderBadges() {
  if (!currentOrder) return;
  const o = currentOrder;
  $('#orderTitle').textContent = o.name;
  $('#orderBadges').innerHTML = [
    `<span class="badge info">${o.mode === 'relay' ? '接力办社' : '并行小绷'}</span>`,
    `<span class="badge">设计 v${o.designVersion}</span>`,
    `<span class="badge ${o.completable ? 'ok' : 'bad'}">${o.completable ? '边界相容 · 可达成订单' : '不可宣布可完'}</span>`,
    `<span class="badge ${o.complete ? 'ok' : 'warn'}">${o.complete ? '全带完成' : '尚未全带交回'}</span>`,
    ...(o.violations || []).slice(0, 3).map((v) => `<span class="badge bad">${esc(v.code)}</span>`)
  ].join('');
}

function render() {
  renderOrderSelect();
  renderBadges();
  renderBoard();
  renderCards();
}

async function onCardAction(ev) {
  const btn = ev.currentTarget;
  try {
    if (btn.dataset.act === 'claim') {
      const i = Number(btn.dataset.i);
      const route = btn.dataset.route;
      const body = { segmentIndex: i, workerId: $('#workerId').value || terminalId, terminalId };
      if (route) body.routeId = route;
      const res = await request(`/api/orders/${currentId}/claim`, { method: 'POST', body: JSON.stringify(body) });
      showMessage(`已签发针单 ${res.ticket.id}，版本 v${res.ticket.version}。针图含入口、出口、相邻格顺序和接缝。`, 'ok');
    }
    if (btn.dataset.act === 'report') {
      const ticket = currentOrder.tickets.find((t) => t.id === btn.dataset.t);
      const res = await request(`/api/tickets/${ticket.id}/report`, {
        method: 'POST', body: JSON.stringify({ expectedVersion: ticket.version, terminalId })
      });
      showMessage(`服务已受理：${ticket.id} 前进到 v${res.version}。`, 'ok');
    }
    if (btn.dataset.act === 'return') {
      const ticket = currentOrder.tickets.find((t) => t.id === btn.dataset.t);
      const res = await request(`/api/tickets/${ticket.id}/return`, {
        method: 'POST',
        body: JSON.stringify({ expectedVersion: ticket.version, cell: ticket.nextCell, terminalId, reason: '网站改样' })
      });
      showMessage(`已交回 ${ticket.nextCell ? `(${ticket.nextCell.row},${ticket.nextCell.col}) ` : ''}这一格及未刺余单；已刺部分保留。新版本 v${res.version}。`, 'ok');
    }
    await loadOrder(currentId);
  } catch (err) {
    showMessage(errText(err), 'err', 12000);
    await loadOrder(currentId).catch(() => {});
  }
}

async function init() {
  $('#createJson').value = JSON.stringify({
    id: `lan-order-${Date.now().toString(36)}`,
    name: '局域网三段样带',
    rows: 2,
    left: { red: 0 },
    right: { red: 0 },
    marks: [],
    forbidden: [],
    segments: [
      { id: 'a', rows: 2, cols: 2, threads: { red: { path: [[0, 0], [0, 1]] } } },
      { id: 'b', rows: 2, cols: 2, threads: { red: { path: [[0, 0], [0, 1]] } } },
      { id: 'c', rows: 2, cols: 2, threads: { red: { path: [[0, 0], [0, 1]] } } }
    ]
  }, null, 2);
  $('#segmentsJson').value = JSON.stringify([
    { id: 'new-a', rows: 2, cols: 2, threads: { red: { path: [[0, 0], [0, 1]] } } },
    { id: 'new-b', rows: 2, cols: 2, threads: { red: { path: [[0, 0], [0, 1]] } } },
    { id: 'new-c', rows: 2, cols: 2, threads: { red: { path: [[0, 0], [0, 1]] } } }
  ], null, 2);

  $('#orderSelect').addEventListener('change', (e) => loadOrder(e.target.value));
  $('#refreshBtn').addEventListener('click', async () => { await checkHealth(); await loadOrders(currentId); });
  $('#resetBtn').addEventListener('click', async () => {
    await request('/api/reset', { method: 'POST', body: '{}' });
    localStorage.removeItem('stitchOrderId');
    currentId = null;
    await loadOrders();
    showMessage('服务端状态已清空。', 'ok');
  });

  $$('[data-scenario]').forEach((btn) => btn.addEventListener('click', async () => {
    try {
      const kind = btn.dataset.scenario;
      const res = await request(`/api/scenarios/${kind}`, { method: 'POST', body: '{}' });
      await loadOrders(kind === 'red-band' ? 'red-site-straight' : res.orderId);
      if (kind === 'red-band') {
        showMessage('已建立三单：red-order-a（徽记迫使换行）、red-site-straight、red-site-switch。右侧同一笔 tail straightOnly 改样可分别提交。', 'info', 12000);
      } else {
        showMessage(`已换入三段新样：${res.orderId}`, 'ok');
      }
    } catch (err) { showMessage(errText(err), 'err', 12000); }
  }));

  $('#createOrderBtn').addEventListener('click', async () => {
    try {
      const body = JSON.parse($('#createJson').value);
      const res = await request('/api/orders', { method: 'POST', body: JSON.stringify(body) });
      await loadOrders(res.order.id);
      showMessage('绣单已创建。', 'ok');
    } catch (err) { showMessage(errText(err), 'err', 12000); }
  });

  $('#changeTarget').addEventListener('change', () => { $('#changeTarget').dataset.touched = '1'; });

  function changeBody() {
    return {
      segmentIndex: Number($('#changeTarget').value),
      straightOnly: $('#straightOnly').checked,
      note: $('#changeNote').value
    };
  }

  $('#saveDraftBtn').addEventListener('click', async () => {
    try {
      const res = await request(`/api/orders/${currentId}/design`, { method: 'PATCH', body: JSON.stringify(changeBody()) });
      showMessage(`图样已保存到 v${res.designVersion}；completable=${res.completable}。`, res.completable ? 'ok' : 'err', 12000);
      await loadOrder(currentId);
    } catch (err) { showMessage(errText(err), 'err', 12000); await loadOrder(currentId).catch(() => {}); }
  });

  $('#applyChangeBtn').addEventListener('click', async () => {
    try {
      const res = await request(`/api/orders/${currentId}/change-requests`, { method: 'POST', body: JSON.stringify(changeBody()) });
      showMessage(`改样申请已受理，生成设计 v${res.designVersion}。`, 'ok');
      await loadOrder(currentId);
    } catch (err) {
      showMessage(`改样申请被服务拒绝：\n${errText(err)}`, 'err', 14000);
      await loadOrder(currentId).catch(() => {});
    }
  });

  $('#restoreTailBtn').addEventListener('click', async () => {
    try {
      const res = await request(`/api/orders/${currentId}/design`, {
        method: 'PATCH',
        body: JSON.stringify({ target: 'tail', segmentIndex: (currentOrder.segmentViews?.length || 1) - 1, restore: true, straightOnly: false, note: '还原尾段' })
      });
      showMessage(`尾段限制已还原；completable=${res.completable}。`, res.completable ? 'ok' : 'err');
      await loadOrder(currentId);
    } catch (err) { showMessage(errText(err), 'err', 12000); }
  });

  $('#replaceSegmentsBtn').addEventListener('click', async () => {
    try {
      const segments = JSON.parse($('#segmentsJson').value);
      const res = await request(`/api/orders/${currentId}/segments/replace`, {
        method: 'POST', body: JSON.stringify({ segments })
      });
      showMessage(`已换入 ${segments.length} 段；completable=${res.completable}。`, res.completable ? 'ok' : 'err');
      await loadOrder(currentId);
    } catch (err) { showMessage(errText(err), 'err', 12000); }
  });

  $('#showPathBtn').addEventListener('click', async () => {
    try {
      const res = await request(`/api/orders/${currentId}/full-path`);
      $('#pathOutput').textContent = JSON.stringify({
        fullPath: res.fullPath,
        actualPath: res.actualPath,
        issuedVersions: res.issuedVersions,
        seams: res.seams
      }, null, 2);
    } catch (err) { $('#pathOutput').textContent = errText(err); }
  });

  $('#raceBtn').addEventListener('click', async () => {
    const ticketId = $('#raceTicketSelect').value;
    if (!ticketId || !currentOrder) return showMessage('请先选择一张进行中的针单。', 'err');
    const ticket = currentOrder.tickets.find((t) => t.id === ticketId);
    const baseVersion = ticket.version;
    const cell = ticket.nextCell;
    $('#raceResult').textContent = `两个请求都携带相同 reportVersion=${baseVersion}…\n等待服务按实际顺序受理。`;
    const outcomes = await Promise.allSettled([
      request(`/api/tickets/${ticketId}/report`, {
        method: 'POST',
        body: JSON.stringify({ expectedVersion: baseVersion, terminalId: `${terminalId}-A` })
      }).then((x) => ({ action: 'confirm', status: 200, version: x.version })),
      request(`/api/tickets/${ticketId}/return`, {
        method: 'POST',
        body: JSON.stringify({ expectedVersion: baseVersion, cell, terminalId: `${terminalId}-B` })
      }).then((x) => ({ action: 'return', status: 200, version: x.version }))
    ]);
    const formatted = outcomes.map((o) => o.status === 'fulfilled'
      ? { winner: true, ...o.value }
      : { winner: false, status: o.reason.status, error: o.reason.body?.error, message: o.reason.message, currentVersion: o.reason.body?.details?.currentVersion })
      ;
    $('#raceResult').textContent = JSON.stringify(formatted, null, 2);
    const winner = formatted.find((x) => x.winner);
    showMessage(winner ? `服务实际先受理：${winner.action}；另一动作因版本冲突未生效。` : '两个动作都未生效。', winner ? 'ok' : 'err');
    await loadOrder(currentId);
  });

  await checkHealth();
  try { await loadOrders(); } catch (err) { showMessage(errText(err), 'err'); }
  setInterval(checkHealth, 4000);
  setInterval(async () => {
    if (!document.hidden && currentId) {
      try {
        currentOrder = await request(`/api/orders/${encodeURIComponent(currentId)}`);
        render();
      } catch { /* health check reports */ }
    }
  }, 2500);
}

init();
