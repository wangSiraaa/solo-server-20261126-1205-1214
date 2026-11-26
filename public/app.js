const $tickets = document.querySelector('#tickets');
const $cases = document.querySelector('#cases');
const $notifications = document.querySelector('#notifications');
const $maintainerSteps = document.querySelector('#maintainer-steps');
const $policy = document.querySelector('#policy');
const $toast = document.querySelector('#toast');

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

let toastTimer;
function toast(message) {
  $toast.textContent = message;
  $toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $toast.classList.remove('show'), 3200);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) {
    const message = data?.error?.message ?? `请求失败：${response.status}`;
    const error = new Error(message);
    error.payload = data;
    throw error;
  }
  return data;
}

function statusBadge(ticket, closure) {
  if (ticket.status === 'closed') return '<span class="badge ok">已签收结案</span>';
  if (ticket.openRecheckId) return '<span class="badge warn">复查中</span>';
  if (closure?.eligible) return '<span class="badge ok">可发结案通知</span>';
  return '<span class="badge bad">未满足结案</span>';
}

function renderTicket(ticket, closure, nextStep) {
  const blockers = (closure?.reasons ?? [])
    .map((reason) => {
      const sources = reason.sourceIds?.length
        ? `（${reason.sourceIds.join('、')}）`
        : '';
      return `<li>${escapeHtml(reason.message)}${escapeHtml(sources)}</li>`;
    })
    .join('');

  const combos = (closure?.compatibility?.combinations ?? [])
    .map((combo) => (combo.length ? combo.join('+') : '∅'))
    .join('，');
  const unresolved = closure?.compatibility?.unresolvedSourceIds ?? [];
  const notice = ticket.closure?.noticeId
    ? closure && ticket.closure
    : null;

  return `
    <article class="card" data-ticket="${escapeHtml(ticket.id)}">
      <h3>
        <span>${escapeHtml(ticket.id)} · ${escapeHtml(ticket.residentName)} · ${escapeHtml(ticket.unit ?? ticket.location)}</span>
        ${statusBadge(ticket, closure)}
      </h3>
      <p class="kv"><strong>报修：</strong>${escapeHtml(ticket.report.symptom)}；${escapeHtml(ticket.location)}</p>
      <p class="kv"><strong>合同：</strong><code>${escapeHtml(ticket.contract.kind)}</code>
        ${ticket.contract.excludeAllCompatibleSources ? '· 排除/处置全部相容源头' : ''}
      </p>
      <p class="kv"><strong>相容组合：</strong>${escapeHtml(combos || '—')}</p>
      <p class="kv"><strong>模型未处置源头：</strong>
        ${unresolved.length ? `<span class="badge bad">${escapeHtml(unresolved.join('、'))}</span>` : '<span class="badge ok">无</span>'}
        <span class="muted">（是否阻断取决于本户合同）</span>
      </p>
      <p class="kv"><strong>住户下一步：</strong>${escapeHtml(nextStep?.text ?? '')}</p>
      ${blockers ? `<ul class="reasons">${blockers}</ul>` : ''}
      <div class="actions">
        <button data-action="sign" data-id="${escapeHtml(ticket.id)}" data-actor="${escapeHtml(ticket.residentId)}">实际签收</button>
        <button class="danger" data-action="recheck" data-id="${escapeHtml(ticket.id)}" data-actor="${escapeHtml(ticket.residentId)}">提出复查</button>
        <button data-action="next" data-id="${escapeHtml(ticket.id)}">刷新下一步</button>
      </div>
      ${notice ? `<p class="muted">结案通知：${escapeHtml(ticket.closure.noticeId)}</p>` : ''}
    </article>
  `;
}

function renderCases(state) {
  if (!state.cases.length) {
    $cases.innerHTML = '<p class="muted">暂无共案。基准场景会建立甲、乙共案。</p>';
    return;
  }
  $cases.innerHTML = state.cases
    .map((workCase) => {
      const order = workCase.inspectionOrder?.length
        ? workCase.inspectionOrder.join(' → ')
        : '尚未排定';
      return `
        <article class="card">
          <h3><span>${escapeHtml(workCase.id)}</span><span class="badge">${escapeHtml(workCase.status)}</span></h3>
          <p class="kv"><strong>在案住户：</strong>${escapeHtml(workCase.memberTicketIds.join('、') || '无')}</p>
          <p class="kv"><strong>原案住户：</strong>${escapeHtml((workCase.formerMemberTicketIds ?? []).join('、') || '—')}</p>
          <p class="kv"><strong>检查次序：</strong>${escapeHtml(order)}</p>
          <div class="actions">
            <button data-case-action="plan" data-id="${escapeHtml(workCase.id)}">发队前定序</button>
            <button class="primary" data-case-action="dispatch" data-id="${escapeHtml(workCase.id)}">发队</button>
            <button class="danger" data-case-action="split" data-id="${escapeHtml(workCase.id)}">拆出选中户</button>
          </div>
          <label>拆案住户 ID（多个用逗号）
            <input data-split-input="${escapeHtml(workCase.id)}" placeholder="T2" />
          </label>
        </article>
      `;
    })
    .join('');
}

