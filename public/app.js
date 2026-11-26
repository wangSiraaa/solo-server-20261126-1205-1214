const state = { roundId: null, rounds: [] };

const reasonLabels = {
  ineligible: '无该席资格',
  unwilling: '未承诺愿任该席',
  uncommitted: '未签愿任承诺',
  refused: '本人拒绝',
  commitment_expired: '承诺已届期',
  person_already_seated: '此人已就任另一席',
  seat_filled: '该席已满额',
  quota_filled_by_earlier_tickets: '定额已由更早票取满，本票转后项',
  quota_selected_by_ticket_order: '按流水号被取用',
  active: '当前有效支持',
};

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers ?? {}) };
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = new Error(body?.error?.message ?? `HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return { body, response };
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function reason(code) {
  return reasonLabels[code] ?? code;
}

async function checkHealth() {
  try {
    const { body } = await api('/api/health');
    document.querySelector('#health').textContent = `服务在线 · ${body.time.slice(11, 19)}`;
  } catch {
    document.querySelector('#health').textContent = '服务未启动';
  }
}

async function loadRounds(selectId = null) {
  const { body } = await api('/api/rounds');
  state.rounds = body.rounds;
  const select = document.querySelector('#round-select');
  select.innerHTML = state.rounds
    .slice()
    .reverse()
    .map((round) => `<option value="${escapeHtml(round.id)}">${escapeHtml(round.name)} · ${escapeHtml(round.id)}</option>`)
    .join('');
  if (selectId) select.value = selectId;
  state.roundId = selectId || select.value || null;
  if (state.roundId) await renderRound(state.roundId);
}

async function runScenario(button) {
  const scenario = button.dataset.scenario;
  const preference = button.dataset.preference;
  const message = document.querySelector('#action-message');
  message.textContent = '正在建卷、交票并原子结算…';
  try {
    const roundId = `web-${scenario}-${Date.now()}`;
    await api('/api/scenarios/seed', {
      method: 'POST',
      body: JSON.stringify({ scenario, options: { roundId, preference } }),
    });
    const { body: settled } = await api(`/api/rounds/${encodeURIComponent(roundId)}/settle`, { method: 'POST' });
    await loadRounds(roundId);
    message.textContent = `已结算：${settled.settlement.appointments.length} 张就任书，轮次 ${roundId}`;
  } catch (error) {
    message.textContent = `失败：${error.message}`;
    document.querySelector('#raw').textContent = JSON.stringify(error.body ?? error, null, 2);
  }
}

function renderRoster(roster, round) {
  const candidates = round.candidates ?? {};
  document.querySelector('#roster').innerHTML = roster
    .map((seat) => {
      if (seat.status === 'vacant') {
        const nominees = (seat.visibleNomineeSupport ?? [])
          .map((item) => `<span class="pill">${escapeHtml(item.personId)} · ${escapeHtml(item.ballotId)}</span>`)
          .join('');
        return `
          <article class="seat vacant">
            <h3>${escapeHtml(seat.seatName)} <span class="badge">空席</span></h3>
            <p class="kv">定额：${seat.quota} 票</p>
            <p class="kv muted">尚无真实就任书；新人接任前此席继续显示为空。</p>
            ${nominees ? `<div class="kv">仅提名支持：<div class="pill-list">${nominees}</div></div>` : ''}
          </article>`;
      }
      return `
        <article class="seat filled">
          <h3>${escapeHtml(seat.seatName)} <span class="badge">已就任</span></h3>
          <p class="kv"><strong>${escapeHtml(seat.officeholder.name)}</strong>（${escapeHtml(seat.officeholder.id)}）</p>
          <p class="kv">就任书：<code>${escapeHtml(seat.appointmentId)}</code></p>
          <p class="kv">回执：<code>${escapeHtml(seat.receiptCode)}</code></p>
          <p class="kv">消耗两票：
            <span class="pill-list">${seat.ballotIds.map((id) => `<span class="pill">${escapeHtml(id)}</span>`).join('')}</span>
          </p>
        </article>`;
    })
    .join('');
}

function renderAudit(destinations, round) {
  const candidateName = (id) => round.candidates?.[id]?.name ?? id;
  document.querySelector('#audit').innerHTML = `
    <table>
      <thead><tr><th>票</th><th>状态</th><th>最终落点</th><th>逐项轨迹</th></tr></thead>
      <tbody>
        ${destinations
          .map((item) => {
            const statusClass = item.status === 'used' ? 'status-used' : 'status-unused';
            const statusText =
              item.status === 'used'
                ? '已实际用票'
                : item.status === 'unused_support_below_quota'
                  ? '未用：支持不足定额'
                  : '未用：无可用后项';
            const target = item.personId
              ? `${candidateName(item.personId)}任${item.seatId === 'reading' ? '阅览' : '球场'}`
              : '—';
            const trail = (item.trail ?? [])
              .map((entry) => {
                const name = candidateName(entry.personId);
                const seat = entry.seatId === 'reading' ? '阅览' : entry.seatId === 'court' ? '球场' : entry.seatId;
                const selected = entry.selectedBallotIds?.length
                  ? `（更早取票：${entry.selectedBallotIds.join(', ')}）`
                  : '';
                return `<li>${name}＋${seat}：${entry.result === 'used' ? '✅ 用票就任' : '↪ 跳过'} · ${reason(entry.reason)}${selected}</li>`;
              })
              .join('');
            return `
              <tr>
                <td><code>${escapeHtml(item.ballotId)}</code><br><span class="muted">#${item.seq} · ${escapeHtml(item.voterId)}</span></td>
                <td class="${statusClass}">${statusText}</td>
                <td>${escapeHtml(target)}${item.receiptCode ? `<br><code>${escapeHtml(item.receiptCode)}</code>` : ''}</td>
                <td><ul class="trail">${trail}</ul></td>
              </tr>`;
          })
          .join('')}
      </tbody>
    </table>`;
}

async function renderRound(roundId) {
  const [roundResult, auditResult, rosterResult] = await Promise.all([
    api(`/api/rounds/${encodeURIComponent(roundId)}`),
    api(`/api/rounds/${encodeURIComponent(roundId)}/audit`),
    api(`/api/rounds/${encodeURIComponent(roundId)}/duty-roster`),
  ]);
  const round = roundResult.body;
  state.roundId = roundId;
  renderRoster(rosterResult.body.roster, round);
  renderAudit(auditResult.body.destinations, round);
  document.querySelector('#raw').textContent = JSON.stringify(
    { round, audit: auditResult.body.destinations, roster: rosterResult.body.roster },
    null,
    2,
  );
}

async function runConcurrentDesk() {
  const output = document.querySelector('#concurrency-output');
  output.textContent = '正在建立未结算四案卷，并让两个柜员同时 POST 同一阅览席…';
  const roundId = `concurrent-${Date.now()}`;
  await api('/api/scenarios/seed', {
    method: 'POST',
    body: JSON.stringify({ scenario: 'transfer', options: { roundId } }),
  });

  const requestBody = JSON.stringify({ seatId: 'reading', personId: 'jia' });
  const [first, second] = await Promise.allSettled([
    fetch(`/api/rounds/${roundId}/appointments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `${roundId}-desk-1` },
      body: requestBody,
    }).then(async (res) => ({ status: res.status, body: await res.json() })),
    fetch(`/api/rounds/${roundId}/appointments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `${roundId}-desk-2` },
      body: requestBody,
    }).then(async (res) => ({ status: res.status, body: await res.json() })),
  ]);

  const round = await api(`/api/rounds/${roundId}`);
  output.textContent = JSON.stringify(
    {
      clerkOne: first.status === 'fulfilled' ? first.value : first.reason,
      clerkTwo: second.status === 'fulfilled' ? second.value : second.reason,
      persistedAppointments: round.body.appointments,
      note: '一个请求成功创建唯一就任书；另一个收到 SEAT_ALREADY_FILLED 并带回同一张就任书。重启后可用 GET /api/receipts/:code 或重发同一 Idempotency-Key 查回 b1/b2。',
    },
    null,
    2,
  );
  await loadRounds(roundId);
}

document.querySelectorAll('button[data-scenario]').forEach((button) => {
  button.addEventListener('click', () => runScenario(button));
});
document.querySelector('#refresh-rounds').addEventListener('click', () => loadRounds());
document.querySelector('#round-select').addEventListener('change', (event) => renderRound(event.target.value));
document.querySelector('#concurrent-desk').addEventListener('click', runConcurrentDesk);

checkHealth();
setInterval(checkHealth, 5000);
loadRounds().catch((error) => {
  document.querySelector('#action-message').textContent = error.message;
});
