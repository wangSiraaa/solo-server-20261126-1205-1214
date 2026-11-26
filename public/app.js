const $ = (id) => document.getElementById(id);

const state = {
  health: null,
  students: [],
  selectedStudentId: 'S-JIA',
  evaluation: null,
  rulebook: null,
  cases: [],
  stamps: [],
  pieces: [],
  audits: [],
  demoMatches: [],
};

async function api(method, url, body) {
  const options = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch(url, options);
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const err = new Error(data?.error?.message || `HTTP ${response.status}`);
    err.status = response.status;
    err.code = data?.error?.code;
    err.details = data?.error?.details;
    err.data = data;
    throw err;
  }
  return data;
}

function toast(message, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast';
  if (isError) el.style.border = '1px solid #ff9c92';
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function appendLog(text) {
  const out = $('scenarioOutput');
  out.textContent += `\n${text}`;
  out.scrollTop = out.scrollHeight;
}

function setLog(text) {
  $('scenarioOutput').textContent = text;
}

async function init() {
  $('refreshBtn').addEventListener('click', refreshAll);
  $('resetBtn').addEventListener('click', onReset);
  $('addStudentBtn').addEventListener('click', addStudent);
  $('recordForm').addEventListener('submit', addRecord);
  $('createCaseBtn').addEventListener('click', () => createCaseForSelected());
  $('queryStampsBtn').addEventListener('click', () => loadStamps({ studentId: state.selectedStudentId }));
  $('queryAllStampsBtn').addEventListener('click', () => loadStamps({}));
  $('happyFlowBtn').addEventListener('click', happyFlow);
  $('controlFlowBtn').addEventListener('click', controlFlow);
  $('negativeFlowBtn').addEventListener('click', showNegativeEvidence);

  try {
    state.health = await api('GET', '/api/health');
    $('health').textContent = `服务正常 · ${state.health.selectedClassType}`;
    $('health').className = 'badge ok';
  } catch (error) {
    $('health').textContent = '服务未启动';
    $('health').className = 'badge bad';
  }
  await refreshAll();
}

async function refreshAll() {
  const [students, rulebook, cases, pieces, audits, demoMatches] = await Promise.all([
    api('GET', '/api/students'),
    api('GET', '/api/rulebook'),
    api('GET', '/api/cases'),
    api('GET', '/api/pieces'),
    api('GET', '/api/audit-logs'),
    api('GET', '/api/demo-matches'),
  ]);
  state.students = students;
  state.rulebook = rulebook;
  state.cases = cases;
  state.pieces = pieces;
  state.audits = audits;
  state.demoMatches = demoMatches;
  if (!students.some((s) => s.id === state.selectedStudentId)) {
    state.selectedStudentId = students[0]?.id;
  }
  await Promise.all([loadEvaluation(), loadStamps({ studentId: state.selectedStudentId })]);
  render();
}

async function onReset() {
  if (!window.confirm('恢复初始种子会清除验题中新增的补修单、印章和规则修改，确定吗？')) return;
  await api('POST', '/api/admin/reset');
  state.selectedStudentId = 'S-JIA';
  await refreshAll();
  toast('已恢复初始验题数据');
}

async function addStudent() {
  const name = $('newStudentName').value.trim();
  if (!name) return toast('请填写学员姓名', true);
  try {
    const student = await api('POST', '/api/students', { name });
    $('newStudentName').value = '';
    state.selectedStudentId = student.id;
    await refreshAll();
    toast(`已新增并选择 ${student.id} ${student.name}`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function addRecord(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const action = form.get('action');
  const mode = form.get('mode');
  const body = {
    studentId: state.selectedStudentId,
    sessionId: form.get('sessionId'),
    pieceNo: form.get('pieceNo'),
    confirmedBy: form.get('confirmedBy'),
    events: [
      {
        action,
        mode: mode === 'assisted' ? 'hands_on' : mode,
        independence: mode === 'assisted' ? 'assisted' : mode === 'hands_on' ? 'independent' : 'not_applicable',
      },
    ],
  };
  try {
    await api('POST', '/api/records', body);
    await refreshAll();
    toast('练习记录已写入');
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadEvaluation() {
  if (!state.selectedStudentId) return;
  state.evaluation = await api(
    'GET',
    `/api/students/${encodeURIComponent(state.selectedStudentId)}/evaluation`,
  );
}

async function loadStamps(query) {
  const qs = query.studentId ? `?studentId=${encodeURIComponent(query.studentId)}` : '';
  state.stamps = await api('GET', `/api/stamps${qs}`);
  renderStamps();
}

function selectStudent(id) {
  state.selectedStudentId = id;
  loadEvaluation().then(render).catch((err) => toast(err.message, true));
  loadStamps({ studentId: id });
}

function render() {
  renderOpening();
  renderStudents();
  renderEvaluation();
  renderRules();
  renderCases();
  renderStamps();
  renderPieces();
  renderAudits();
}

function renderOpening() {
  const first = state.demoMatches[0];
  $('openingAnswer').textContent = first
    ? first.answer
    : '当前种子中没有补看示范记录。';
  $('demoMatches').innerHTML = state.demoMatches
    .map(
      (m) => `<div class="demo-item">
        <strong>${esc(m.studentName)}（${esc(m.studentId)}）</strong>：
        示范事件 <code>${esc(m.demoEventId)}</code> 来自 ${esc(m.demoSessionId)} / ${esc(m.demoPieceNo)}。
        <div>流程见闻档：${pill(m.auditWorkflowComplete, '可补完整', '仍不完整')}
        ${pill(m.practicalCompletionComplete, '可发亲手结课章', '亲手结课章仍不可发')}</div>
      </div>`,
    )
    .join('');
}

function pill(ok, yes, no) {
  if (ok === undefined || ok === null) return '';
  return `<span class="pill ${ok ? 'green' : 'red'}">${ok ? `✓ ${yes || '满足'}` : `✕ ${no || '不满足'}`}</span>`;
}

function renderStudents() {
  $('studentList').innerHTML = state.students
    .map(
      (s) => `<button class="chip ${s.id === state.selectedStudentId ? 'active' : ''}" data-id="${esc(s.id)}">
        ${esc(s.name)} · ${esc(s.id)}
      </button>`,
    )
    .join('');
  document.querySelectorAll('.chip[data-id]').forEach((btn) => {
    btn.onclick = () => selectStudent(btn.dataset.id);
  });
}

function renderEvaluation() {
  const ev = state.evaluation;
  if (!ev) {
    $('studentDetail').innerHTML = '<p class="muted">请选择学员。</p>';
    return;
  }
  const transfer = ev.transferableCredits
    .map((c) => pill(c.satisfied, `${c.name}有效`, `${c.name}未完成`))
    .join('');
  const groups = ev.groups
    .map((g) => {
      const gaps = g.outstandingSegments
        .map(
          (s) => `<div class="rule">
            <strong>待补连续段：${s.segmentNames.join(' → ')}</strong>
            <div>${s.retainable ? '<span class="pill blue">可保留同件</span>' : ''}
              ${s.pieceRecycled ? '<span class="pill red">泥件已回收</span>' : ''}
              ${s.canBeFilledByObservation ? '<span class="pill amber">允许旁听补入</span>' : '<span class="pill red">观摩不可替代亲手</span>'}
            </div>
            <p class="muted">缺口键 <code>${esc(s.gapKey)}</code><br/>
            须亲手新增：<strong>${s.requiredNewActionNames.join('、') || '无'}</strong><br/>
            锚点泥件：<code>${esc(s.pieceNo || '无')}</code> ${esc(s.pieceUid || '')}</p>
            <div class="reason">${esc((s.reasonCodes || []).join(', '))}</div>
          </div>`,
        )
        .join('');
      return `<div class="rule">
        <h4>${esc(g.name)} <span class="pill ${g.satisfied ? 'green' : 'red'}">${g.satisfied ? '已完整学成' : '未完整'}</span>
          ${g.stampType === 'audit_only' ? '<span class="pill amber">见闻档</span>' : ''}
        </h4>
        <div class="muted">动作顺序：${g.actionNames.join(' → ')}</div>
        ${g.satisfied ? `<p><span class="pill green">证据：${g.chosenEvidence.map((e) => esc(e.id)).join(' / ')}</span></p>` : gaps}
      </div>`;
    })
    .join('');

  $('studentDetail').innerHTML = `
    <dl class="kv">
      <dt>学员</dt><dd><strong>${esc(ev.student.name)}</strong>（${esc(ev.student.id)}）— ${esc(ev.student.note || '')}</dd>
      <dt>规则版本</dt><dd>v${esc(ev.rulebookVersion)}；动手历史版本 ${esc(ev.historyRevision)}；指纹 <code>${esc(ev.historyFingerprint)}</code></dd>
      <dt>已完整组</dt><dd>${ev.completedGroups.map((g) => `<span class="pill green">${esc(g.name)}</span>`).join('') || '<span class="pill gray">暂无</span>'}</dd>
      <dt>可迁移项</dt><dd>${transfer || '<span class="pill gray">无</span>'}</dd>
    </dl>
    ${groups}`;
}

function renderRules() {
  $('ruleList').innerHTML = state.rulebook.groups
    .map(
      (g) => `
      <div class="rule">
        <h4>${esc(g.name)} <span class="pill gray">${esc(g.code)}</span></h4>
        ${g.links
          .map(
            (l) => `<div class="case">
              <strong>${esc(l.from)} → ${esc(l.to)}</strong> <code>${esc(l.id)}</code>
              <p class="muted">${esc(l.description || '')}</p>
              <div class="rule-controls">
                <label class="switch"><input type="checkbox" data-rule="${esc(l.id)}" data-field="samePiece" ${l.samePiece ? 'checked' : ''}/>必须同件</label>
                <label class="switch"><input type="checkbox" data-rule="${esc(l.id)}" data-field="sameContinuousChain" ${l.sameContinuousChain ? 'checked' : ''}/>同一连续练习</label>
                <label class="switch"><input type="checkbox" data-rule="${esc(l.id)}" data-field="supplementByAudit" ${l.supplementByAudit ? 'checked' : ''}/>允许旁听补入</label>
                <button class="ghost" data-save-rule="${esc(l.id)}">保存该条规定</button>
              </div>
            </div>`,
          )
          .join('')}
      </div>`,
    )
    .join('');

  document.querySelectorAll('[data-save-rule]').forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.dataset.saveRule;
      const body = { changedBy: 'WEB-VERIFIER' };
      document.querySelectorAll(`[data-rule="${id}"]`).forEach((input) => {
        body[input.dataset.field] = input.checked;
      });
      try {
        const result = await api('PATCH', `/api/rules/links/${encodeURIComponent(id)}`, body);
        toast(`规则已更新到 v${result.rulebookVersion}，可重新办理同一缺口`);
        await refreshAll();
      } catch (error) {
        toast(error.message, true);
      }
    };
  });
}

async function createCaseForSelected(extraBody = {}) {
  try {
    const result = await api('POST', '/api/cases', {
      studentId: state.selectedStudentId,
      classType: 'retained_original',
      ...extraBody,
    });
    state.cases = await api('GET', '/api/cases');
    renderCases();
    toast(result.existing ? '该缺口已有补修单，未重复创建' : `已创建补修单 ${result.case.id}`);
    return result;
  } catch (error) {
    if (error.code === 'NOTHING_TO_MAKE_UP') toast('当前规则下没有缺口，无需补修');
    else toast(error.message, true);
    throw error;
  }
}

function renderCases() {
  if (!state.cases.length) {
    $('caseList').innerHTML = '<p class="muted">还没有补修单。</p>';
    return;
  }
  $('caseList').innerHTML = state.cases
    .slice()
    .reverse()
    .map((c) => {
      const reservation = c.reservation
        ? `<dl class="kv">
            <dt>保留泥件</dt><dd><code>${esc(c.reservation.pieceNo)}</code> / ${esc(c.reservation.pieceUid)}</dd>
            <dt>归属学员</dt><dd>${esc(c.reservation.forStudentId)}</dd>
            <dt>存放位</dt><dd>${esc(c.reservation.storageSlot)}</dd>
            <dt>到课场次/台位</dt><dd>${esc(c.reservation.sessionId)} · ${esc(c.reservation.tableName)}（${esc(c.reservation.tableId)}）</dd>
          </dl>`
        : '<p class="muted">无原件保留安排。</p>';
      const decision = c.mentorDecision
        ? `<p><span class="pill ${c.stampId ? 'green' : 'amber'}">${c.stampId ? '已签结课章' : '已签下班补做通知'}</span>${esc(c.mentorDecision.statement)}</p>`
        : '';
      return `<article class="case">
        <h4>${esc(c.id)} <span class="pill blue">${esc(c.status)}</span> ${c.stampId ? `<span class="stamp-id">${esc(c.stampId)}</span>` : ''}</h4>
        <div class="muted">学员 <strong>${esc(c.studentId)}</strong> · 动作段 ${c.segmentNames.map(esc).join(' → ')} · 须新增 ${c.requiredNewActionNames.map(esc).join('、')}</div>
        ${reservation}
        ${decision}
        <div class="case-actions">
          <button data-action="attendance" data-id="${esc(c.id)}">真实到课签到</button>
          <button data-action="valid-receipt" data-id="${esc(c.id)}">交同件亲手完成回执</button>
          <button class="secondary" data-action="observed-receipt" data-id="${esc(c.id)}">反例：交观摩回执</button>
          <button class="secondary" data-action="wrong-piece" data-id="${esc(c.id)}">反例：另一件亲手</button>
          <button data-action="confirm-lin" data-id="${esc(c.id)}">导师林确认办理</button>
          <button class="secondary" data-action="confirm-he" data-id="${esc(c.id)}">第二名导师何重办</button>
        </div>
      </article>`;
    })
    .join('');

  document.querySelectorAll('[data-action]').forEach((btn) => {
    btn.onclick = () => caseAction(btn.dataset.id, btn.dataset.action);
  });
}

async function caseAction(id, action) {
  try {
    if (action === 'attendance') {
      await api('POST', `/api/cases/${id}/attendance`, { confirmedBy: 'MENTOR-LIN' });
      toast('已记录真实到课');
    } else if (action === 'valid-receipt') {
      const c = state.cases.find((x) => x.id === id);
      const targetAction = c?.requiredNewActions?.[0] || c?.segment?.at(-1) || 'close';
      await api('POST', `/api/cases/${id}/receipt`, {
        action: targetAction,
        mode: 'hands_on',
        pieceNo: c?.reservation?.pieceNo,
        pieceUid: c?.reservation?.pieceUid,
        confirmedBy: 'TA-ZHOU',
      });
      toast('真实亲手完成回执已收录');
    } else if (action === 'observed-receipt') {
      await expectError(() =>
        api('POST', `/api/cases/${id}/receipt`, { action: 'close', mode: 'observed', confirmedBy: 'TA-ZHOU' }),
      );
    } else if (action === 'wrong-piece') {
      const c = state.cases.find((x) => x.id === id);
      await expectError(() =>
        api('POST', `/api/cases/${id}/receipt`, {
          action: c?.requiredNewActions?.[0] || 'close',
          mode: 'hands_on',
          pieceNo: c?.studentId === 'S-JIA' ? 'P-JIA-02' : 'P-OTHER-NUMBER',
          confirmedBy: 'TA-ZHOU',
        }),
      );
    } else if (action === 'confirm-lin') {
      const result = await api('POST', `/api/cases/${id}/confirm`, { mentorId: 'MENTOR-LIN' });
      toast(result.stamp ? `已盖章：${result.stamp.id}` : '已签下班实际补做段');
    } else if (action === 'confirm-he') {
      const result = await api('POST', `/api/cases/${id}/confirm`, { mentorId: 'MENTOR-HE' });
      toast(result.duplicate ? `重复办理，仍只有 ${result.stamp?.id}` : '办理完成');
    }
    await refreshAll();
  } catch (error) {
    toast(`${error.code || 'ERROR'}：${error.message}`, true);
    await refreshAll();
  }
}

async function expectError(fn) {
  try {
    const result = await fn();
    appendLog(`预期应失败，但返回：${pretty(result)}`);
    toast('反例未被拦住，请检查规则', true);
    return result;
  } catch (error) {
    appendLog(`已正确拦截 ${error.code}: ${error.message}`);
    toast(`反例已拦截：${error.code}`);
    return { __expectedError: error.code };
  }
}

function renderStamps() {
  if (!state.stamps.length) {
    $('stampList').innerHTML = '<p class="muted">当前查询没有结课章。</p>';
    return;
  }
  $('stampList').innerHTML = state.stamps
    .map(
      (s) => `<div class="stamp">
        <h4>${esc(s.title)}</h4>
        <div class="stamp-id">${esc(s.id)}</div>
        <div>学员：${esc(s.studentId)} · 动作组：${esc(s.groupCode)}</div>
        <div class="muted">签发导师：${esc(s.mentorId)} · 时间：${esc(s.issuedAt)} · 规则 v${esc(s.rulebookVersion)}</div>
      </div>`,
    )
    .join('');
}

function renderPieces() {
  $('pieceList').innerHTML = `<table>
    <thead><tr><th>泥件号 / UID</th><th>归属</th><th>状态</th><th>保留位/台</th><th>回收</th><th>操作</th></tr></thead>
    <tbody>${state.pieces
      .map(
        (p) => `<tr>
          <td><strong>${esc(p.no)}</strong><br/><code class="muted">${esc(p.uid)}</code></td>
          <td>${esc(p.studentId || '示范/公用')}</td>
          <td><span class="pill ${p.recycled ? 'red' : p.status === 'reserved' ? 'blue' : 'gray'}">${esc(p.status)}</span></td>
          <td>${esc(p.storageSlot || '—')}<br/>${esc(p.continueTableId || '')}</td>
          <td>${p.recycled ? `已回收<br/><span class="muted">${esc(p.recycledAt || '')}</span>` : '未回收'}</td>
          <td>${p.recycled ? '' : `<button class="danger-soft" data-recycle="${esc(p.no)}">回收重揉</button>`}</td>
        </tr>`,
      )
      .join('')}</tbody>
  </table>`;
  document.querySelectorAll('[data-recycle]').forEach((btn) => {
    btn.onclick = async () => {
      if (!window.confirm(`确认把 ${btn.dataset.recycle} 回收重揉？未完成连续段将不能再算。`)) return;
      await api('POST', `/api/pieces/${encodeURIComponent(btn.dataset.recycle)}/recycle`, { actor: 'WEB-VERIFIER' });
      await refreshAll();
      toast('泥件已回收');
    };
  });
}

function renderAudits() {
  $('auditList').innerHTML = state.audits
    .slice(0, 30)
    .map(
      (a) => `<div class="audit-item"><strong>${esc(a.action)}</strong> · ${esc(a.actor)}
        <div class="muted">${esc(a.at)}</div><div>${esc(a.message)}</div></div>`,
    )
    .join('');
}

async function showNegativeEvidence() {
  setLog('核对甲的现行规则评估…');
  const ev = await api('GET', '/api/students/S-JIA/evaluation');
  const gap = ev.outstandingSegments.find((s) => s.linkIds.includes('L-WALL-CLOSE'));
  appendLog(pretty({ gap, note: 'P-JIA-02 的亲手收口因 piece_mismatch 被拒；观摩收口因 mode_observed_not_accepted 被拒。' }));
}

async function happyFlow() {
  setLog('1) 恢复种子，开始甲的保留原件完整流程…');
  await api('POST', '/api/admin/reset');
  state.selectedStudentId = 'S-JIA';
  await refreshAll();

  const created = await api('POST', '/api/cases', {
    studentId: 'S-JIA',
    groupCode: 'BASIC_CYLINDER',
    linkId: 'L-WALL-CLOSE',
  });
  appendLog(`2) 补修单：${created.case.id}；保留 ${created.case.reservation.pieceNo} 在 ${created.case.reservation.sessionId} / ${created.case.reservation.tableId}`);

  await api('POST', `/api/cases/${created.case.id}/attendance`, { confirmedBy: 'MENTOR-LIN' });
  appendLog('3) 甲已真实到课并在保留台位签到。');

  const observedError = await expectError(() =>
    api('POST', `/api/cases/${created.case.id}/receipt`, { action: 'close', mode: 'observed' }),
  );
  const wrongError = await expectError(() =>
    api('POST', `/api/cases/${created.case.id}/receipt`, {
      action: 'close',
      mode: 'hands_on',
      pieceNo: 'P-JIA-02',
    }),
  );
  appendLog(`4) 两个拼课反例均被拦住：${observedError.__expectedError} / ${wrongError.__expectedError}`);

  const receipt = await api('POST', `/api/cases/${created.case.id}/receipt`, {
    action: 'close',
    mode: 'hands_on',
    pieceNo: 'P-JIA-01',
    confirmedBy: 'TA-ZHOU',
  });
  appendLog(`5) 同件亲手回执：${receipt.receipt.id} / ${receipt.recordId}`);

  const [lin, he] = await Promise.all([
    api('POST', `/api/cases/${created.case.id}/confirm`, { mentorId: 'MENTOR-LIN' }),
    api('POST', `/api/cases/${created.case.id}/confirm`, { mentorId: 'MENTOR-HE' }),
  ]);
  const resend = await api('POST', `/api/cases/${created.case.id}/confirm`, { mentorId: 'MENTOR-LIN' });
  const stamps = await api('GET', '/api/stamps?studentId=S-JIA');
  const sameStamp = lin.stamp.id === he.stamp.id && he.stamp.id === resend.stamp.id;
  const jiaStamps = stamps.filter((s) => s.groupCode === 'BASIC_CYLINDER');
  appendLog(
    pretty({
      linCreated: lin.created,
      heDuplicate: he.duplicate,
      resendDuplicate: resend.duplicate,
      stampId: lin.stamp.id,
      sameStampForConcurrentAndResend: sameStamp,
      basicStampCount: jiaStamps.length,
      statement: lin.signedDecision.statement,
      pass: sameStamp && jiaStamps.length === 1,
    }),
  );
  await refreshAll();
  toast(sameStamp && jiaStamps.length === 1 ? '一键验题通过：只发一份章' : '印章幂等检查失败', !(sameStamp && jiaStamps.length === 1));
}

async function controlFlow() {
  setLog('对照组：恢复种子，乙的动手历史保持不变…');
  await api('POST', '/api/admin/reset');
  const before = await api('GET', '/api/students/S-YI/evaluation');
  const beforeOk = before.groups.find((g) => g.code === 'HANDLE_JOIN_CONTROL').satisfied;
  const fingerprint = before.historyFingerprint;
  appendLog(`规则修改前 HANDLE_JOIN_CONTROL.satisfied=${beforeOk}，fingerprint=${fingerprint}`);

  const updated = await api('PATCH', '/api/rules/links/L-HANDLE-JOIN', { samePiece: true, changedBy: 'VERIFIER' });
  const after = await api('GET', '/api/students/S-YI/evaluation');
  const afterGroup = after.groups.find((g) => g.code === 'HANDLE_JOIN_CONTROL');
  appendLog(`规则已改为必须同件 v${updated.rulebookVersion}；修改后 satisfied=${afterGroup.satisfied}`);
  appendLog(`历史指纹不变：${after.historyFingerprint === fingerprint}`);
  appendLog(pretty(afterGroup.outstandingSegments[0]?.reasonCodes || afterGroup.linkResults));

  const created = await api('POST', '/api/cases', {
    studentId: 'S-YI',
    groupCode: 'HANDLE_JOIN_CONTROL',
    linkId: 'L-HANDLE-JOIN',
  });
  const firstDecision = await api('POST', `/api/cases/${created.case.id}/confirm`, { mentorId: 'MENTOR-LIN' });
  appendLog(`未实际补做前：${firstDecision.signedDecision.decision}；印章=${firstDecision.stamp?.id || '无'}`);
  appendLog(firstDecision.signedDecision.statement);

  await api('POST', `/api/cases/${created.case.id}/attendance`, { confirmedBy: 'MENTOR-LIN' });
  await api('POST', `/api/cases/${created.case.id}/receipt`, {
    action: 'smooth_join',
    mode: 'hands_on',
    pieceNo: 'P-YI-H1',
    confirmedBy: 'TA-ZHOU',
  });
  const done = await api('POST', `/api/cases/${created.case.id}/confirm`, { mentorId: 'MENTOR-LIN' });
  appendLog(`乙到保留原件台实际补做后：${done.stamp?.id || '未盖章'}`);
  await refreshAll();
}

init().catch((error) => {
  console.error(error);
  toast(error.message, true);
});