function renderNotifications(state) {
  if (!state.notifications.length) {
    $notifications.innerHTML = '<p class="muted">暂无通知。</p>';
    return;
  }
  $notifications.innerHTML = state.notifications
    .slice()
    .reverse()
    .map(
      (notification) => `
      <div class="notice">
        <strong>${escapeHtml(notification.ticketId)} · ${escapeHtml(notification.type)}</strong>
        <div class="muted">${escapeHtml(notification.dedupeKey)}</div>
        <div>${escapeHtml(notification.status)} · ${escapeHtml(notification.issuedAt)}</div>
      </div>`,
    )
    .join('');
}

async function refresh() {
  const state = await request('/api/state');
  const closureEntries = await Promise.all(
    state.tickets.map(async (ticket) => {
      try {
        return [ticket.id, await request(`/api/tickets/${ticket.id}/closure`)];
      } catch (error) {
        return [ticket.id, { eligible: false, reasons: [{ message: error.message }] }];
      }
    }),
  );
  const closures = Object.fromEntries(closureEntries);
  const stepEntries = await Promise.all(
    state.tickets.map(async (ticket) => [
      ticket.id,
      await request(`/api/tickets/${ticket.id}/next-step?role=resident`),
    ]),
  );
  const steps = Object.fromEntries(stepEntries);

  $tickets.innerHTML = state.tickets
    .map((ticket) => renderTicket(ticket, closures[ticket.id], steps[ticket.id]))
    .join('');
  renderCases(state);
  renderNotifications(state);

  const workbench = await request('/api/next-steps?role=maintainer');
  $maintainerSteps.innerHTML = workbench.items
    .map(
      (item) =>
        `<p class="kv"><span class="badge warn">${escapeHtml(item.code)}</span> ${escapeHtml(item.text)}</p>`,
    )
    .join('');

  const policy = await request('/api/policy/recommendation');
  $policy.innerHTML = `
    <p class="kv"><span class="badge ok">${escapeHtml(policy.mode)}</span></p>
    <p class="kv">${escapeHtml(policy.reason)}</p>
    ${policy.ticketIds ? `<p class="muted">建议住户单：${escapeHtml(policy.ticketIds.join('、'))}</p>` : ''}
    ${policy.evidenceId ? `<p class="muted">证据：<code>${escapeHtml(policy.evidenceId)}</code> → ${escapeHtml(policy.targetTicketId)}</p>` : ''}
  `;
}

