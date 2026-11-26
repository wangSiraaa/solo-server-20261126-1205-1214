const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || 'GET',
    headers: { 'content-type': 'application/json' },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  $('#output').textContent = `${options.method || 'GET'} ${path}\n→ ${response.status}\n\n${JSON.stringify(data, null, 2)}`;
  if (!response.ok) throw new Error(data?.error?.message || `${response.status}`);
  return data;
}

function values(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function intervalFrom(values, prefix = '') {
  const start = Number(values[`${prefix}start`] ?? values.start);
  const end = Number(values[`${prefix}end`] ?? values.end);
  return [start, end];
}

function setStory(message, kind = '') {
  const el = $('#storyResult');
  el.className = `notice ${kind}`;
  el.innerHTML = message;
}

function esc(value) {
  return String(value ?? '').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

async function refresh() {
  try {
    const health = await fetch('/health').then((r) => r.json());
    $('#clock').textContent = health.logicalClock;
    $('#stationCount').textContent = health.stationCount;
    $('#reportCount').textContent = health.reportCount;

    const stations = await fetch('/api/stations').then((r) => r.json());
    $('#stations').innerHTML = `<table><tbody>${stations.items.map((s) => `
      <tr>
        <td><b>${esc(s.id)}</b><br><span class="muted">${esc(s.name)}</span></td>
        <td><span class="badge ${s.status === 'online' ? 'good' : 'bad'}">${esc(s.status)}</span></td>
        <td>偏差 ${esc(s.clock.agreedOffset)}<br><span class="muted">${esc(s.clock.syncs.length)} 次对时</span></td>
      </tr>`).join('')}</tbody></table>`;

    const commissions = await fetch('/api/commissions').then((r) => r.json());
    $('#commissions').innerHTML = commissions.items.map((c) => `
      <tr>
        <td><b>${esc(c.id)}</b><br><span class="muted">${esc(c.title)}</span></td>
        <td><span class="badge ${c.status === 'sealed' ? 'good' : c.status === 'pending_makeup' ? 'warn' : 'muted'}">${esc(c.status)}</span></td>
        <td>${esc(c.requiredCommonSeconds)}s<br><span class="interval muted">[${esc(c.phenomenonInterval.start)},${esc(c.phenomenonInterval.end)})</span></td>
        <td><a href="/reader.html?commissionId=${encodeURIComponent(c.id)}">追踪</a></td>
      </tr>`).join('');

    const reports = await fetch('/api/reports').then((r) => r.json());
    $('#reports').innerHTML = reports.items.map((r) => `
      <tr><td><a href="/api/reports/${encodeURIComponent(r.id)}">${esc(r.id)}</a></td><td>${esc(r.commissionId)}</td><td>v${esc(r.version)}${r.original ? ' 原版' : ''}</td><td>${esc(r.commonSeconds)}s</td></tr>
    `).join('');

    const events = await fetch('/api/events').then((r) => r.json());
    $('#events').innerHTML = events.items.slice(-18).reverse().map((e) => `
      <div class="event"><b>L${esc(e.logicalClock)}</b> ${esc(e.type)} <span class="muted">${esc(e.actor || '')}</span>
        <div class="muted">${esc(JSON.stringify(e.payload))}</div>
      </div>`).join('');
  } catch (err) {
    setStory(`刷新失败：${esc(err.message)}`, 'bad');
  }
}

$('#refreshAll').addEventListener('click', refresh);

$('#clockForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const v = values(event.target);
  await api(`/api/stations/${encodeURIComponent(v.stationId)}/clock`, {
    method: 'PUT',
    body: { agreedOffset: Number(v.agreedOffset) },
  });
  await refresh();
});

$('#syncForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const v = values(event.target);
  await api(`/api/stations/${encodeURIComponent(v.stationId)}/sync`, {
    method: 'POST',
    body: { localSecond: Number(v.localSecond), commonSecond: Number(v.commonSecond) },
  });
  await refresh();
});

$('#statusForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const v = values(event.target);
  const path = v.status === 'lost' ? 'lost' : 'online';
  await api(`/api/stations/${encodeURIComponent(v.stationId)}/${path}`, { method: 'POST', body: {} });
  await refresh();
});

