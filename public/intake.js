import { api, $, escapeHtml, badge, getTesterId, setTesterId, getDeviceId } from '/api.js';

let tasks = [];
let packages = [];
let agreements = {};

function taskPrice(task) {
  return task.agreement?.unitPrice ?? agreements[task.agreementId]?.unitPrice ?? 3;
}

function testerIdValue() {
  return $('#current-tester').value.trim();
}

async function loadTasks() {
  const config = await api('/api/config');
  tasks = config.tasks;
  agreements = config.agreements;
  $('#package-tasks').innerHTML = tasks
    .map((task) => `<option value="${escapeHtml(task.id)}">${escapeHtml(task.title)}（${escapeHtml(task.id)}）</option>`)
    .join('');
  $('#dispatch-task').innerHTML = tasks
    .map((task) => `<option value="${escapeHtml(task.id)}">${escapeHtml(task.title)} · ${taskPrice(task)}点</option>`)
    .join('');
  $('#task-list').innerHTML = `
    <table>
      <thead><tr><th>任务</th><th>步骤/发现键</th><th>约定</th></tr></thead>
      <tbody>${tasks
        .map((task) => {
          const price = taskPrice(task);
          return `<tr>
            <td><strong>${escapeHtml(task.title)}</strong><br><span class="mono small">${escapeHtml(task.id)}@v${task.version}</span></td>
            <td class="small">${task.steps.map((s) => `${s.verb}:${s.targetId}`).join(' → ')}<br><span class="muted">${escapeHtml((task.requiredDiscoveryKeys ?? []).join(', '))}</span></td>
            <td><span class="badge green">${price} 点</span></td>
          </tr>`;
        })
        .join('')}</tbody>
    </table>`;
}

async function loadPackages() {
  packages = await api('/api/packages');
  $('#package-list').innerHTML = packages.length
    ? packages
        .map(
          (pkg) => `<div class="notice info"><strong>${escapeHtml(pkg.title)}</strong> <span class="code-pill">${escapeHtml(pkg.id)}</span><br>
          任务：${pkg.taskIds.map(escapeHtml).join(', ')}<br>
          提示：${pkg.hintsSealed ? '已封存，实际拆封查看才记录曝光' : '未封存'}
          <div class="row" style="margin-top:8px">
            <button class="secondary" data-dispatch-pkg="${escapeHtml(pkg.id)}">把整包派给右侧当前试用员</button>
            <span data-pkg-result="${escapeHtml(pkg.id)}" class="small"></span>
          </div></div>`,
        )
        .join('')
    : '<p class="muted">尚未封存任务包。</p>';

  document.querySelectorAll('[data-dispatch-pkg]').forEach((button) => {
    button.addEventListener('click', async () => {
      const testerId = testerIdValue() || $('#dispatch-tester').value.trim();
      const resultBox = document.querySelector(`[data-pkg-result="${button.dataset.dispatchPkg}"]`);
      if (!testerId) {
        resultBox.innerHTML = '<span class="badge red">请先在右侧填写试用员身份</span>';
        return;
      }
      try {
        const result = await api(`/api/packages/${encodeURIComponent(button.dataset.dispatchPkg)}/dispatch`, {
          method: 'POST',
          body: { testerId },
        });
        resultBox.innerHTML = `<span class="badge green">已派出 ${result.invitations.length} 单</span>`;
        setTesterId(testerId);
        $('#current-tester').value = testerId;
        await loadInbox();
      } catch (error) {
        resultBox.innerHTML = `<span class="badge red">${escapeHtml(error.message)}</span>`;
      }
    });
  });
}

function selectedPackageTasks() {
  return [...$('#package-tasks').selectedOptions].map((option) => option.value);
}

$('#task-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const task = await api('/api/tasks', {
      method: 'POST',
      body: Object.fromEntries(form.entries()),
    });
    $('#task-result').innerHTML = ` <span class="badge green">已登记 ${escapeHtml(task.id)}</span>`;
    await loadTasks();
  } catch (error) {
    $('#task-result').innerHTML = ` <span class="badge red">${escapeHtml(error.message)}</span>`;
  }
});

$('#plan-btn').addEventListener('click', async () => {
  const taskIds = selectedPackageTasks();
  if (taskIds.length < 2) {
    $('#plan-result').innerHTML = '<div class="notice warn">请至少选择两项来检查发包顺序。</div>';
    return;
  }
  const plan = await api('/api/packages/plan', { method: 'POST', body: { taskIds } });
  $('#plan-result').innerHTML = plan.safeForOneUnmoderatedPackage
    ? `<div class="notice ok"><strong>可封入同一个无主持包。</strong><br>${escapeHtml(plan.policy)}</div>`
    : `<div class="notice err"><strong>存在提前泄题，不能按“同一人首次发现”发包。</strong>
       <ul>${plan.conflicts
         .map(
           (c) => `<li><span class="code-pill">${c.earlierTaskId}</span> 会泄露 <span class="code-pill">${c.laterTaskId}</span> 的
           ${c.sharedDiscoveryKeys.map((k) => `<span class="code-pill">${escapeHtml(k)}</span>`).join(' ')}</li>`,
         )
         .join('')}</ul>
       <p>${escapeHtml(plan.policy)}</p></div>`;
});

