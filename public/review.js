import { api, $, escapeHtml, badge } from '/api.js';

const highlightCompletion = new URLSearchParams(location.search).get('completion');

async function load() {
  const [queue, audit, health] = await Promise.all([
    api('/api/review/queue'),
    api('/api/audit'),
    api('/api/health'),
  ]);

  $('#data-file').textContent = health.dataFile;
  $('#counts').innerHTML = Object.entries(audit.counts)
    .map(([key, value]) => `<div class="card"><div class="muted small">${escapeHtml(key)}</div><div class="kpi">${value}</div></div>`)
    .join('');

  $('#queue').innerHTML = queue.length
    ? queue
        .map(({ completion, claim, settlement, review }) => {
          const v = completion.verdict;
          const highlighted = highlightCompletion === completion.id ? 'style="outline:3px solid #93c5fd"' : '';
          return `<div class="card" ${highlighted} style="box-shadow:none;border-color:${v.discoveryPayable ? '#abefc6' : '#fecdca'};margin-bottom:14px">
            <div class="row spread">
              <div>
                <strong>${escapeHtml(claim.taskTitle)}</strong><br>
                <span class="mono small">${escapeHtml(completion.id)}</span> ·
                <span class="code-pill">${escapeHtml(completion.testerId)}</span> ·
                ${new Date(completion.submittedAt).toLocaleString()}
              </div>
              <div class="row">
                ${badge(completion.status)}
                ${v.behaviorPassed ? '<span class="badge green">行为通过</span>' : '<span class="badge red">行为失败</span>'}
                ${v.discoveryPayable ? `<span class="badge green">可付 ${v.payableAmount} 点</span>` : '<span class="badge red">不付发现奖</span>'}
              </div>
            </div>
            <div class="notice ${v.discoveryPayable ? 'ok' : v.behaviorPassed ? 'warn' : 'err'}">${escapeHtml(v.summary)}</div>
            <div class="grid cols-2">
              <div class="table-wrap"><table>
                <tbody>${v.checks
                  .map(
                    (check) => `<tr><td>${escapeHtml(check.id)}</td><td>${check.ok ? '✓' : '✕'}</td><td class="small">${escapeHtml(check.detail)}</td></tr>`,
                  )
                  .join('')}</tbody>
              </table></div>
              <div class="small">
                <p><strong>受理回条：</strong><span class="code-pill">${escapeHtml(claim.receiptNo)}</span></p>
                <p><strong>事前约定：</strong>${escapeHtml(claim.agreementSnapshot.title)}，单价 <strong>${claim.agreementSnapshot.unitPrice}</strong> ${claim.agreementSnapshot.currency}</p>
                <p><strong>设备：</strong>${(claim.devices ?? []).map((d) => escapeHtml(d.deviceId)).join(', ') || '未登记'}</p>
                <p><strong>重传：</strong>${completion.deviceIds?.length ?? 0} 台设备 / ${completion.retransmissions?.length ?? 0} 次额外重传</p>
                ${v.eligibility?.sourceReceiptNos?.length ? `<p><strong>泄题来源回条：</strong>${v.eligibility.sourceReceiptNos.map((n) => `<span class="badge red">${escapeHtml(n)}</span>`).join(' ')}</p>` : ''}
                ${settlement ? `<p><strong>结算单：</strong>${escapeHtml(settlement.settlementNo)} · ${settlement.amount} 点 · ${badge(settlement.status)}</p>` : ''}
                ${review ? `<p><strong>审核：</strong>${escapeHtml(review.decision)} / ${escapeHtml(review.reviewerName)}</p>` : ''}
                <div class="row" style="margin-top:12px">
                  ${
                    completion.status === 'awaiting-review'
                      ? `<button class="success" data-approve="${escapeHtml(completion.id)}">审核通过并按原价立结算单</button>
                         <button class="secondary" data-block="${escapeHtml(completion.id)}">确认行为成立但不付发现奖</button>
                         <button class="danger" data-reject="${escapeHtml(completion.id)}">驳回</button>`
                      : '<span class="muted small">已处理；可在下方台账复查。</span>'
                  }
                </div>
                <div data-review-result="${escapeHtml(completion.id)}"></div>
              </div>
            </div>
          </div>`;
        })
        .join('')
    : '<div class="notice info">还没有完成记录。可运行 <span class="code-pill">npm run verify</span> 或在试用终端提交。</div>';

  document.querySelectorAll('[data-approve]').forEach((btn) =>
    btn.addEventListener('click', () => review(btn.dataset.approve, 'approve')),
  );
  document.querySelectorAll('[data-block]').forEach((btn) =>
    btn.addEventListener('click', () => review(btn.dataset.block, 'acknowledge-blocked')),
  );
  document.querySelectorAll('[data-reject]').forEach((btn) =>
    btn.addEventListener('click', () => review(btn.dataset.reject, 'reject')),
  );

  $('#receipts').innerHTML = audit.claims.length
    ? audit.claims
        .slice()
        .reverse()
        .map((claim) => {
          const exposures = audit.exposures.filter((e) => e.claimId === claim.id);
          return `<div style="border-bottom:1px solid var(--line);padding:10px 0">
            <div class="row spread"><strong>${escapeHtml(claim.receiptNo)}</strong>${badge(claim.status)}</div>
            <div class="small muted">${escapeHtml(claim.testerId)} · ${escapeHtml(claim.taskSnapshot.title)} · ${claim.agreementSnapshot.unitPrice}点</div>
            ${exposures.map((e) => `<div class="notice warn small" style="margin:6px 0">实际展示提示：${escapeHtml(e.hintId)} · ${new Date(e.shownAt).toLocaleString()}<br>来源回条 ${escapeHtml(e.sourceReceiptNo)}</div>`).join('')}
          </div>`;
        })
        .join('')
    : '<p class="muted small">暂无回条。</p>';

  $('#ledger').innerHTML = `
    <h3>结算单</h3>
    ${audit.settlements.length ? audit.settlements.map((s) => `<div class="small" style="padding:6px 0;border-bottom:1px dashed var(--line)">${escapeHtml(s.settlementNo)} · ${escapeHtml(s.testerId)} · <strong>${s.amount}点</strong> · ${badge(s.status)}</div>`).join('') : '<p class="muted small">无</p>'}
    <h3>发放流水</h3>
    ${audit.payouts.length ? audit.payouts.map((p) => `<div class="small" style="padding:6px 0;border-bottom:1px dashed var(--line)">${escapeHtml(p.id)} · ${escapeHtml(p.testerId)} · <strong>${p.amount}点</strong> · ${new Date(p.paidAt).toLocaleString()}</div>`).join('') : '<p class="muted small">无</p>'}
  `;

  $('#raw-audit').textContent = JSON.stringify(
    {
      meta: audit.meta,
      counts: audit.counts,
      invitations: audit.invitations.map((x) => ({ id: x.id, no: x.invitationNo, tester: x.testerId, task: x.taskId, status: x.status })),
      claims: audit.claims.map((x) => ({ id: x.id, receipt: x.receiptNo, tester: x.testerId, status: x.status, acceptedAt: x.acceptedAt })),
      exposures: audit.exposures,
      completions: audit.completions.map((x) => ({ id: x.id, claimId: x.claimId, status: x.status, payable: x.verdict.discoveryPayable, amount: x.verdict.payableAmount, devices: x.deviceIds, retransmissions: x.retransmissions.length })),
      settlements: audit.settlements,
      payouts: audit.payouts,
    },
    null,
    2,
  );
}

async function review(completionId, decision) {
  const box = document.querySelector(`[data-review-result="${completionId}"]`);
  try {
    await api(`/api/completions/${encodeURIComponent(completionId)}/review`, {
      method: 'POST',
      body: { decision, reviewerName: '招募方负责人', note: decision === 'approve' ? '轨迹与资格复核通过' : '' },
    });
    await load();
  } catch (error) {
    box.innerHTML = `<div class="notice err">${escapeHtml(error.message)}<br><pre class="json">${escapeHtml(JSON.stringify(error.data?.details ?? {}, null, 2))}</pre></div>`;
  }
}

$('#refresh').addEventListener('click', load);
load().catch((error) => {
  $('#queue').innerHTML = `<div class="notice err">${escapeHtml(error.message)}。请确认 <span class="code-pill">npm start</span> 已运行。</div>`;
});
