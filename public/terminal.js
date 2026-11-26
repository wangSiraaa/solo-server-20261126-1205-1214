import { api, $, escapeHtml, badge, getDeviceId } from '/api.js';

const params = new URLSearchParams(location.search);
let claimId = params.get('claim') ?? '';
let terminal = null;
let receipt = null;
let config = null;
let lastCompletionId = null;

$('#claim-input').value = claimId;
$('#device-id').value = getDeviceId();

function bookName(id) {
  return config?.prototype?.books?.find((book) => book.id === id)?.name ?? id;
}

async function load() {
  claimId = $('#claim-input').value.trim();
  if (!claimId) return;
  const [view, claimReceiptData, cfg] = await Promise.all([
    api(`/api/claims/${encodeURIComponent(claimId)}/terminal`),
    api(`/api/claims/${encodeURIComponent(claimId)}`),
    api('/api/config'),
  ]);
  terminal = view;
  receipt = claimReceiptData;
  config = cfg;
  claimId = view.claimId;
  $('#claim-input').value = claimId;
  history.replaceState({}, '', `/terminal.html?claim=${encodeURIComponent(claimId)}`);
  render();
}

function render() {
  if (!terminal) return;
  const { screen, actions, task, eligibility } = terminal;

  $('#claim-badges').innerHTML = `
    ${badge(receipt?.settlement?.status ?? receipt?.payout?.status ?? 'active')}
    <span class="badge blue">${escapeHtml(terminal.prototype.id)}@v${terminal.prototype.version}</span>
    <span class="badge green">单价 ${receipt?.agreementSnapshot?.unitPrice ?? '?'} 点</span>`;

  $('#phone').innerHTML = `
    <h2 class="phone-title">${escapeHtml(screen.title)}</h2>
    <p class="muted small">${escapeHtml(screen.body ?? '')}</p>
    <div class="small">
      <strong>收藏：</strong>${terminal.state.favorites.length ? terminal.state.favorites.map((id) => `<span class="badge blue">${escapeHtml(bookName(id))}</span>`).join(' ') : '<span class="muted">空</span>'}
      <br style="margin:6px 0">
      <strong>待借：</strong>${terminal.state.loanFlags.length ? terminal.state.loanFlags.map((id) => `<span class="badge amber">${escapeHtml(bookName(id))}</span>`).join(' ') : '<span class="muted">无</span>'}
    </div>
    <div class="action-list">
      ${actions
        .map(
          (action) => `
          <button data-action='${escapeHtml(JSON.stringify({ actionId: action.actionId, targetId: action.targetId ?? null }))}'
            class="${action.designerShortcut ? 'secondary' : ''}">
            <span>${escapeHtml(action.label)}${action.targetName ? ` · ${escapeHtml(action.targetName)}` : ''}</span>
            <small>${escapeHtml(action.actionId)}</small>
          </button>`,
        )
        .join('')}
    </div>`;

  document.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', () => sendClick(JSON.parse(button.dataset.action)));
  });

  $('#task-panel').innerHTML = `
    <div class="notice info"><strong>${escapeHtml(task.title)}</strong><br>${escapeHtml(task.brief ?? '')}</div>
    <div><strong>进度 ${task.doneCount}/${task.totalCount}：</strong> ${badge(task.status)}</div>
    <div style="margin-top:10px">
      ${task.steps
        .map(
          (step) => `<div class="task-step">
            <div class="check ${step.done ? 'done' : ''}">${step.done ? '✓' : step.ordinal}</div>
            <div><strong>${escapeHtml(step.label)}</strong><br>
            <span class="small muted">${escapeHtml(step.verb)} → ${escapeHtml(step.targetId)}</span>
            ${step.matchedEvent ? `<br><span class="small green">第 ${step.matchedEvent.seq} 次点击已真实生效 · ${new Date(step.matchedEvent.at).toLocaleTimeString()}</span>` : ''}
            </div>
          </div>`,
        )
        .join('')}
    </div>`;

  $('#eligibility-panel').innerHTML = eligibility.excluded
    ? `<div class="notice err"><strong>发现奖资格已排除</strong><br>原因：${escapeHtml(eligibility.reason)}<br>
       来源回条：${(eligibility.sourceReceiptNos ?? []).map((n) => `<span class="code-pill">${escapeHtml(n)}</span>`).join(' ') || '当前/历史曝光'}
       </div>`
    : `<div class="notice ok"><strong>当前未发现此前泄题或带教记录。</strong> 若主动拆封“直接说破答案”的提示，本单发现奖将不能结算。</div>`;

  const taskDef = config.tasks.find((item) => item.id === task.id);
  const hintIds = taskDef?.allowedHintIds ?? [];
  $('#hint-panel').innerHTML = hintIds
    .map((hintId) => {
      const hint = config.hints[hintId];
      const shown = receipt?.hintExposures?.some((e) => e.hintId === hintId);
      return `<div class="notice ${hint.answerRevealing ? 'warn' : 'info'}">
        <strong>${escapeHtml(hint.title)}</strong><br>${escapeHtml(hint.body)}
        <div class="row" style="margin-top:8px">
          <button class="secondary" data-hint="${escapeHtml(hintId)}" ${shown ? 'disabled' : ''}>
            ${shown ? '已实际展示并留痕' : hint.answerRevealing ? '拆封（会取消发现奖）' : '查看提示'}
          </button>
        </div>
      </div>`;
    })
    .join('');
  document.querySelectorAll('[data-hint]').forEach((button) => {
    button.addEventListener('click', () => viewHint(button.dataset.hint));
  });
}