document.querySelector('.toolbar').addEventListener('click', async (event) => {
  const seedButton = event.target.closest('[data-seed]');
  if (seedButton) {
    try {
      await request('/api/scenarios/seed', {
        method: 'POST',
        body: { variant: seedButton.dataset.seed },
      });
      toast('场景已重置：' + seedButton.textContent);
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  }
});

document.querySelector('#seed-bing').addEventListener('click', async () => {
  await request('/api/scenarios/seed', {
    method: 'POST',
    body: { variant: 'baseline', options: { includeBing: true } },
  });
  toast('已载入基准并加办丙户');
  refresh();
});

document.querySelector('#seed-retract-b').addEventListener('click', async () => {
  await request('/api/scenarios/seed', {
    method: 'POST',
    body: { variant: 'baseline', options: { retractBExclusion: true } },
  });
  toast('已从基准撤下排除 B 的观察；乙不能签收');
  refresh();
});

document.querySelector('#run-closures').addEventListener('click', async () => {
  try {
    const result = await request('/api/closures/run', { method: 'POST', body: {} });
    toast(`扫描完成：发出/命中 ${result.issued.length} 条，阻断 ${result.blocked.length} 户`);
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

$tickets.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const { action, id, actor } = button.dataset;
  try {
    if (action === 'sign') {
      await request(`/api/tickets/${id}/sign`, {
        method: 'POST',
        body: { actorId: actor },
      });
      toast(`${id} 已实际签收`);
    } else if (action === 'recheck') {
      const reason = window.prompt('请输入复查原因', '仍有返潮/滴水');
      if (reason === null) return;
      await request(`/api/tickets/${id}/recheck-requests`, {
        method: 'POST',
        body: { residentId: actor, reason },
      });
      toast(`${id} 已提出复查`);
    } else {
      const step = await request(`/api/tickets/${id}/next-step?role=resident`);
      toast(step.text);
    }
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

$cases.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-case-action]');
  if (!button) return;
  const { id, caseAction } = button.dataset;
  try {
    if (caseAction === 'plan') {
      await request(`/api/cases/${id}/plan`, {
        method: 'POST',
        body: { inspectionOrder: ['CP_JIA', 'CP_YI', 'CP_B_TEST'] },
      });
      toast('检查次序已在发队前确定');
    } else if (caseAction === 'dispatch') {
      await request(`/api/cases/${id}/dispatch`, { method: 'POST', body: {} });
      toast('已模拟发队');
    } else if (caseAction === 'split') {
      const input = document.querySelector(`[data-split-input="${CSS.escape(id)}"]`);
      const ticketIds = (input?.value ?? '').split(',').map((x) => x.trim()).filter(Boolean);
      await request(`/api/cases/${id}/split`, {
        method: 'POST',
        body: { ticketIds, reason: '证据显示不同源，拆案但保留已回访结果' },
      });
      toast('已拆案，回访与通知未抹掉');
    }
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

document.querySelector('#borrow-public').addEventListener('click', async () => {
  try {
    const evidenceId = document.querySelector('#borrow-evidence').value.trim();
    const targetTicketId = document.querySelector('#borrow-target').value.trim();
    await request('/api/evidence/borrow', {
      method: 'POST',
      body: { evidenceId, targetTicketId },
    });
    toast('公区结论已借给相邻工单');
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

document.querySelector('#borrow-ding').addEventListener('click', async () => {
  try {
    try {
      await request('/api/tickets', {
        method: 'POST',
        body: {
          id: 'T4',
          residentId: 'U_DING',
          residentName: '丁',
          location: '相邻支路户',
          symptom: '邻段潮湿疑问',
          pointIds: ['CP_YI'],
          adjacentTo: ['T2'],
          contractKind: 'strict_source_exclusion',
        },
      });
    } catch (error) {
      if (error.payload?.error?.code !== 'VALIDATION') throw error;
    }
    await request('/api/evidence/borrow', {
      method: 'POST',
      body: { evidenceId: 'INS_B_PRESSURE_DRY', targetTicketId: 'T4' },
    });
    document.querySelector('#borrow-target').value = 'T4';
    toast('丁邻单已建立，并借到 B_EXCLUDED 公区结论');
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

document.querySelector('#create-ticket').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const entryStart = form.get('entryStart');
  const entryEnd = form.get('entryEnd');
  const revisitStart = form.get('revisitStart');
  const revisitEnd = form.get('revisitEnd');
  const body = {
    residentName: form.get('residentName'),
    location: form.get('location'),
    symptom: form.get('symptom'),
    contractKind: form.get('contractKind'),
    pointIds: String(form.get('pointIds') ?? '')
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean),
    entryWindows:
      entryStart && entryEnd
        ? [{ start: new Date(entryStart).toISOString(), end: new Date(entryEnd).toISOString() }]
        : [],
    revisitWindows:
      revisitStart && revisitEnd
        ? [{ id: 'RW_CREATED', start: new Date(revisitStart).toISOString(), end: new Date(revisitEnd).toISOString() }]
        : [],
  };
  if (body.revisitWindows.length) body.contract = { revisitWindowId: 'RW_CREATED' };
  try {
    await request('/api/tickets', { method: 'POST', body });
    event.currentTarget.reset();
    toast('报修单已提交');
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

document.querySelector('#maintainer-sign-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await request('/api/signoffs', {
      method: 'POST',
      body: {
        role: 'maintainer',
        kind: 'work_ack',
        ticketId: form.get('ticketId'),
        actorId: 'web-crew',
      },
    });
    toast('维修员已签收/确认工单');
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

document.querySelector('#revisit-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await request('/api/revisits', {
      method: 'POST',
      body: {
        ticketId: form.get('ticketId'),
        revisitId: form.get('revisitId'),
        result: form.get('result'),
        actorId: 'web-crew',
      },
    });
    toast('回访结果已登记，结案扫描已执行');
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

document.querySelector('#repair-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const caseId = String(form.get('caseId') ?? '').trim();
  const ticketId = String(form.get('ticketId') ?? '').trim();
  try {
    await request('/api/repairs', {
      method: 'POST',
      body: {
        caseId: caseId || undefined,
        ticketId: ticketId || undefined,
        sourceIds: String(form.get('sourceIds') ?? '')
          .split(',')
          .map((value) => value.trim())
          .filter(Boolean),
        actorId: 'web-crew',
      },
    });
    toast('模拟维修已登记，施工工序已生成');
    await refresh();
  } catch (error) {
    toast(error.message);
  }
});

refresh().catch((error) => toast(error.message));
