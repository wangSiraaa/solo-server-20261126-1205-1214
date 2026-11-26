const $ = (sel) => document.querySelector(sel);

function esc(value) {
  if (value === null || value === undefined) return '—';
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function interval(iv) {
  if (!iv) return '—';
  const start = Number.isInteger(iv.start) ? iv.start : Number(iv.start).toFixed(2);
  const end = Number.isInteger(iv.end) ? iv.end : Number(iv.end).toFixed(2);
  return `<span class="interval">[${esc(start)},${esc(end)})</span>`;
}

function badge(ok, goodText = '合格', badText = '不足') {
  return ok
    ? `<span class="badge good">${esc(goodText)}</span>`
    : `<span class="badge bad">${esc(badText)}</span>`;
}

const BACKGROUND_REASON_TEXT = {
  TOO_FAR: '背景超出前后距离',
  NO_BACKGROUND: '没有背景帧',
  NO_BACKGROUND_FOR_SEGMENT: '该秒段找不到对应背景',
  BACKGROUND_OVERLAPS_EXPOSURE: '背景与曝光重叠',
  NO_BEFORE_POLICY: '未规定提前距离',
  NO_AFTER_POLICY: '未规定延后距离',
};

async function getJson(url) {
  const res = await fetch(url);
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(data?.error?.message || `${res.status} ${res.statusText}`);
  return data;
}

function renderSummary(trace) {
  const c = trace.commission;
  const enough = trace.commonSeconds >= c.requiredCommonSeconds;
  const backgroundSupportedSeconds = trace.commonSegments
    .filter((segment) => segment.backgroundSupported)
    .reduce((sum, segment) => sum + segment.seconds, 0);
  return `
    <section class="card cols-2" style="grid-column:1/-1">
      <div class="grid cols-2">
        <div>
          <h2>${esc(c.id)} · ${esc(c.title || '')}</h2>
          <p class="muted">委托状态 <span class="badge ${c.status === 'sealed' ? 'good' : c.status === 'pending_makeup' ? 'warn' : 'muted'}">${esc(c.status)}</span>
             条件版本 v${esc(c.requirementsVersion)} · 逻辑钟 ${esc(trace.logicalClock)}</p>
          <p>天象区间 ${interval(c.phenomenonInterval)}；要求 <b>${esc(c.requiredCommonSeconds)}</b> 秒多站共同可用。</p>
          <p>站点组合：${c.stationIds.map((s) => `<span class="badge muted">${esc(s)}</span>`).join(' ')}</p>
          <p>背景前后距离策略：<code>${esc(JSON.stringify(c.backgroundPolicy || '不限制'))}</code></p>
        </div>
        <div>
          <div class="kpi ${enough ? 'good' : 'bad'}">${esc(trace.commonSeconds)} 秒</div>
          <p>当前共同可用秒 / 要求 ${esc(c.requiredCommonSeconds)} 秒</p>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, (trace.commonSeconds / c.requiredCommonSeconds) * 100)}%"></div></div>
          <p class="muted">背景可支撑共同秒：${esc(backgroundSupportedSeconds)} 秒</p>
        </div>
      </div>
      <div class="notice ${enough ? 'good' : 'warn'}">
        ${enough ? '当前实到记录可算出足够共视；是否已封交以报告区为准。' : '当前实到共同秒数不足或背景不合规；计划表只能用于补看，不能冒充实测。'}
      </div>
    </section>`;
}

function renderCommonSegments(trace) {
  const rows = trace.commonSegments.length ? trace.commonSegments.map((seg) => `
    <tr>
      <td>${interval(seg.interval)}</td>
      <td>${esc(seg.seconds)}</td>
      <td>${badge(seg.backgroundSupported, '各站背景可用', '背景不合格')}</td>
      <td>${seg.stationChecks.map((chk) => `
        <div>
          <b>${esc(chk.stationId)}</b>
          ${badge(
            chk.backgroundEligible,
            `${esc(chk.side === 'before' ? '前' : chk.side === 'after' ? '后' : chk.side)} Δ${esc(chk.distance)}s`,
            esc(BACKGROUND_REASON_TEXT[chk.reason] || chk.reason),
          )}
          <span class="muted">背景帧 ${esc(chk.backgroundFrameId || '无')}；曝光帧 ${esc(chk.exposureFrameId || '无')}</span>
        </div>`).join('')}
      </td>
    </tr>`).join('') : `
    <tr><td colspan="4" class="notice warn">没有任何多站交集。各站自己的三秒不能相加凑成联测。</td></tr>`;

  return `
    <section class="card" style="grid-column:1/-1">
      <h2>① 共同秒段</h2>
      <table>
        <thead><tr><th>公共秒区间</th><th>秒数</th><th>背景</th><th>逐站背景判定</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function renderStations(trace) {
  return trace.stationEvidence.map((s) => `
    <section class="card">
      <h2>② ${esc(s.stationId)} · ${esc(s.stationName)}</h2>
      <p>状态 <span class="badge ${s.status === 'online' ? 'good' : s.status === 'offline' ? 'bad' : 'warn'}">${esc(s.status)}</span>
         对时校准 ${badge(s.clockCalibrated, `两次以上 · 偏差 ${esc(s.clock.agreedOffset)}`, '缺少两次对时')}</p>
      <p class="muted">映射公式 <code>${esc(s.clock.formula)}</code>；对时记录 ${esc(s.clock.syncs.length)} 条。</p>
      <h3>合格目标曝光（已映射/挖空）</h3>
      ${s.targetFrameEvidence.length ? s.targetFrameEvidence.map((f) => `
        <div class="notice">
          <div><b>${esc(f.frameId)}</b> · 本地 ${interval(f.localInterval)} → 公共 nominal ${interval(f.commonNominalInterval)}</div>
          <div>丢帧空段：${f.mappedDroppedIntervals.length ? f.mappedDroppedIntervals.map(interval).join(' ') : '无'}</div>
          <div>可用段：${f.usableIntervals.length ? f.usableIntervals.map(interval).join(' ') : '无'}</div>
          <div class="muted">交帧次数：${esc(f.deliveries)}</div>
        </div>`).join('') : '<p class="notice bad">没有可计入的实到目标帧。</p>'}
      <h3>背景标定</h3>
      ${s.backgrounds.length ? s.backgrounds.map((b) => `
        <div>${esc(b.frameId)}：本地 ${interval(b.localInterval)} → 公共 ${interval(b.commonInterval)}</div>`).join('') : '<p class="notice warn">没有背景帧。</p>'}
    </section>`).join('');
}

function renderDispatches(trace) {
  const items = trace.dispatchReceipts.length ? trace.dispatchReceipts.map((d) => `
    <div class="notice">
      <b>${esc(d.id)}</b> <span class="badge muted">${esc(d.mode)}</span>
      <div>委托：${esc(d.commissionId)}；站：${(d.stationIds || []).map(esc).join(', ')}；时段 ${interval(d.interval)}</div>
      <div>不可转让：${esc(d.nonTransferable ? '是' : '否')}；状态 <b>${esc(d.status)}</b></div>
      ${d.plan ? `<pre class="json">${esc(JSON.stringify(d.plan.timeline, null, 2))}</pre>` : ''}
      <details><summary>回执明细</summary><pre class="json">${esc(JSON.stringify(d.receipts || d, null, 2))}</pre></details>
    </div>`).join('') : '<p class="muted">没有派班回执。</p>';
  return `
    <section class="card" style="grid-column:1/-1">
      <h2>③ 派班 / 接力回执</h2>
      ${items}
    </section>`;
}

function renderReports(trace) {
  const items = trace.reports.length ? trace.reports.map((r) => `
    <div class="notice good">
      <b>${esc(r.id)}</b> <span class="badge good">已封交 · v${esc(r.version)}${r.original ? ' · 原版' : ''}</span>
      <div>封交逻辑钟：${esc(r.sealedAtLogical)}；共同秒：${esc(r.evaluation.commonSeconds)}</div>
      <div class="muted">校验：<code>${esc(r.checksum)}</code></div>
      <details><summary>阅读报告全文</summary><pre class="json">${esc(JSON.stringify(r, null, 2))}</pre></details>
    </div>`).join('') : '<p class="notice warn">尚未形成封交成功报告；若主持人已结案，可能是待补记录。</p>';

  const makeup = trace.makeupPlan ? `
    <h3>补看计划（不是实测）</h3>
    <div class="notice warn">${esc(trace.makeupPlan.rule)}</div>
    <pre class="json">${esc(JSON.stringify(trace.makeupPlan, null, 2))}</pre>` : '';

  return `
    <section class="card" style="grid-column:1/-1">
      <h2>④ 封交原版报告与补看计划</h2>
      ${items}
      ${makeup}
    </section>`;
}

async function loadList() {
  try {
    const data = await getJson('/api/commissions');
    $('#commissionIds').innerHTML = data.items.map((c) => `<option value="${esc(c.id)}">${esc(c.title || c.id)}</option>`).join('');
  } catch { /* datalist is optional */ }
}

async function load(id) {
  const status = $('#status');
  const result = $('#result');
  $('#jsonLink').href = `/api/commissions/${encodeURIComponent(id)}/trace`;
  status.className = 'notice';
  status.textContent = '正在从实到记录、背景和回执重建追踪……';
  result.innerHTML = '';
  try {
    const trace = await getJson(`/api/commissions/${encodeURIComponent(id)}/trace`);
    status.className = 'notice good';
    status.textContent = `已找到委托 ${id}；当前逻辑钟 ${trace.logicalClock}。每次打开都重新计算，不使用旧结论缓存。`;
    result.innerHTML = [
      renderSummary(trace),
      renderCommonSegments(trace),
      renderStations(trace),
      renderDispatches(trace),
      renderReports(trace),
      `<section class="card" style="grid-column:1/-1"><h2>原始追踪 JSON</h2><pre class="json">${esc(JSON.stringify(trace, null, 2))}</pre></section>`,
    ].join('');
  } catch (err) {
    status.className = 'notice bad';
    status.textContent = err.message;
  }
}

$('#lookup').addEventListener('submit', (event) => {
  event.preventDefault();
  const id = $('#commissionId').value.trim();
  const url = new URL(location.href);
  url.searchParams.set('commissionId', id);
  history.replaceState({}, '', url);
  load(id);
});

loadList();
const pathMatch = location.pathname.match(/\/reader\/([^/]+)/);
const initialId = new URLSearchParams(location.search).get('commissionId')
  || (pathMatch ? decodeURIComponent(pathMatch[1]) : null)
  || 'C-DONE';
$('#commissionId').value = initialId;
load(initialId);