$('#create-package-btn').addEventListener('click', async () => {
  const taskIds = selectedPackageTasks();
  try {
    const pkg = await api('/api/packages', {
      method: 'POST',
      body: { title: '互不泄题密封包 ' + new Date().toLocaleString(), taskIds },
    });
    $('#plan-result').insertAdjacentHTML('beforeend', `<div class="notice ok">已封存：${escapeHtml(pkg.id)}</div>`);
    await loadPackages();
  } catch (error) {
    $('#plan-result').innerHTML = `<div class="notice err">${escapeHtml(error.message)}<br><pre class="json">${escapeHtml(JSON.stringify(error.data?.details?.plan ?? {}, null, 2))}</pre></div>`;
  }
});

$('#dispatch-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const body = Object.fromEntries(form.entries());
  try {
    const invitation = await api('/api/invitations/dispatch', { method: 'POST', body });
    $('#dispatch-result').innerHTML = `
      <div class="notice ok">邀约已派出：<span class="code-pill">${escapeHtml(invitation.invitationNo)}</span><br>
      试用员 <strong>${escapeHtml(invitation.testerId)}</strong> 可在下方读取。
      ${invitation.eligibilityPreview?.excluded ? `<br><span class="badge red">派出时已发现资格排除：${escapeHtml(invitation.eligibilityPreview.reason)}</span>` : '<br><span class="badge green">当前无泄题/已掌握记录</span>'}
      </div>`;
    setTesterId(invitation.testerId);
    $('#current-tester').value = invitation.testerId;
    await loadInbox();
  } catch (error) {
    $('#dispatch-result').innerHTML = `<div class="notice err">${escapeHtml(error.message)}</div>`;
  }
});

$('#save-tester').addEventListener('click', async () => {
  const id = testerIdValue();
  if (!id) return;
  setTesterId(id);
  await loadInbox();
});

$('#new-tester').addEventListener('click', async () => {
  const id = 'T-' + Math.random().toString(36).slice(2, 10);
  $('#current-tester').value = id;
  setTesterId(id);
  await api('/api/testers', { method: 'POST', body: { id, displayName: '新招募试用员' } });
  await loadInbox();
});

async function loadInbox() {
  const testerId = testerIdValue();
  if (!testerId) return;
  setTesterId(testerId);
  const [invitations, receipts] = await Promise.all([
    api(`/api/invitations?testerId=${encodeURIComponent(testerId)}`),
    api(`/api/testers/${encodeURIComponent(testerId)}/receipts`),
  ]);

  const known = receipts.history.knownKeys;
  $('#tester-history').innerHTML = `
    <div class="notice info">
      身份 <span class="code-pill">${escapeHtml(testerId)}</span>
      已实际掌握/被泄题的发现键：
      ${known.length ? known.map((k) => `<span class="badge amber">${escapeHtml(k)}</span>`).join(' ') : '<span class="muted">暂无</span>'}
      <br>已保存领取回条 <strong>${receipts.receipts.length}</strong> 张；实际提示曝光 <strong>${receipts.history.exposures.length}</strong> 次。
    </div>`;

  $('#invitation-list').innerHTML = invitations.length
    ? invitations
        .map((inv) => {
          const receipt = receipts.receipts.find((r) => r.invitationId === inv.id);
          return `<div class="card" style="box-shadow:none;margin:10px 0;padding:14px">
            <div class="row spread">
              <strong>${escapeHtml(inv.taskSnapshot.title)}</strong>
              ${badge(inv.status)}
            </div>
            <div class="small muted">${escapeHtml(inv.invitationNo)} · 单价 ${inv.agreementSnapshot.unitPrice} 点 · ${new Date(inv.dispatchedAt).toLocaleString()}</div>
            <div class="row" style="margin-top:10px">
              ${
                receipt
                  ? `<a class="button" href="/terminal.html?claim=${encodeURIComponent(receipt.claimId)}">进入试用终端</a>
                     <a class="button secondary" href="/pay.html?tester=${encodeURIComponent(testerId)}">看报酬</a>`
                  : `<button data-accept="${escapeHtml(inv.id)}" class="success">接受约定并生成领取回条</button>`
              }
            </div>
            <div data-accept-result="${escapeHtml(inv.id)}"></div>
          </div>`;
        })
        .join('')
    : '<p class="muted">该身份暂无邀约。可在左侧派出一单。</p>';

  document.querySelectorAll('[data-accept]').forEach((button) => {
    button.addEventListener('click', async () => {
      const invitationId = button.dataset.accept;
      const resultBox = document.querySelector(`[data-accept-result="${invitationId}"]`);
      try {
        const result = await api(`/api/invitations/${encodeURIComponent(invitationId)}/accept`, {
          method: 'POST',
          body: { testerId, acceptTerms: true, deviceId: getDeviceId() },
        });
        resultBox.innerHTML = `<div class="notice ok">回条 ${escapeHtml(result.claim.receiptNo)} 已保存，正在进入终端…</div>`;
        setTimeout(() => {
          location.href = `/terminal.html?claim=${encodeURIComponent(result.claim.claimId)}`;
        }, 500);
      } catch (error) {
        resultBox.innerHTML = `<div class="notice err">${escapeHtml(error.message)}</div>`;
      }
    });
  });
}

(async function init() {
  $('#current-tester').value = getTesterId();
  await Promise.all([loadTasks(), loadPackages()]);
  if (getTesterId()) await loadInbox();
})();
