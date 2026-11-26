import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, '..');
const PORT = Number(process.env.E2E_PORT || 4319);
const BASE = `http://127.0.0.1:${PORT}`;
const DATA_FILE = process.env.E2E_DATA_FILE || path.join(os.tmpdir(), `verified-review-e2e-${Date.now()}.json`);

let child;
let logs = '';

function startServer() {
  logs = '';
  child = spawn(process.execPath, [path.join(root, 'server.js')], {
    cwd: root,
    env: { ...process.env, PORT: String(PORT), HOST: '127.0.0.1', DATA_FILE },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (d) => { logs += d.toString(); });
  child.stderr.on('data', (d) => { logs += d.toString(); });
}

function stopServer() {
  if (!child || child.killed) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(() => child.kill('SIGKILL'), 3000);
    child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
    child.kill('SIGTERM');
  });
}

async function waitHealthy(timeoutMs = 10000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(`${BASE}/health`);
      if (res.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(`server did not become healthy\n${logs}`);
}

async function api(method, route, payload, headers = {}) {
  const res = await fetch(`${BASE}${route}`, {
    method,
    headers: {
      ...(payload === undefined ? {} : { 'content-type': 'application/json' }),
      ...headers,
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const text = await res.text();
  let json;
  try { json = text ? JSON.parse(text) : null; } catch { json = { rawHtml: text }; }
  return { status: res.status, json, text };
}

function assert(condition, message, extra) {
  if (!condition) {
    throw new Error(`${message}${extra ? `\n${typeof extra === 'string' ? extra : JSON.stringify(extra, null, 2)}` : ''}`);
  }
}

async function expectStatus(promise, expected, label) {
  const result = await promise;
  assert(expected.includes(result.status), `${label}: expected status ${expected.join('/')}, got ${result.status}`, result.json || result.text);
  return result;
}

async function runScenarios() {
  console.log('1) 正常收过乙、发文仍持有乙、同意回答：评价 -> 商家询问 -> 本人账号回复 -> 产品页后续');
  await api('POST', '/api/products', { modelCode: 'YI', name: '乙款' });
  await api('POST', '/api/products', { modelCode: 'JIA', name: '甲款' });

  await api('POST', '/api/buyers', {
    id: 'byr_ok',
    displayName: '乙款实测买家',
    agreesToAnswer: true,
  });
  await api('POST', '/api/orders', {
    id: 'ord_ok',
    buyerId: 'byr_ok',
    orderedModelCode: 'JIA',
    units: [{ id: 'u_ok', orderedModelCode: 'JIA' }],
  });
  await api('POST', '/api/deliveries', {
    id: 'rcp_ok',
    orderId: 'ord_ok',
    unitId: 'u_ok',
    modelCode: 'YI',
    voucherNo: 'VCH-OK-YI',
    proof: { signer: '本人', parcel: 'split-1' },
  });
  const review = await expectStatus(api('POST', '/api/reviews', {
    id: 'rvw_ok',
    buyerId: 'byr_ok',
    modelCode: 'YI',
    receiptId: 'rcp_ok',
    rating: 5,
    title: '乙款佩戴稳固',
    body: 'UNIQUE_OK_BODY 我确实收到替代品乙，发文时仍持有。',
    agreesToAnswer: true,
  }, { 'x-buyer-id': 'byr_ok' }), [201], '发布乙款实测');
  assert(review.json.verification.status === 'VERIFIED', '乙款评价应核对为已实测', review.json);

  const question = await expectStatus(api('POST', '/api/reviews/rvw_ok/questions', {
    id: 'q_ok',
    body: '想补充问：长时间佩戴会压耳吗？',
  }), [201], '商家针对具体乙体验发询问');
  assert(question.json.question.buyerId === 'byr_ok', '询问必须路由给对应试用人 byr_ok');

  const inbox = await api('GET', '/api/account/questions', undefined, { 'x-buyer-id': 'byr_ok' });
  assert(inbox.status === 200 && inbox.json.count === 1 && inbox.json.inbox[0].id === 'q_ok', '买家正常账号收件箱应看到该询问', inbox.json);

  await expectStatus(api('POST', '/api/questions/q_ok/open', {}, { 'x-buyer-id': 'byr_ok' }), [200], '买家从本人账号打开问题');
  const answer = await expectStatus(api('POST', '/api/questions/q_ok/answer', {
    buyerId: 'byr_ok',
    body: 'UNIQUE_OK_ANSWER 六小时略有压感，但可以接受。',
  }, { 'x-buyer-id': 'byr_ok' }), [201], '买家回复询问');
  assert(answer.json.readerPath === '/p/YI#followup-q_ok', '答复应给出产品页后续锚点', answer.json);

  const yiPage = await api('GET', '/api/products/YI/page');
  assert(yiPage.json.reviews.length === 1, '乙款产品 JSON 应有一篇首测', yiPage.json);
  assert(yiPage.json.followUpIndex[0]?.url === '/p/YI#followup-q_ok', '产品 JSON 后续索引应指向答案');
  const yiHtml = await api('GET', '/p/YI');
  assert(yiHtml.text.includes('UNIQUE_OK_BODY'), '乙款 HTML 产品页应包含原测');
  assert(yiHtml.text.includes('UNIQUE_OK_ANSWER'), '乙款 HTML 产品页应包含后续答复');
  assert(yiHtml.text.includes('id="followup-q_ok"'), 'HTML 应有后续锚点');
  assert(yiHtml.text.includes('VCH-OK-YI'), 'HTML 应展示可核对凭证号');

  console.log('2) 复做并只把乙凭证更正为拒收：旧刊缺依据、未回询问作废、不能作答、不转款');
  await api('POST', '/api/buyers', { id: 'byr_corr', displayName: '凭证更正买家' });
  await api('POST', '/api/orders', {
    id: 'ord_corr', buyerId: 'byr_corr', orderedModelCode: 'JIA',
    units: [{ id: 'u_corr' }],
  });
  await api('POST', '/api/deliveries', {
    id: 'rcp_corr', orderId: 'ord_corr', unitId: 'u_corr', modelCode: 'YI',
  });
  await expectStatus(api('POST', '/api/reviews', {
    id: 'rvw_corr', buyerId: 'byr_corr', modelCode: 'YI', receiptId: 'rcp_corr',
    body: 'UNIQUE_CORR_BODY 这篇之后会因为改成拒收而失去依据。',
  }, { 'x-buyer-id': 'byr_corr' }), [201], '先刊出待更正评价');
  await expectStatus(api('POST', '/api/reviews/rvw_corr/questions', { id: 'q_corr', body: '这条询问尚未回答。' }), [201], '发出待作废询问');

  const correction = await api('POST', '/api/receipts/rcp_corr/correct', {
    correctTo: 'REJECTION',
    reason: '复做：单把乙的收货凭证改为拒收',
    correctedBy: 'e2e-audit',
  });
  assert(correction.status === 200, '凭证更正应成功', correction.json);
  assert(correction.json.effects.affectedReviews[0]?.reviewId === 'rvw_corr', '旧刊应被冻结');
  assert(correction.json.effects.voidedQuestions[0] === 'q_corr', '未回询问应作废');

  const oldReview = await api('GET', '/api/reviews/rvw_corr?audit=true', undefined, { 'x-merchant-token': 'merchant-dev-token' });
  assert(oldReview.json.status === 'WITHHELD', '旧刊状态应为 WITHHELD');
  assert(oldReview.json.modelCode === 'YI', '旧刊仍必须挂在乙款，不能静默搬走');
  assert(oldReview.json.modelMovedTo === undefined, '不得出现转款目标');

  const voidAnswer = await api('POST', '/api/questions/q_corr/answer', {
    buyerId: 'byr_corr', body: '不应成功',
  }, { 'x-buyer-id': 'byr_corr' });
  assert(voidAnswer.status === 409 && voidAnswer.json.error.code === 'QUESTION_VOIDED', '作废询问不能再收答复', voidAnswer.json);

  const yiAfterCorrection = await api('GET', '/api/products/YI/page');
  assert(!yiAfterCorrection.json.reviews.some((r) => r.id === 'rvw_corr'), '失效旧刊不得作为公开实测');
  assert(yiAfterCorrection.json.correctionNotices.some((n) => n.id === 'rvw_corr' && n.modelCode === 'YI'), '应有不转款的更正启事');
  const htmlAfterCorrection = await api('GET', '/p/YI');
  assert(!htmlAfterCorrection.text.includes('UNIQUE_CORR_BODY'), '失效评价正文不得留在公开产品页');

  const rejectedBeforeReview = await api('POST', '/api/receipts', {
    id: 'rcp_reject_direct', orderId: 'ord_corr', unitId: 'u_corr', type: 'REJECTION', modelCode: 'YI',
  });
  assert(rejectedBeforeReview.status === 201, '可另开拒收凭证');
  const directRejectReview = await api('POST', '/api/reviews', {
    buyerId: 'byr_corr', modelCode: 'YI', receiptId: 'rcp_reject_direct', body: '拒收不应能发实测',
  }, { 'x-buyer-id': 'byr_corr' });
  assert(directRejectReview.status === 422 && directRejectReview.json.error.details?.code === 'DELIVERY_REJECTED', '拒收凭证发文应被拒绝', directRejectReview.json);

  console.log('3) 正常收货重做，但买家拒收/拒绝后续询问：原测保留，商家没有已答复回条');
  await api('POST', '/api/buyers', { id: 'byr_decline', displayName: '不愿追访买家' });
  await api('POST', '/api/orders', {
    id: 'ord_decline', buyerId: 'byr_decline', orderedModelCode: 'YI', units: [{ id: 'u_decline' }],
  });
  await api('POST', '/api/deliveries', {
    id: 'rcp_decline', orderId: 'ord_decline', unitId: 'u_decline', modelCode: 'YI',
  });
  await expectStatus(api('POST', '/api/reviews', {
    id: 'rvw_decline', buyerId: 'byr_decline', modelCode: 'YI', receiptId: 'rcp_decline',
    body: 'UNIQUE_DECLINE_BODY 体验可以看，但我不回答商家追问。',
  }, { 'x-buyer-id': 'byr_decline' }), [201], '发布拒绝询问场景的原测');
  await expectStatus(api('POST', '/api/reviews/rvw_decline/questions', { id: 'q_decline', body: '可以追问吗？' }), [201], '发出询问');
  const declined = await api('POST', '/api/questions/q_decline/decline', {
    buyerId: 'byr_decline', reason: '买家拒收后续询问',
  }, { 'x-buyer-id': 'byr_decline' });
  assert(declined.status === 200 && declined.json.answeredReceipt === null, '拒收后不能有已答复回条', declined.json);
  const answerAfterDecline = await api('POST', '/api/questions/q_decline/answer', {
    buyerId: 'byr_decline', body: '不该收到',
  }, { 'x-buyer-id': 'byr_decline' });
  assert(answerAfterDecline.status === 409, '拒收后作答必须冲突');

  const merchantQuestions = await api('GET', '/api/merchant/questions');
  const qd = merchantQuestions.json.questions.find((q) => q.id === 'q_decline');
  assert(qd.status === 'DECLINED' && qd.answerReceipt === null, '商家只能看到拒收状态，不能看到已答复回条', qd);
  const declinePage = await api('GET', '/api/products/YI/page');
  assert(declinePage.json.reviews.some((r) => r.id === 'rvw_decline'), '产品页应保留原体验');
  assert(!declinePage.json.followUpIndex.some((f) => f.questionId === 'q_decline'), '拒收询问不应产生读者后续');
  const declineHtml = await api('GET', '/p/YI');
  assert(declineHtml.text.includes('UNIQUE_DECLINE_BODY'), 'HTML 应保留原体验');

  console.log('4) 同单两件、分批到货、只换走一件；另买同款不产生第二份意见');
  await api('POST', '/api/buyers', { id: 'byr_two', displayName: '两件分批买家' });
  await api('POST', '/api/orders', {
    id: 'ord_two',
    buyerId: 'byr_two',
    orderedModelCode: 'JIA',
    splitShipment: true,
    units: [
      { id: 'u_two_1', orderedModelCode: 'JIA' },
      { id: 'u_two_2', orderedModelCode: 'JIA' },
    ],
  });
  await api('POST', '/api/deliveries', {
    id: 'rcp_two_1', orderId: 'ord_two', unitId: 'u_two_1', modelCode: 'JIA',
    voucherNo: 'VCH-TWO-1',
  });
  await api('POST', '/api/deliveries', {
    id: 'rcp_two_2', orderId: 'ord_two', unitId: 'u_two_2', modelCode: 'YI',
    voucherNo: 'VCH-TWO-2-YI-SUBSTITUTE',
    proof: { confirmedSubstitute: true, buyerConfirmedAt: new Date().toISOString() },
  });
  await expectStatus(api('POST', '/api/reviews', {
    id: 'rvw_jia_first', buyerId: 'byr_two', modelCode: 'JIA', receiptId: 'rcp_two_1',
    title: '第一件甲款首测', body: 'UNIQUE_JIA_BODY 第一件甲款一直留在我手里。',
  }, { 'x-buyer-id': 'byr_two' }), [201], '甲款首测');
  await expectStatus(api('POST', '/api/reviews', {
    id: 'rvw_yi_second', buyerId: 'byr_two', modelCode: 'YI', receiptId: 'rcp_two_2',
    title: '拆出的乙款', body: 'UNIQUE_YI_TWO_BODY 第二件实际是确认替代品乙。',
  }, { 'x-buyer-id': 'byr_two' }), [201], '乙款实测');

  await api('POST', '/api/returns', {
    id: 'rcp_two_return',
    orderId: 'ord_two',
    unitId: 'u_two_2',
    modelCode: 'YI',
    exchangeGroupId: 'exg_two',
    voucherNo: 'VCH-TWO-RETURN',
    refundAmountCents: 1,
    refundNote: '退款/补偿金额不决定试用过什么',
  });
  await api('POST', '/api/exchange-deliveries', {
    id: 'rcp_two_exchange',
    orderId: 'ord_two',
    unitId: 'u_two_3',
    newUnitId: 'u_two_3',
    replacesUnitId: 'u_two_2',
    modelCode: 'JIA',
    exchangeGroupId: 'exg_two',
    voucherNo: 'VCH-TWO-EXCHANGE',
  });
  await expectStatus(api('POST', '/api/reviews/rvw_jia_first/addenda', {
    id: 'add_jia_exchange',
    buyerId: 'byr_two',
    receiptId: 'rcp_two_exchange',
    kind: 'EXCHANGE',
    title: '只换走第二件后收到的甲款补交件',
    body: 'UNIQUE_JIA_ADDENDUM 新补交件是另一个物件，按时间挂追记。',
  }, { 'x-buyer-id': 'byr_two' }), [201], '甲款追记');
  await expectStatus(api('POST', '/api/reviews/rvw_jia_first/addenda', {
    id: 'add_jia_side',
    buyerId: 'byr_two',
    receiptId: 'rcp_two_1',
    kind: 'SIDE_SWAP',
    side: 'left/right',
    title: '左右耳调换后的补充',
    body: 'UNIQUE_SIDE_ADD 调换左右件后，触控位置需要重新适应。',
  }, { 'x-buyer-id': 'byr_two' }), [201], '左右件追记');

  const order = await api('GET', '/api/orders/ord_two');
  const u1 = order.json.order.units.find((u) => u.id === 'u_two_1');
  const u2 = order.json.order.units.find((u) => u.id === 'u_two_2');
  const u3 = order.json.order.units.find((u) => u.id === 'u_two_3');
  assert(u1.status === 'HELD', '第一件不应被换走', order.json.order.units);
  assert(u2.status === 'RETURNED', '第二件乙应已凭退件凭证收回');
  assert(u3.status === 'HELD', '换件补交的新物件应在买家手中');

  const secondOrder = await api('POST', '/api/orders', {
    id: 'ord_two_more', buyerId: 'byr_two', orderedModelCode: 'JIA', units: [{ id: 'u_more' }],
  });
  assert(secondOrder.status === 201, '可再下一单');
  await api('POST', '/api/deliveries', { orderId: 'ord_two_more', unitId: 'u_more', modelCode: 'JIA' });
  const moreOrderDetail = await api('GET', '/api/orders/ord_two_more');
  const moreReceiptId = moreOrderDetail.json.receipts[0].id;
  const duplicateReview = await api('POST', '/api/reviews', {
    buyerId: 'byr_two', modelCode: 'JIA', receiptId: moreReceiptId,
    body: 'UNIQUE_DUPLICATE_SHOULD_NOT_PUBLISH',
  }, { 'x-buyer-id': 'byr_two' });
  assert(duplicateReview.status === 409 && duplicateReview.json.error.code === 'FIRST_REVIEW_ALREADY_EXISTS', '另买一件同款不能产生第二份首测', duplicateReview.json);

  const jiaPage = await api('GET', '/api/products/JIA/page');
  const jiaReview = jiaPage.json.reviews.find((r) => r.id === 'rvw_jia_first');
  assert(jiaReview?.addenda?.length === 2, '甲款首测下应有两条有时间的追记', jiaReview);
  assert(jiaReview.addenda.some((a) => a.body.includes('UNIQUE_JIA_ADDENDUM')), '应包含换件追记');
  assert(jiaReview.addenda.some((a) => a.kind === 'SIDE_SWAP'), '应包含左右件调换追记');
  const yiTwoPage = await api('GET', '/api/products/YI/page');
  const yiTwoReview = yiTwoPage.json.reviews.find((r) => r.id === 'rvw_yi_second');
  assert(yiTwoReview.verification.interval.end !== null, '乙件被只换走后，乙款评价应展示持有区间结束/退件凭证');
  assert(yiTwoReview.verification.interval.returnReceiptId === 'rcp_two_return', '应核对到指定退件凭证');
  const jiaHtml = await api('GET', '/p/JIA');
  assert(jiaHtml.text.includes('UNIQUE_JIA_BODY') && jiaHtml.text.includes('UNIQUE_JIA_ADDENDUM') && jiaHtml.text.includes('UNIQUE_SIDE_ADD'), '甲款 HTML 应含原测与追记');

  console.log('5) 重启服务：款号公开页仍能打开可核对原测与后续');
  await stopServer();
  startServer();
  await waitHealthy();
  const restartYi = await api('GET', '/api/products/YI/page');
  const okReview = restartYi.json.reviews.find((r) => r.id === 'rvw_ok');
  assert(okReview?.body.includes('UNIQUE_OK_BODY'), '重启后乙款原测仍在');
  assert(okReview.followUp?.answer?.body.includes('UNIQUE_OK_ANSWER'), '重启后后续答复仍在');
  assert(restartYi.json.followUpIndex.some((f) => f.url === '/p/YI#followup-q_ok'), '重启后后续锚点仍可从款号页找到');
  const restartJia = await api('GET', '/api/products/JIA/page');
  const restartJiaReview = restartJia.json.reviews.find((r) => r.id === 'rvw_jia_first');
  assert(restartJiaReview?.addenda?.some((a) => a.id === 'add_jia_exchange'), '重启后甲款换件追记仍在');
  const receiptAfterRestart = await api('GET', '/api/receipts/rcp_ok');
  assert(receiptAfterRestart.status === 200 && receiptAfterRestart.json.voucherNo === 'VCH-OK-YI', '重启后凭证可核对');
  const htmlAfterRestart = await api('GET', '/p/YI');
  assert(htmlAfterRestart.text.includes('id="followup-q_ok"') && htmlAfterRestart.text.includes('UNIQUE_OK_ANSWER'), '重启后 HTML 读者路径仍可用');

  console.log('\nALL E2E CHECKS PASSED');
  console.log(`Data file retained at: ${DATA_FILE}`);
}

startServer();
try {
  await waitHealthy();
  await runScenarios();
} catch (err) {
  console.error('\nE2E FAILED:', err.message);
  console.error('\n--- server logs ---\n' + logs);
  await stopServer();
  process.exitCode = 1;
} finally {
  await stopServer();
}