$('#frameForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const v = values(event.target);
  let droppedIntervals = [];
  try {
    droppedIntervals = JSON.parse(v.droppedIntervals || '[]');
  } catch {
    setStory('丢帧空段不是合法 JSON', 'bad');
    return;
  }
  const body = {
    id: v.id || undefined,
    mode: v.mode,
    commissionId: v.commissionId || undefined,
    localInterval: [Number(v.start), Number(v.end)],
    droppedIntervals,
  };
  await api(`/api/stations/${encodeURIComponent(v.stationId)}/frames`, { method: 'POST', body });
  await refresh();
});

$('#commissionForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const v = values(event.target);
  await api('/api/commissions', {
    method: 'POST',
    body: {
      id: v.id,
      stationIds: v.stationIds.split(',').map((s) => s.trim()).filter(Boolean),
      phenomenonInterval: intervalFrom(v),
      requiredCommonSeconds: Number(v.requiredCommonSeconds),
      background: { before: Number(v.before), after: Number(v.after) },
    },
  });
  await refresh();
});

$('#evaluateBtn').addEventListener('click', async () => {
  const id = new FormData($('#closeForm')).get('commissionId');
  const result = await api(`/api/commissions/${encodeURIComponent(id)}/evaluate`, { method: 'POST', body: {} });
  const reasons = result.reasonMessages?.length ? result.reasonMessages.join('；') : (result.reasons.join(', ') || '无');
  setStory(`共同秒 <b>${result.commonSeconds}</b>，背景支撑 <b>${result.backgroundSupportedSeconds}</b>，qualified=<b>${result.qualified}</b>；判定：${esc(reasons)}`, result.qualified ? 'good' : 'warn');
});

$('#closeForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const id = new FormData(event.target).get('commissionId');
  const result = await api(`/api/commissions/${encodeURIComponent(id)}/close`, { method: 'POST', body: { host: '网页主持人' } });
  if (result.report) {
    setStory(`已封交 <a href="/api/reports/${result.report.id}">${result.report.id}</a>；原版报告继续可读。`, 'good');
  } else {
    const reasons = result.evaluation.reasonMessages?.length
      ? result.evaluation.reasonMessages.join('；')
      : result.evaluation.reasons.join(', ');
    setStory(`${id} 判定为 <b>${result.result}</b>：${esc(reasons)}。补看窗口数 ${result.makeupPlan.candidateConfirmedWindows.length}。`, 'warn');
  }
  await refresh();
});

$('#closeForm input[name="commissionId"]').addEventListener('input', (event) => {
  $('#readerLink').href = `/reader.html?commissionId=${encodeURIComponent(event.target.value)}`;
});

$('#windowDispatchForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const v = values(event.target);
  try {
    const receipt = await api('/api/dispatch/window', { method: 'POST', body: v });
    setStory(`派班回执 ${receipt.id} 已发出，时段 ${JSON.stringify(receipt.interval)}，不可转让。`, 'good');
  } catch (err) {
    setStory(`派班被拒绝：${esc(err.message)}`, 'bad');
  }
  await refresh();
});

$('#relayForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const v = values(event.target);
  const body = {
    stationId: v.stationId,
    forecast: {
      sentAt: Number(v.sentAt),
      predictedInterval: [Number(v.forecastStart), Number(v.forecastEnd)],
      knownUntil: Number(v.sentAt),
    },
    notificationDelaySeconds: Number(v.notificationDelaySeconds),
    receiptDelaySeconds: Number(v.receiptDelaySeconds),
    slewSeconds: Number(v.slewSeconds),
    availableInterval: [Number(v.availStart), Number(v.availEnd)],
  };
  try {
    const receipt = await api('/api/relay/dispatch', { method: 'POST', body });
    setStory(`接力回执 ${receipt.id} 可赶段 ${JSON.stringify(receipt.interval)}；未使用过点后的完整轨迹。`, 'good');
  } catch (err) {
    setStory(`接力派班失败：${esc(err.message)}`, 'bad');
  }
});