async function sendClick(payload, extra = {}) {
  try {
    const result = await api(`/api/sessions/${encodeURIComponent(terminal.sessionId)}/click`, {
      method: 'POST',
      body: {
        ...payload,
        ...extra,
        eventId: crypto.randomUUID(),
        deviceId: $('#device-id').value || getDeviceId(),
      },
    });
    terminal = result.terminal;
    render();
  } catch (error) {
    if (error.data?.terminal) {
      terminal = error.data.terminal;
      render();
    }
    $('#completion-result').insertAdjacentHTML(
      'beforeend',
      `<div class="notice err">${escapeHtml(error.data?.error ?? 'CLICK_REJECTED')}：${escapeHtml(error.message)}</div>`,
    );
  }
}

async function viewHint(hintId) {
  try {
    await api(`/api/claims/${encodeURIComponent(claimId)}/hints/${encodeURIComponent(hintId)}/view`, {
      method: 'POST',
      body: { deviceId: $('#device-id').value || getDeviceId() },
    });
    await load();
  } catch (error) {
    $('#hint-panel').insertAdjacentHTML('beforeend', `<div class="notice err">${escapeHtml(error.message)}</div>`);
  }
}

$('#load-claim').addEventListener('click', load);

$('#retarget-btn').addEventListener('click', () => {
  sendClick({ actionId: 'uncollect:book-blue', targetId: 'book-green' });
});

$('#shortcut-btn').addEventListener('click', () => {
  sendClick({ actionId: 'shortcut-lookalike-finish' });
});

async function submitCompletion(deviceId = $('#device-id').value || getDeviceId(), completionId = $('#completion-id').value.trim() || undefined) {
  try {
    const result = await api(`/api/claims/${encodeURIComponent(claimId)}/completions`, {
      method: 'POST',
      // 同一 completionId 是跨设备重传的幂等身份；不另外固定 Idempotency-Key，
      // 以便服务端记录不同设备确实重传了同一完成。
      body: { completionId, deviceId },
    });
    lastCompletionId = result.completion.id;
    $('#completion-id').value = lastCompletionId;
    $('#retransmit-b-btn').disabled = false;
    $('#retransmit-c-btn').disabled = false;
    renderVerdict(result);
    receipt = await api(`/api/claims/${encodeURIComponent(claimId)}`);
    render();
    return result;
  } catch (error) {
    $('#verdict-panel').innerHTML = `<div class="notice err">${escapeHtml(error.data?.error ?? 'ERROR')}：${escapeHtml(error.message)}<pre class="json">${escapeHtml(JSON.stringify(error.data?.details ?? {}, null, 2))}</pre></div>`;
  }
}

function renderVerdict(result) {
  const v = result.verdict ?? result.completion?.verdict;
  const duplicate = result.deduplicated ? '<span class="badge blue">多设备重传去重</span>' : '';
  const settlement = result.settlement ? `<div class="notice ok">已有结算单 ${escapeHtml(result.settlement.settlementNo)}，金额 ${result.settlement.amount} 点，不会重复建立。</div>` : '';
  $('#completion-result').innerHTML = `<div class="notice ${v.discoveryPayable ? 'ok' : v.behaviorPassed ? 'warn' : 'err'}">
    ${duplicate}
    <strong>${escapeHtml(v.summary)}</strong></div>${settlement}`;
  $('#verdict-panel').innerHTML = `
    <div class="row" style="margin-bottom:10px">
      ${v.behaviorPassed ? '<span class="badge green">轨迹行为通过</span>' : '<span class="badge red">轨迹行为不通过</span>'}
      ${v.discoveryPayable ? '<span class="badge green">可付发现奖</span>' : '<span class="badge red">不可付发现奖</span>'}
      <span class="badge gray">completion: ${escapeHtml(result.completion.id)}</span>
    </div>
    <div class="table-wrap"><table><thead><tr><th>检查</th><th>结果</th><th>证据</th></tr></thead><tbody>
      ${v.checks
        .map(
          (check) => `<tr><td>${escapeHtml(check.id)}</td><td>${check.ok ? '<span class="badge green">通过</span>' : '<span class="badge red">失败</span>'}</td><td class="small">${escapeHtml(check.detail)}</td></tr>`,
        )
        .join('')}
    </tbody></table></div>
    <p class="row" style="margin-top:12px">
      <a class="button secondary" href="/review.html?completion=${encodeURIComponent(result.completion.id)}">去招募方审核</a>
      <a class="button success" href="/pay.html?tester=${encodeURIComponent(terminal.testerId)}">去领报酬</a>
    </p>`;
}

$('#submit-btn').addEventListener('click', () => submitCompletion());
$('#retransmit-b-btn').addEventListener('click', () => submitCompletion('device-B-multi', lastCompletionId));
$('#retransmit-c-btn').addEventListener('click', () => submitCompletion('device-C-multi', lastCompletionId));

if (claimId) load().catch((error) => {
  $('#phone').innerHTML = `<div class="notice err">${escapeHtml(error.message)}</div>`;
});
