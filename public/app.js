const $ = (id) => document.getElementById(id);

let state = null;
let lastQuote = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = data?.error?.message || `请求失败 ${response.status}`;
    const error = new Error(message);
    error.data = data;
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(message, isError = false) {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show${isError ? ' error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = 'toast'; }, 4200);
}

function gridHtml(pattern, extraClass = '') {
  if (!pattern) return '<span class="muted">无图</span>';
  const large = extraClass.includes('pattern-preview');
  const cellSize = large ? 24 : 15;
  const wrapClass = large ? 'pattern-preview' : 'mini-grid';
  return `
    <div class="${wrapClass} ${extraClass}" style="grid-template-columns:repeat(${pattern.cols}, ${cellSize}px)">
      ${pattern.cells.map((row) => row.map((color) => {
        const style = color && color.startsWith('#') ? ` style="background:${color};border-color:rgba(0,0,0,.18)"` : '';
        return `<div class="cell ${color && !color.startsWith('#') ? color : ''}"${style} title="${color || '空'}"></div>`;
      }).join('')).join('')}
    </div>`;
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function setDebug(value) {
  $('debug').textContent = typeof value === 'string' ? value : pretty(value);
}

function badge(text, kind = '') {
  return `<span class="badge ${kind}">${text}</span>`;
}

async function loadState() {
  state = await api('/api/state');
  $('globalStatus').textContent = `revision ${state.revision} · ${state.bars.length} 条 / ${state.pieces.length} 片 / ${state.boxes.length} 盒`;
  renderCards();
  renderBoxList();
  await Promise.all([renderBar(), renderRemnants()]);
}

function renderCards() {
  const select = $('cardSelect');
  const current = select.value || 'CARD-FLOWER';
  select.innerHTML = state.cards.map((c) => `<option value="${c.code}">${c.code} · ${c.name}</option>`).join('');
  select.value = state.cards.some((c) => c.code === current) ? current : state.cards[0]?.code;
}

async function renderBar() {
  try {
    const replay = await api('/api/bars/BAR-DEMO/replay');
    $('barCode').textContent = replay.barCode;
    $('barStrip').innerHTML = replay.layers.map((layer) => `
      <div class="layer-card ${layer.state === 'kerf' ? 'kerf' : layer.state === 'piece' ? 'piece' : ''}">
        <div class="layer-index">
          <span>#${layer.index}</span>
          <span>${layer.state === 'kerf' ? '切口' : layer.pieceId ? layer.pieceId.replace('PIECE-', 'P') : layer.poured ? '浇注' : '留空'}</span>
        </div>
        ${gridHtml(layer.barHeadView)}
      </div>
    `).join('');
  } catch (error) {
    $('barStrip').innerHTML = `<span class="muted">演示条不存在，请点“重置并灌入八格样条”。</span>`;
  }
}

async function renderRemnants() {
  try {
    const rem = await api('/api/bars/BAR-DEMO/remnants');
    $('remnants').innerHTML = rem.segments.length
      ? rem.segments.map((s) => badge(`连续格 ${s.start}..${s.end}（${s.length} 格）`, s.fitsStandardPiece ? 'ok' : '')).join(' ')
      : '已无完整连续余料；切口格也不会恢复为成品。';
  } catch {
    $('remnants').textContent = '无演示条。';
  }
}

async function loadRecommendations() {
  const params = new URLSearchParams({
    barCode: 'BAR-DEMO',
    piecesPerBox: String($('piecesPerBox').value || 2),
    orientationPolicy: $('policySelect').value
  });
  try {
    const recs = await api(`/api/recommendations?${params}`);
    $('recommendList').innerHTML = recs.map((r) => `
      <div class="match-item">
        <div><strong>${r.cardId}</strong> · ${r.name}<br/>
        <span class="kv">真实匹配 ${r.physicalMatchCount} 片；当前可领 ${r.availablePieceCount} 片</span></div>
        ${badge(r.canDeliverBox ? `可装 ${r.maxCompleteBoxes} 整盒` : '凑不齐整盒', r.canDeliverBox ? 'ok' : 'bad')}
      </div>
    `).join('');
  } catch (error) {
    $('recommendList').innerHTML = `<span class="muted">${error.message}</span>`;
  }
}

async function runQuote() {
  const body = {
    barCode: 'BAR-DEMO',
    cardId: $('cardSelect').value,
    piecesPerBox: Number($('piecesPerBox').value || 2),
    orientationPolicy: $('policySelect').value,
    boxCount: 1
  };
  try {
    lastQuote = await api('/api/quotes', { method: 'POST', body });
    const q = lastQuote;
    const plan = q.maxAssignments;
    $('quoteSummary').innerHTML = `
      <div class="row">
        ${badge(`真实截面匹配 ${q.physicalMatchCount} 片`, q.physicalMatchCount >= 2 ? 'ok' : 'bad')}
        ${badge(`当前可凑 ${q.maxBoxes} 整盒`, q.maxBoxes >= 1 ? 'ok' : 'bad')}
        ${badge(`可领候选 ${q.piecesConsidered} 片`)}
        ${badge(q.orientationPolicies.join(','), 'blue')}
      </div>
      <div class="muted" style="margin-top:8px">询单一盒 ${body.piecesPerBox} 片：${q.feasible ? '可领片且有匹配的盒，必须能交' : '当前可领库存凑不齐；已装盒片只计入真实花纹回放'}</div>
    `;
    $('matchList').innerHTML = (q.physicalMatches || []).map((m) => `
      <div class="match-item">
        <div>
          <strong>${m.pieceCode}</strong> · ${m.barCode}<br/>
          <span class="kv">真实面：向${m.face === 'head' ? '头' : '尾'} · 第 ${m.cellIndex} 格；
          朝向：${m.transform === 'identity' ? '条坯方向正放' : '左右翻转后合卡'}</span><br/>
          ${badge(m.status, m.available ? 'ok' : 'bad')}
        </div>
        ${gridHtml(m.outwardPattern)}
      </div>
    `).join('') || '<div class="muted">没有任何真实端面匹配；系统不会另画虚构切面。</div>';
    setDebug(q);
    loadRecommendations();
  } catch (error) {
    toast(error.message, true);
    setDebug(error.data || error.stack);
  }
}

async function createOrder() {
  const body = {
    barCode: 'BAR-DEMO',
    cardId: $('cardSelect').value,
    piecesPerBox: Number($('piecesPerBox').value || 2),
    boxCount: 1,
    orientationPolicy: $('policySelect').value,
    clerk: $('clerk').value,
    customer: $('customer').value,
    destination: $('destination').value
  };
  try {
    const result = await api('/api/orders', { method: 'POST', body });
    $('orderResult').style.display = 'block';
    $('orderResult').textContent = pretty(result);
    $('boxSearch').value = result.boxes[0].code;
    toast(`已装盒：${result.boxes.map((b) => b.code).join(', ')}`);
    await loadState();
    await showBox(result.boxes[0].code);
  } catch (error) {
    toast(error.data?.error?.details?.maxBoxes !== undefined ? `${error.message}（最多 ${error.data.error.details.maxBoxes} 盒）` : error.message, true);
    setDebug(error.data || error.stack);
  }
}

async function showBox(code = $('boxSearch').value.trim()) {
  if (!code) return;
  try {
    const box = await api(`/api/boxes/${encodeURIComponent(code)}`);
    $('boxSearch').value = box.code;
    $('boxDetail').innerHTML = `
      <div class="box-card">
        <div class="box-head">
          <div><strong class="code">${box.code}</strong> ${badge(box.status, box.status === 'picked_up' ? 'ok' : 'blue')}</div>
          <div class="muted">订单 ${box.orderId}</div>
        </div>
        <div class="muted" style="margin-top:8px">客人：${box.customer || '-'}；去向：<strong>${box.pickup?.destination || box.destination || '待登记'}</strong></div>
        ${box.pickup ? `<div class="muted">${box.pickup.at} 由 ${box.pickup.receivedBy} 取走</div>` : ''}
        ${box.slots.map((slot) => `
          <div class="pattern-block">
            ${gridHtml(slot.outwardPattern, 'pattern-preview')}
            <div class="pattern-meta">
              <strong>${slot.pieceCode}</strong> / ${slot.barCode}<br/>
              朝外：向${slot.outwardFace === 'head' ? '头' : '尾'}面，第 ${slot.actualCellIndex} 真实格<br/>
              朝向记录：${slot.orientationTransform}<br/>
              <span class="muted">样卡版本 v${slot.cardSnapshot.version} · ${slot.cardSnapshot.name}</span>
              <div class="face-pair">
                ${slot.twoFaces.map((f) => `
                  <div class="face">
                    ${f.side === 'head' ? '向头面直视' : '向尾面直视（左右已反）'}
                    ${gridHtml(f.outsideView)}
                  </div>
                `).join('')}
              </div>
            </div>
          </div>
        `).join('')}
      </div>`;
    setDebug(box);
  } catch (error) {
    $('boxDetail').innerHTML = `<div class="muted">${error.message}</div>`;
  }
}

function renderBoxList() {
  $('boxList').innerHTML = state.boxes.slice().reverse().map((b) => `
    <div class="match-item" style="cursor:pointer" data-box="${b.code}">
      <div><strong>${b.code}</strong><br/><span class="kv">${b.pieceIds.join(' · ')} → ${b.pickup?.destination || b.destination || '待取'}</span></div>
      ${badge(b.status, b.status === 'picked_up' ? 'ok' : 'blue')}
    </div>
  `).join('') || '<span class="muted">还没有盒号。</span>';
  document.querySelectorAll('[data-box]').forEach((el) => el.addEventListener('click', () => showBox(el.dataset.box)));
}

async function resetDemo() {
  await api('/api/admin/reset', { method: 'POST', body: {} });
  $('boxDetail').innerHTML = '';
  toast('已重置为八格样条、两张样卡和两片标准片');
  await loadState();
  await runQuote();
}

async function yellowBlue() {
  await api('/api/bars/BAR-DEMO/cells/6', { method: 'PATCH', body: { row: 2, col: 2, color: 'B', reason: '演示：仅第二处黄心改蓝' } });
  toast('第二处实际浇注黄格已改蓝，再询单应少一片');
  await loadState();
  await runQuote();
}

async function restoreBar() {
  await api('/api/bars/BAR-DEMO/restore-original', { method: 'POST', body: {} });
  toast('已换回合模时的原条');
  await loadState();
  await runQuote();
}

async function openBar() {
  const code = $('newBarCode').value.trim() || `BAR-${Date.now()}`;
  const length = Number($('newBarLength').value || 9);
  try {
    await api('/api/bars', { method: 'POST', body: {
      code,
      length: Number($('newBarLength').value || 9),
      rows: 5,
      cols: 5,
      closeMode: $('closeMode').value,
      autoClose: true
    } });
    $('cutBarCode').value = code;
    toast(`已开模、浇注空格并合模：${code}`);
    setDebug({ ok: true, code, length });
    await loadState();
  } catch (error) { toast(error.message, true); }
}

async function cutOnce() {
  const body = {
    from: $('cutFrom').value,
    width: Number($('cutWidth').value),
    kerfWidth: Number($('cutKerf').value),
    clerk: $('clerk').value || 'clerk'
  };
  try {
    const piece = await api(`/api/bars/${encodeURIComponent($('cutBarCode').value.trim())}/cut`, { method: 'POST', body });
    $('cutResult').innerHTML = `领到真实片号 <strong>${piece.code}</strong>：保留格 ${piece.start}..${piece.end}，切口格 ${piece.kerfIndexes.join(',')}`;
    setDebug(piece);
    await loadState();
  } catch (error) {
    $('cutResult').textContent = error.message;
    setDebug(error.data || error.stack);
    toast(error.message, true);
  }
}

async function concurrentClaim() {
  await resetDemo();
  const responses = await Promise.allSettled([
    api('/api/pieces/PIECE-0001/claim', { method: 'POST', body: { clerk: 'clerk-A' } }),
    api('/api/pieces/PIECE-0001/claim', { method: 'POST', body: { clerk: 'clerk-B' } })
  ]);
  const summary = responses.map((r, i) => r.status === 'fulfilled'
    ? `柜员 ${i === 0 ? 'A' : 'B'}：成功，${r.value.code}`
    : `柜员 ${i === 0 ? 'A' : 'B'}：失败，${r.reason.message}`);
  toast(summary.join(' ｜ '), responses.some((r) => r.status === 'rejected') && responses.some((r) => r.status === 'fulfilled') ? false : true);
  setDebug(responses.map((r) => r.status === 'fulfilled' ? r.value : { error: r.reason.message, data: r.reason.data?.error }));
  await loadState();
}

async function concurrentCut() {
  const code = `BAR-CONC-${Date.now().toString().slice(-6)}`;
  await api('/api/bars', { method: 'POST', body: { code, length: 12, rows: 3, cols: 3, autoClose: true } });
  const responses = await Promise.allSettled([
    api(`/api/bars/${code}/cut`, { method: 'POST', body: { clerk: 'A', width: 3, kerfWidth: 1 } }),
    api(`/api/bars/${code}/cut`, { method: 'POST', body: { clerk: 'B', width: 3, kerfWidth: 1 } })
  ]);
  $('cutBarCode').value = code;
  setDebug(responses.map((r) => r.status === 'fulfilled' ? r.value : { error: r.reason.message, details: r.reason.data?.error?.details }));
  const okCuts = responses.filter((r) => r.status === 'fulfilled').map((r) => r.value.code);
  toast(`并发切取完成：成功 ${okCuts.length} 片（${okCuts.join(', ')}），其余请求只能看到不存在/已占格`, okCuts.length === 0);
  await loadState();
}

async function pickup() {
  const code = $('boxSearch').value.trim();
  try {
    const box = await api(`/api/boxes/${encodeURIComponent(code)}/pickup`, {
      method: 'POST',
      body: { receivedBy: $('clerk').value, destination: $('destination').value }
    });
    toast(`${box.code} 已取货，去向：${box.pickup.destination}`);
    await loadState();
    await showBox(box.code);
  } catch (error) {
    toast(error.message, true);
  }
}

$('refreshBtn').addEventListener('click', loadState);
$('resetBtn').addEventListener('click', resetDemo);
$('quoteBtn').addEventListener('click', runQuote);
$('recommendBtn').addEventListener('click', loadRecommendations);
$('cardSelect').addEventListener('change', runQuote);
$('policySelect').addEventListener('change', runQuote);
$('yellowBlueBtn').addEventListener('click', yellowBlue);
$('restoreBtn').addEventListener('click', restoreBar);
$('openBarBtn').addEventListener('click', openBar);
$('cutBtn').addEventListener('click', cutOnce);
$('concurrentClaimBtn').addEventListener('click', concurrentClaim);
$('concurrentCutBtn').addEventListener('click', concurrentCut);
$('boxOrderBtn').addEventListener('click', createOrder);
$('boxSearchBtn').addEventListener('click', () => showBox());
$('pickupBtn').addEventListener('click', pickup);

loadState().then(runQuote).catch((error) => {
  toast(error.message, true);
  setDebug(error.stack);
});
