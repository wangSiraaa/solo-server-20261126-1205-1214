import { api, $, escapeHtml, badge, getTesterId, setTesterId, getDeviceId } from '/api.js';

const queryTester = new URLSearchParams(location.search).get('tester');
$('#tester-id').value = queryTester || getTesterId();

async function load() {
  const testerId = $('#tester-id').value.trim();
  if (!testerId) return;
  setTesterId(testerId);

  const [items, payments] = await Promise.all([
    api(`/api/settlements?testerId=${encodeURIComponent(testerId)}`),
    api(`/api/payments?testerId=${encodeURIComponent(testerId)}`),
  ]);

  const approvedUnpaid = items.filter((x) => x.settlement.status === 'approved-unpaid');
  const paid = items.filter((x) => x.settlement.status === 'paid');
  const readyAmount = approvedUnpaid.reduce((sum, x) => sum + x.settlement.amount, 0);

  $('#totals').innerHTML = `
    <div class="card" style="box-shadow:none"><div class="muted small">已发放</div><div class="kpi">${payments.totalPaid}</div><div class="muted small">点 / ${payments.payouts.length} 笔</div></div>
    <div class="card" style="box-shadow:none"><div class="muted small">待领取</div><div class="kpi">${readyAmount}</div><div class="muted small">点 / ${approvedUnpaid.length} 单</div></div>
    <div class="card" style="box-shadow:none"><div class="muted small">结算记录</div><div class="kpi">${items.length}</div><div class="muted small">同一完成永不重复建单</div></div>`;

  $('#settlements').innerHTML = items.length
    ? `<div class="table-wrap"><table>
        <thead><tr><th>任务/受理单</th><th>事前单价</th><th>金额</th><th>状态</th><th>领取</th></tr></thead>
        <tbody>${items
          .map(({ settlement, payout }) => {
            return `<tr>
              <td><span class="mono small">${escapeHtml(settlement.claimId)}</span><br>
                   <span class="small muted">${escapeHtml(settlement.settlementNo)} · ${escapeHtml(settlement.completionId)}</span></td>
              <td>${settlement.rateSnapshot.unitPrice} ${settlement.currency === 'points' ? '点' : settlement.currency}<br><span class="small muted">${escapeHtml(settlement.rateSnapshot.title)}</span></td>
              <td><strong>${settlement.amount}</strong> 点</td>
              <td>${badge(settlement.status)}${payout ? `<br><span class="small muted">${escapeHtml(payout.id)}<br>${new Date(payout.paidAt).toLocaleString()}</span>` : ''}</td>
              <td class="row">
                ${
                  payout
                    ? `<button class="secondary" data-replay="${escapeHtml(settlement.claimId)}">用另一台设备重传领取</button>
                       <span class="badge green">已发，重传不加工资</span>`
                    : `<button class="success" data-pay="${escapeHtml(settlement.claimId)}">领取 ${settlement.amount} 点</button>`
                }
                <div data-result="${escapeHtml(settlement.claimId)}"></div>
              </td>
            </tr>`;
          })
          .join('')}</tbody>
      </table></div>`
    : '<div class="notice info">还没有审核通过的结算单。请先完成试用并由招募方在审核台批准。</div>';

  document.querySelectorAll('[data-pay]').forEach((button) => {
    button.addEventListener('click', () => pay(button.dataset.pay));
  });
  document.querySelectorAll('[data-replay]').forEach((button) => {
    button.addEventListener('click', () => pay(button.dataset.replay, 'device-B-retry'));
  });
}

async function pay(claimId, deviceId = getDeviceId()) {
  const resultBox = document.querySelector(`[data-result="${claimId}"]`);
  try {
    const result = await api(`/api/claims/${encodeURIComponent(claimId)}/payout`, {
      method: 'POST',
      body: { deviceId, requestId: `payout-${claimId}` },
    });
    resultBox.innerHTML = `<div class="notice ok">${result.deduplicated ? '重复请求已去重：' : '发放成功：'} ${result.payout.amount} 点 → ${escapeHtml(result.payout.id)}</div>`;
    await load();
  } catch (error) {
    resultBox.innerHTML = `<div class="notice err">${escapeHtml(error.message)}</div>`;
  }
}

$('#load').addEventListener('click', load);
if ($('#tester-id').value) load();