document.querySelector('[data-action="eval-lab"]').addEventListener('click', async () => {
  const e = await api('/api/commissions/C-LAB/evaluate', { method: 'POST', body: {} });
  setStory(`初始：共同秒 ${e.commonSeconds}，交集 ${JSON.stringify(e.commonIntervals)}，合格 ${e.qualified}。`, 'good');
});
document.querySelector('[data-action="shift-s2"]').addEventListener('click', async () => {
  await api('/api/stations/S2/clock', { method: 'PUT', body: { agreedOffset: 1 } });
  const e = await api('/api/commissions/C-LAB/evaluate', { method: 'POST', body: {} });
  const s2Mapped = e.stationEvidence[1].targetFrameEvidence[0]?.commonNominalInterval;
  setStory(`S2 曝光映射为 ${JSON.stringify(s2Mapped)}；委托段内共同交集 ${JSON.stringify(e.commonIntervals)}，共 <b>${e.commonSeconds}</b> 秒。`, 'warn');
  await refresh();
});
document.querySelector('[data-action="restore-s2"]').addEventListener('click', async () => {
  await api('/api/stations/S2/clock', { method: 'PUT', body: { agreedOffset: 0 } });
  const e = await api('/api/commissions/C-LAB/evaluate', { method: 'POST', body: {} });
  setStory(`时钟恢复后共同秒：<b>${e.commonSeconds}</b>，合格 ${e.qualified}。`, 'good');
  await refresh();
});
document.querySelector('[data-action="require-four"]').addEventListener('click', async () => {
  await api('/api/commissions/C-LAB', { method: 'PATCH', body: { requiredCommonSeconds: 4 } });
  const e = await api('/api/commissions/C-LAB/evaluate', { method: 'POST', body: {} });
  setStory(`要求改为 4 后重新计算：实际仍只有 ${e.commonSeconds} 秒，qualified=${e.qualified}。旧的 3 秒结论未被沿用。`, 'bad');
  await refresh();
});
document.querySelector('[data-action="trace-stagger"]').addEventListener('click', async () => {
  const t = await api('/api/commissions/C-STAGGER/trace');
  setStory(`S1/S2 各自总曝光 ${t.stationEvidence.reduce((n, s) => n + s.targetIntervals.reduce((m, i) => m + (i.end - i.start), 0), 0)} 秒，但共同秒 <b>${t.commonSeconds}</b>；不能相加。<a href="/reader.html?commissionId=C-STAGGER">打开读片员</a>`, 'warn');
});
document.querySelector('[data-action="repeat-frame"]').addEventListener('click', async () => {
  const first = await api('/api/stations/S4/frames', { method: 'POST', body: { id: 'F-FOUR-S4', commissionId: 'C-FOUR', mode: 'target', localInterval: [41, 43] } });
  const second = await api('/api/stations/S4/frames', { method: 'POST', body: { id: 'F-FOUR-S4', commissionId: 'C-FOUR', mode: 'target', localInterval: [41, 43] } });
  const report = await api('/api/reports/RPT-C-FOUR-V1');
  setStory(`同一帧已第 ${second.deliveries} 次送达（duplicate=${second.duplicate}）；原版报告 ${report.id} 仍 sealed，校验 ${report.checksum.slice(0, 12)}… 可读。`, 'good');
  await refresh();
});
document.querySelector('[data-action="race"]').addEventListener('click', async () => {
  const results = await Promise.allSettled([
    api('/api/dispatch/window', { method: 'POST', body: { commissionId: 'C-RACE-A', windowId: 'W-RACE-50-54' } }),
    api('/api/dispatch/window', { method: 'POST', body: { commissionId: 'C-RACE-B', windowId: 'W-RACE-50-54' } }),
  ]);
  const summary = results.map((r, i) => `${i === 0 ? 'A' : 'B'}: ${r.status === 'fulfilled' ? '成功' : r.reason.message}`).join('；');
  setStory(`并发抢同一窗口结果：${esc(summary)}。恰好一单成功，另一单收到 SLOT_CONFLICT。`, 'warn');
  await refresh();
});
document.querySelector('[data-action="reset"]').addEventListener('click', async () => {
  await api('/api/admin/reset', { method: 'POST', body: { seed: true } });
  setStory('已恢复内置剧本。', 'good');
  await refresh();
});

refresh();
setInterval(refresh, 5000);
