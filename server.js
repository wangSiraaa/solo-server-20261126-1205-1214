import http from 'node:http';
import { randomUUID } from 'node:crypto';
import {
  loadStore,
  saveStore,
  nextId,
  appendEvent,
  dataFilePath,
  emptyStore,
} from './lib/store.js';
import {
  RECEIPT_TYPES,
  RECEIPT_TYPE_LABELS,
  isoTime,
  effectiveReceipt,
  evaluateExperienceReceipt,
  findActiveFirstReview,
  reviewPublicState,
  reviewView,
  productPageData,
  applyCorrectionSideEffects,
} from './lib/domain.js';
import { renderIndex, renderProductPage } from './lib/html.js';

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || '0.0.0.0';
const store = loadStore();

function seedIfNeeded() {
  if (!store.merchants.merchant_default) {
    store.merchants.merchant_default = {
      id: 'merchant_default',
      name: '配件商城官方商家账号',
      token: process.env.MERCHANT_TOKEN || 'merchant-dev-token',
      createdAt: new Date().toISOString(),
    };
  }
  const products = [
    ['JIA', '甲款 · 开放式耳机'],
    ['YI', '乙款 · 入耳式耳机（确认替代品）'],
    ['BING', '丙款 · 换件补交示例款'],
  ];
  for (const [modelCode, name] of products) {
    if (!store.products[modelCode]) {
      store.products[modelCode] = {
        modelCode,
        name,
        createdAt: new Date().toISOString(),
      };
    }
  }
}
seedIfNeeded();
saveStore(store);

class HttpError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'access-control-allow-origin': '*',
    'access-control-allow-headers': 'content-type, authorization, x-buyer-id, x-buyer-token, x-merchant-token, x-merchant-id',
    'access-control-allow-methods': 'GET,POST,PATCH,OPTIONS',
  });
  res.end(body);
}

function sendHtml(res, status, html) {
  res.writeHead(status, {
    'content-type': 'text/html; charset=utf-8',
    'content-length': Buffer.byteLength(html),
  });
  res.end(html);
}

async function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 2_000_000) {
        reject(new HttpError(413, 'BODY_TOO_LARGE', '请求体过大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!raw.trim()) return resolve({});
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) resolve(parsed);
        else reject(new HttpError(400, 'INVALID_JSON_OBJECT', 'JSON 请求体必须是对象'));
      } catch (err) {
        reject(new HttpError(400, 'INVALID_JSON', '无法解析 JSON 请求体'));
      }
    });
    req.on('error', reject);
  });
}

function bearerToken(req) {
  const header = req.headers.authorization || '';
  return header.startsWith('Bearer ') ? header.slice(7).trim() : null;
}

function authBuyer(req, body = {}) {
  const token = bearerToken(req) || req.headers['x-buyer-token'] || body.buyerToken || null;
  const headerBuyerId = req.headers['x-buyer-id'] ? String(req.headers['x-buyer-id']) : null;
  const bodyBuyerId = body.buyerId || body.authorId || body.userId || body.accountId || null;
  let buyer = null;

  if (token) {
    buyer = Object.values(store.buyers).find((b) => b.token === token) || null;
    if (!buyer) throw new HttpError(401, 'INVALID_BUYER_TOKEN', '买家账号凭证无效');
    if (headerBuyerId && headerBuyerId !== buyer.id) {
      throw new HttpError(403, 'BUYER_ID_TOKEN_MISMATCH', '账号与凭证不匹配');
    }
  } else if (headerBuyerId) {
    buyer = store.buyers[headerBuyerId];
    if (!buyer) throw new HttpError(404, 'BUYER_NOT_FOUND', '指定的买家账号不存在');
  } else if (bodyBuyerId) {
    buyer = store.buyers[bodyBuyerId];
    if (!buyer) throw new HttpError(404, 'BUYER_NOT_FOUND', '指定的买家账号不存在');
  }

  if (!buyer) throw new HttpError(401, 'BUYER_AUTH_REQUIRED', '请使用买家正常账号登录后操作；创建买家会返回 token，也可在本机用 x-buyer-id。');
  return buyer;
}

function optionalBuyer(req, body = {}) {
  try {
    const token = bearerToken(req) || req.headers['x-buyer-token'];
    const headerBuyerId = req.headers['x-buyer-id'] ? String(req.headers['x-buyer-id']) : null;
    if (!token && !headerBuyerId && !body.buyerId && !body.authorId && !body.userId) return null;
    return authBuyer(req, body);
  } catch {
    return null;
  }
}

function authMerchant(req) {
  const token = bearerToken(req) || req.headers['x-merchant-token'] || null;
  const merchantId = req.headers['x-merchant-id'] ? String(req.headers['x-merchant-id']) : 'merchant_default';
  if (token && token !== store.merchants.merchant_default?.token) {
    const merchant = Object.values(store.merchants).find((m) => m.token === token);
    if (!merchant) throw new HttpError(401, 'INVALID_MERCHANT_TOKEN', '商家凭证无效');
    return merchant;
  }
  const merchant = store.merchants[merchantId] || store.merchants.merchant_default;
  if (!merchant) throw new HttpError(404, 'MERCHANT_NOT_FOUND', '商家账号不存在');
  return merchant;
}

function requireBuyerOwnsQuestion(buyer, question) {
  if (question.buyerId !== buyer.id) {
    throw new HttpError(403, 'QUESTION_ROUTED_TO_OTHER_BUYER', '这条询问投给了另一位试用人，不能由当前账号回复');
  }
}

function getOrderOr404(orderId) {
  const order = store.orders[orderId];
  if (!order) throw new HttpError(404, 'ORDER_NOT_FOUND', '订单不存在');
  return order;
}

function getReviewOr404(reviewId) {
  const review = store.reviews[reviewId];
  if (!review) throw new HttpError(404, 'REVIEW_NOT_FOUND', '评价不存在');
  return review;
}

function getQuestionOr404(questionId) {
  const question = store.questions[questionId];
  if (!question) throw new HttpError(404, 'QUESTION_NOT_FOUND', '商家询问不存在');
  return question;
}

function findOrderUnit(order, unitId, { required = true } = {}) {
  if (!unitId) {
    if (order.units.length === 1) return order.units[0];
    if (required) throw new HttpError(400, 'UNIT_ID_REQUIRED', '同单有多件商品/物件，必须用 unitId 指定具体哪一件');
    return null;
  }
  const unit = order.units.find((u) => u.id === unitId);
  if (!unit) throw new HttpError(404, 'ORDER_UNIT_NOT_FOUND', `订单 ${order.id} 中没有物件 ${unitId}`);
  return unit;
}

function normalizeReceiptType(type) {
  const aliases = {
    DELIVERY: RECEIPT_TYPES.ARRIVAL,
    DELIVERED: RECEIPT_TYPES.ARRIVAL,
    RECEIVED: RECEIPT_TYPES.ARRIVAL,
    SIGNED: RECEIPT_TYPES.ARRIVAL,
    ACCEPTED: RECEIPT_TYPES.ARRIVAL,
    DELIVERY_ACCEPTED: RECEIPT_TYPES.ARRIVAL,
    ARRIVAL_ACCEPTED: RECEIPT_TYPES.ARRIVAL,
    到件: RECEIPT_TYPES.ARRIVAL,
    签收: RECEIPT_TYPES.ARRIVAL,
    REJECTED: RECEIPT_TYPES.REJECTION,
    REFUSAL: RECEIPT_TYPES.REJECTION,
    拒收: RECEIPT_TYPES.REJECTION,
    EXCHANGE_PICKUP: RECEIPT_TYPES.RETURN,
    RETURNED: RECEIPT_TYPES.RETURN,
    退件: RECEIPT_TYPES.RETURN,
    换货收回: RECEIPT_TYPES.RETURN,
    REPLACEMENT_DELIVERY: RECEIPT_TYPES.EXCHANGE_DELIVERY,
    RESHIPMENT: RECEIPT_TYPES.EXCHANGE_DELIVERY,
    换件补交: RECEIPT_TYPES.EXCHANGE_DELIVERY,
  };
  const normalized = aliases[type] || type;
  if (!Object.values(RECEIPT_TYPES).includes(normalized)) {
    throw new HttpError(400, 'INVALID_RECEIPT_TYPE', `凭证类型必须是 ${Object.values(RECEIPT_TYPES).join(', ')} 之一`);
  }
  return normalized;
}

function createReceipt(input, { typeOverride } = {}) {
  const type = normalizeReceiptType(typeOverride || input.type);
  const orderId = input.orderId;
  if (!orderId) throw new HttpError(400, 'ORDER_ID_REQUIRED', '凭证必须关联订单');
  const order = getOrderOr404(orderId);
  if (input.buyerId && input.buyerId !== order.buyerId) {
    throw new HttpError(403, 'ORDER_BUYER_MISMATCH', '凭证买家与订单买家不一致');
  }

  let unitId = input.unitId || input.orderItemId || input.itemId || null;
  let unit = unitId ? order.units.find((u) => u.id === unitId) : findOrderUnit(order, unitId);

  if (type === RECEIPT_TYPES.EXCHANGE_DELIVERY) {
    const newUnitId = input.newUnitId
      || input.replacementUnitId
      || unitId
      || `unt_${randomUUID().slice(0, 10)}`;
    if (!order.units.find((u) => u.id === newUnitId)) {
      const replacesUnitId = input.replacesUnitId || input.replacementForUnitId || null;
      order.units.push({
        id: newUnitId,
        orderedModelCode: input.orderedModelCode || order.orderedModelCode || null,
        actualModelCode: input.modelCode || input.sku || null,
        replacesUnitId,
        status: 'HELD',
        createdAt: new Date().toISOString(),
      });
    }
    unitId = newUnitId;
    unit = order.units.find((u) => u.id === unitId);
  } else if (!unit) {
    unit = findOrderUnit(order, unitId);
    unitId = unit.id;
  }

  const modelCode = input.modelCode
    || input.sku
    || input.productCode
    || (type === RECEIPT_TYPES.RETURN ? unit.actualModelCode : null)
    || unit.actualModelCode
    || unit.orderedModelCode
    || order.orderedModelCode;
  if (!modelCode) throw new HttpError(400, 'MODEL_CODE_REQUIRED', '凭证需要实际款号 modelCode');

  const id = input.id || nextId(store, 'receipt', 'rcp');
  if (store.receipts[id]) throw new HttpError(409, 'RECEIPT_ID_EXISTS', '凭证 ID 已存在');
  const occurredAt = isoTime(input.occurredAt || input.at || input.receivedAt);
  const voucherNo = input.voucherNo || input.proofNo || `vch_${id}`;

  const receipt = {
    id,
    type,
    typeLabel: RECEIPT_TYPE_LABELS[type],
    orderId,
    buyerId: order.buyerId,
    unitId,
    replacesUnitId: input.replacesUnitId || input.replacementForUnitId || unit.replacesUnitId || null,
    exchangeGroupId: input.exchangeGroupId || null,
    modelCode: String(modelCode),
    voucherNo,
    carrier: input.carrier || input.proof?.carrier || null,
    signer: input.signer || input.proof?.signer || null,
    proof: input.proof || {},
    refundAmountCents: input.refundAmountCents ?? input.refund?.amountCents ?? null,
    refundCurrency: input.refundCurrency || input.refund?.currency || 'CNY',
    refundNote: input.refundNote || input.refund?.note || null,
    note: input.note || null,
    status: 'EFFECTIVE',
    occurredAt,
    createdAt: new Date().toISOString(),
  };
  store.receipts[id] = receipt;

  if (type === RECEIPT_TYPES.ARRIVAL) {
    unit.actualModelCode = receipt.modelCode;
    unit.status = 'HELD';
    unit.heldSince = occurredAt;
    unit.currentReceiptId = id;
  } else if (type === RECEIPT_TYPES.REJECTION) {
    unit.status = 'REJECTED';
    unit.rejectedAt = occurredAt;
    unit.rejectionReceiptId = id;
  } else if (type === RECEIPT_TYPES.RETURN) {
    unit.status = 'RETURNED';
    unit.returnedAt = occurredAt;
    unit.returnReceiptId = id;
  } else if (type === RECEIPT_TYPES.EXCHANGE_DELIVERY) {
    unit.actualModelCode = receipt.modelCode;
    unit.status = 'HELD';
    unit.heldSince = occurredAt;
    unit.currentReceiptId = id;
    if (receipt.replacesUnitId) {
      const oldUnit = order.units.find((u) => u.id === receipt.replacesUnitId);
      if (oldUnit && oldUnit.status !== 'RETURNED') oldUnit.status = 'EXCHANGED_AWAITING_RETURN_RECORD';
    }
  }

  appendEvent(store, 'receipt.recorded', {
    receiptId: receipt.id,
    type,
    orderId,
    unitId,
    modelCode: receipt.modelCode,
  });
  return receipt;
}

function receiptView(receiptOrId) {
  const receipt = typeof receiptOrId === 'string' ? store.receipts[receiptOrId] : receiptOrId;
  if (!receipt) return null;
  const effective = effectiveReceipt(store, receipt.id);
  return {
    ...receipt,
    effectiveReceipt: effective && effective.id !== receipt.id ? effective : undefined,
    publicVerification: evaluateExperienceReceipt(store, {
      buyerId: receipt.buyerId,
      modelCode: receipt.modelCode,
      receiptId: receipt.id,
      at: new Date().toISOString(),
    }),
  };
}

function questionView(question, { full = false } = {}) {
  const buyer = store.buyers[question.buyerId];
  const review = store.reviews[question.reviewId];
  const view = {
    id: question.id,
    reviewId: question.reviewId,
    modelCode: question.modelCode,
    status: question.status,
    body: question.body,
    createdAt: question.createdAt,
    deliveredAt: question.deliveredAt,
    openedAt: question.openedAt || null,
    answeredAt: question.answeredAt || null,
    declinedAt: question.declinedAt || null,
    voidedAt: question.voidedAt || null,
    review: review ? {
      id: review.id,
      modelCode: review.modelCode,
      anchor: `review-${review.id}`,
      productPath: `/p/${encodeURIComponent(review.modelCode)}`,
    } : null,
    productPage: {
      url: `/p/${encodeURIComponent(question.modelCode)}`,
      followUpAnchor: `followup-${question.id}`,
      readerPath: `/p/${encodeURIComponent(question.modelCode)}#followup-${question.id}`,
    },
    answerReceipt: question.status === 'ANSWERED' ? {
      answerId: question.answer?.id,
      receivedAt: question.answeredAt,
      routedToProductPage: question.publiclyVisible !== false,
      anchor: `followup-${question.id}`,
    } : null,
  };

  if (question.status === 'ANSWERED' && question.answer && question.publiclyVisible !== false) {
    view.answer = {
      id: question.answer.id,
      body: question.answer.body,
      createdAt: question.answer.createdAt,
      anchor: `answer-${question.answer.id}`,
    };
  }

  if (full) {
    view.buyerId = question.buyerId;
    view.merchantId = question.merchantId;
    view.delivery = question.delivery;
    view.events = question.events;
    view.declineReason = question.declineReason || null;
    view.voidReason = question.voidReason || null;
    view.voidExplanation = question.voidExplanation || null;
    view.buyerAccount = {
      buyerId: question.buyerId,
      displayName: buyer?.displayName || null,
      inboxPath: '/api/account/questions',
    };
    if (question.answer && question.publiclyVisible === false) view.answer = question.answer;
  }
  return view;
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'OPTIONS') {
      res.writeHead(204, {
        'access-control-allow-origin': '*',
        'access-control-allow-headers': 'content-type, authorization, x-buyer-id, x-buyer-token, x-merchant-token, x-merchant-id',
        'access-control-allow-methods': 'GET,POST,PATCH,OPTIONS',
      });
      return res.end();
    }

    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const pathname = url.pathname.replace(/\/+$/, '') || '/';
    const segments = pathname.split('/').filter(Boolean);
    const body = ['POST', 'PUT', 'PATCH'].includes(req.method) ? await readBody(req) : {};

    if (req.method === 'GET' && pathname === '/health') {
      return sendJson(res, 200, {
        ok: true,
        service: 'verified-fit-review-service',
        time: new Date().toISOString(),
        dataFile: dataFilePath(),
        persisted: true,
      });
    }

    if (req.method === 'GET' && pathname === '/api') {
      return sendJson(res, 200, {
        service: 'verified-fit-review-service',
        principles: [
          '评价挂在凭证证明的实际款号与具体物件上，不按订单下的原款静默推断。',
          '每个买家在每个款号下只有一份有效首测；另买同款不会产生第二个人的意见。',
          '拒收凭证不能证明收用；收货登记更正为拒收后，旧刊冻结，未回询问作废，不转款、不转人。',
          '商家的一次补充询问按 reviewId 路由到该评价作者的买家账号。',
          '退款金额不参与实测资格判断。',
        ],
        endpoints: {
          buyers: 'POST /api/buyers',
          accountQuestions: 'GET /api/account/questions (Bearer buyer token or x-buyer-id)',
          products: 'POST /api/products; GET /api/products; GET /api/products/:modelCode/page',
          productHtml: 'GET /p/:modelCode',
          orders: 'POST /api/orders; GET /api/orders/:id',
          receipts: 'POST /api/receipts; GET /api/receipts/:id; POST /api/receipts/:id/correct',
          semanticReceipts: 'POST /api/deliveries | /api/rejections | /api/returns | /api/exchange-deliveries',
          reviews: 'POST /api/reviews; GET /api/reviews/:id; POST /api/reviews/:id/addenda',
          merchantQuestion: 'POST /api/reviews/:reviewId/questions',
          buyerReply: 'POST /api/questions/:id/answer | /open | /decline',
          merchantQuestions: 'GET /api/merchant/questions',
          restartSafe: '所有写入原子保存到 DATA_FILE 或 ./data/store.json',
        },
      });
    }

    if (req.method === 'GET' && pathname === '/') {
      return sendHtml(res, 200, renderIndex(store));
    }

    // Product HTML pages: /p/JIA, /product/JIA, /products/JIA (but not /api/products).
    const htmlProductMatch = pathname.match(/^\/(?:p|product|products)\/([^/]+)$/);
    if (req.method === 'GET' && htmlProductMatch && segments[0] !== 'api') {
      const modelCode = decodeURIComponent(htmlProductMatch[1]);
      if (!store.products[modelCode]) throw new HttpError(404, 'PRODUCT_NOT_FOUND', `款号 ${modelCode} 不存在`);
      return sendHtml(res, 200, renderProductPage(productPageData(store, modelCode)));
    }

    if (req.method === 'POST' && pathname === '/api/admin/reset') {
      if (body.confirm !== true && url.searchParams.get('confirm') !== 'true') {
        throw new HttpError(400, 'CONFIRM_REQUIRED', '重置需要 {"confirm":true}');
      }
      const fresh = emptyStore();
      for (const key of Object.keys(store)) delete store[key];
      Object.assign(store, fresh);
      seedIfNeeded();
      appendEvent(store, 'admin.reset', {});
      saveStore(store);
      return sendJson(res, 200, { ok: true, resetAt: new Date().toISOString() });
    }

    // Buyers
    if (req.method === 'POST' && pathname === '/api/buyers') {
      const displayName = body.displayName || body.name || body.nickname;
      if (!displayName) throw new HttpError(400, 'DISPLAY_NAME_REQUIRED', '需要买家名称');
      const id = body.id || nextId(store, 'buyer', 'byr');
      if (store.buyers[id]) throw new HttpError(409, 'BUYER_ID_EXISTS', '买家 ID 已存在');
      const buyer = {
        id,
        displayName,
        token: body.token || `buyer_${randomUUID()}`,
        answerConsent: Boolean(body.answerConsent ?? body.agreesToAnswer ?? body.consentToQuestions ?? false),
        note: body.note || null,
        createdAt: new Date().toISOString(),
      };
      store.buyers[id] = buyer;
      appendEvent(store, 'buyer.created', { buyerId: id, answerConsent: buyer.answerConsent });
      saveStore(store);
      return sendJson(res, 201, { ok: true, buyer });
    }

    if (req.method === 'GET' && pathname === '/api/buyers') {
      return sendJson(res, 200, { buyers: Object.values(store.buyers).map((b) => ({ ...b, token: undefined })) });
    }

    if (req.method === 'GET' && pathname === '/api/buyers/me') {
      const buyer = authBuyer(req);
      return sendJson(res, 200, { buyer: { ...buyer, token: undefined } });
    }

    if (req.method === 'GET' && pathname === '/api/account/questions') {
      const buyer = authBuyer(req);
      const questions = Object.values(store.questions)
        .filter((q) => q.buyerId === buyer.id)
        .sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt))
        .map((q) => questionView(q, { full: true }));
      return sendJson(res, 200, {
        buyerId: buyer.id,
        account: buyer.displayName,
        inbox: questions,
        count: questions.length,
      });
    }

    // Products
    if (req.method === 'POST' && pathname === '/api/products') {
      const modelCode = body.modelCode || body.sku || body.code;
      if (!modelCode) throw new HttpError(400, 'MODEL_CODE_REQUIRED', '需要款号 modelCode');
      const code = String(modelCode);
      if (store.products[code]) throw new HttpError(409, 'PRODUCT_EXISTS', '款号已存在');
      store.products[code] = {
        modelCode: code,
        name: body.name || code,
        description: body.description || null,
        createdAt: new Date().toISOString(),
      };
      appendEvent(store, 'product.created', { modelCode: code });
      saveStore(store);
      return sendJson(res, 201, { ok: true, product: store.products[code] });
    }

    if (req.method === 'GET' && pathname === '/api/products') {
      const products = Object.values(store.products).map((p) => ({
        ...p,
        page: `/p/${encodeURIComponent(p.modelCode)}`,
        jsonPage: `/api/products/${encodeURIComponent(p.modelCode)}/page`,
      }));
      return sendJson(res, 200, { products });
    }

    const productPageMatch = pathname.match(/^\/api\/products\/([^/]+)(?:\/page)?$/);
    if (req.method === 'GET' && productPageMatch) {
      const modelCode = decodeURIComponent(productPageMatch[1]);
      if (!store.products[modelCode]) throw new HttpError(404, 'PRODUCT_NOT_FOUND', `款号 ${modelCode} 不存在`);
      return sendJson(res, 200, productPageData(store, modelCode));
    }

    // Orders
    if (req.method === 'POST' && pathname === '/api/orders') {
      const buyerId = body.buyerId || body.customerId || body.userId || body.authorId;
      if (!buyerId || !store.buyers[buyerId]) throw new HttpError(404, 'BUYER_NOT_FOUND', '下单需要已存在的 buyerId');
      const orderedModelCode = body.orderedModelCode || body.modelCode || body.sku || null;
      const id = body.id || nextId(store, 'order', 'ord');
      if (store.orders[id]) throw new HttpError(409, 'ORDER_ID_EXISTS', '订单 ID 已存在');

      let units = [];
      if (Array.isArray(body.units)) {
        units = body.units.map((u, i) => {
          if (typeof u === 'string') return { id: u, orderedModelCode, status: 'ORDERED' };
          const unitId = u.id || u.unitId || u.clientUnitId || `unt_${id}_${i + 1}`;
          return {
            id: unitId,
            orderedModelCode: u.orderedModelCode || u.modelCode || orderedModelCode,
            serialNo: u.serialNo || null,
            status: 'ORDERED',
            createdAt: new Date().toISOString(),
          };
        });
      } else if (Array.isArray(body.items)) {
        let n = 0;
        for (const item of body.items) {
          const qty = Number(item.quantity || item.qty || 1);
          for (let i = 0; i < qty; i += 1) {
            n += 1;
            units.push({
              id: `${id}_u${n}`,
              orderItemId: item.id || item.orderItemId || null,
              orderedModelCode: item.modelCode || item.sku || orderedModelCode,
              status: 'ORDERED',
              createdAt: new Date().toISOString(),
            });
          }
        }
      } else {
        const qty = Number(body.quantity || body.qty || 1);
        for (let i = 0; i < qty; i += 1) {
          units.push({
            id: `${id}_u${i + 1}`,
            orderedModelCode,
            status: 'ORDERED',
            createdAt: new Date().toISOString(),
          });
        }
      }

      const order = {
        id,
        buyerId,
        orderedModelCode,
        status: 'PAID',
        paymentId: body.paymentId || body.payment?.id || `pay_${id}`,
        paidAmountCents: body.paidAmountCents ?? body.payment?.amountCents ?? null,
        splitShipment: Boolean(body.splitShipment ?? (units.length > 1)),
        note: body.note || null,
        units,
        createdAt: isoTime(body.createdAt || body.placedAt),
      };
      store.orders[id] = order;
      appendEvent(store, 'order.created', { orderId: id, buyerId, unitCount: units.length });
      saveStore(store);
      return sendJson(res, 201, { ok: true, order });
    }

    if (req.method === 'GET' && pathname === '/api/orders') {
      return sendJson(res, 200, { orders: Object.values(store.orders) });
    }

    const orderMatch = pathname.match(/^\/api\/orders\/([^/]+)$/);
    if (req.method === 'GET' && orderMatch) {
      const order = getOrderOr404(decodeURIComponent(orderMatch[1]));
      const receipts = Object.values(store.receipts)
        .filter((r) => r.orderId === order.id)
        .sort((a, b) => new Date(a.occurredAt) - new Date(b.occurredAt));
      return sendJson(res, 200, { ok: true, order, receipts });
    }

    const orderReceiptsMatch = pathname.match(/^\/api\/orders\/([^/]+)\/receipts$/);
    if (req.method === 'POST' && orderReceiptsMatch) {
      const orderId = decodeURIComponent(orderReceiptsMatch[1]);
      const receipt = createReceipt({ ...body, orderId });
      saveStore(store);
      return sendJson(res, 201, { ok: true, receipt: receiptView(receipt) });
    }

    // Semantic receipt aliases
    const semanticReceiptRoutes = {
      '/api/deliveries': RECEIPT_TYPES.ARRIVAL,
      '/api/arrivals': RECEIPT_TYPES.ARRIVAL,
      '/api/rejections': RECEIPT_TYPES.REJECTION,
      '/api/refusals': RECEIPT_TYPES.REJECTION,
      '/api/returns': RECEIPT_TYPES.RETURN,
      '/api/exchange-pickups': RECEIPT_TYPES.RETURN,
      '/api/exchange-deliveries': RECEIPT_TYPES.EXCHANGE_DELIVERY,
      '/api/replacements': RECEIPT_TYPES.EXCHANGE_DELIVERY,
    };
    if (req.method === 'POST' && semanticReceiptRoutes[pathname]) {
      const receipt = createReceipt(body, { typeOverride: semanticReceiptRoutes[pathname] });
      saveStore(store);
      return sendJson(res, 201, { ok: true, receipt: receiptView(receipt) });
    }

    if (req.method === 'POST' && pathname === '/api/receipts') {
      const receipt = createReceipt(body);
      saveStore(store);
      return sendJson(res, 201, { ok: true, receipt: receiptView(receipt) });
    }

    const receiptCorrectMatch = pathname.match(/^\/api\/receipts\/([^/]+)(?:\/(correct|correction|reject))?$/);
    const isCorrectionRequest = receiptCorrectMatch
      && (receiptCorrectMatch[2] || req.method === 'PATCH' || req.method === 'PUT');
    if (isCorrectionRequest && ['POST', 'PATCH', 'PUT'].includes(req.method)) {
      const id = decodeURIComponent(receiptCorrectMatch[1]);
      const correctionAction = receiptCorrectMatch[2];
      const original = store.receipts[id];
      if (!original) throw new HttpError(404, 'RECEIPT_NOT_FOUND', '收货凭证不存在');
      const correctedType = normalizeReceiptType(
        (correctionAction === 'reject' ? RECEIPT_TYPES.REJECTION : null)
        || body.correctedType
        || body.correctTo
        || body.type
        || RECEIPT_TYPES.REJECTION,
      );
      const correctionId = body.correctionId || body.newReceiptId || nextId(store, 'receipt', 'rcp_corr');
      if (store.receipts[correctionId]) throw new HttpError(409, 'CORRECTION_ID_EXISTS', '更正凭证 ID 已存在');
      const correction = {
        id: correctionId,
        type: correctedType,
        typeLabel: RECEIPT_TYPE_LABELS[correctedType],
        orderId: original.orderId,
        buyerId: original.buyerId,
        unitId: body.unitId || original.unitId,
        replacesUnitId: original.replacesUnitId || null,
        exchangeGroupId: original.exchangeGroupId || null,
        modelCode: body.modelCode || original.modelCode,
        voucherNo: body.voucherNo || `vch_corr_${correctionId}`,
        carrier: body.carrier || original.carrier,
        signer: body.signer || original.signer,
        proof: body.proof || { correction: true, originalVoucherNo: original.voucherNo },
        refundAmountCents: body.refundAmountCents ?? original.refundAmountCents,
        status: 'EFFECTIVE',
        correctsReceiptId: original.id,
        reason: body.reason || body.correctionReason || '收货登记更正',
        correctedBy: body.correctedBy || body.operator || 'operations',
        occurredAt: isoTime(body.occurredAt || body.at || original.occurredAt),
        createdAt: new Date().toISOString(),
      };
      store.receipts[correction.id] = correction;
      original.status = 'CORRECTED';
      original.correctedAt = correction.createdAt;
      original.correctedByReceiptId = correction.id;

      const effects = applyCorrectionSideEffects(store, original, correction);
      appendEvent(store, 'receipt.corrected', {
        originalReceiptId: original.id,
        correctionId: correction.id,
        correctedType,
        ...effects,
      });
      saveStore(store);
      return sendJson(res, 200, {
        ok: true,
        original,
        correction,
        effects,
        policy: '旧刊只冻结/标记，不修改 modelCode；未回询问作废且不转投。',
      });
    }

    const receiptMatch = pathname.match(/^\/api\/receipts\/([^/]+)$/);
    if (req.method === 'GET' && receiptMatch) {
      const id = decodeURIComponent(receiptMatch[1]);
      const view = receiptView(id);
      if (!view) throw new HttpError(404, 'RECEIPT_NOT_FOUND', '收货凭证不存在');
      return sendJson(res, 200, view);
    }

    // Reviews
    if (req.method === 'POST' && pathname === '/api/reviews') {
      const buyer = authBuyer(req, body);
      const modelCode = body.modelCode || body.sku || body.productCode;
      const receiptId = body.receiptId || body.proofReceiptId;
      if (!modelCode) throw new HttpError(400, 'MODEL_CODE_REQUIRED', '评价需要 modelCode');
      if (!store.products[modelCode]) throw new HttpError(404, 'PRODUCT_NOT_FOUND', `款号 ${modelCode} 不存在`);
      if (!receiptId) throw new HttpError(400, 'RECEIPT_ID_REQUIRED', '实测评价必须关联收货凭证 receiptId');

      const existing = findActiveFirstReview(store, buyer.id, modelCode);
      if (existing) {
        throw new HttpError(409, 'FIRST_REVIEW_ALREADY_EXISTS', '该作者在该款下已有一份有效首测；另买一件同款不会新增一个人的意见，可发有时间的追记。', {
          existingReviewId: existing.id,
          addendumEndpoint: `/api/reviews/${existing.id}/addenda`,
        });
      }

      const publishedAt = isoTime(body.publishedAt || new Date().toISOString());
      const experiencedAt = isoTime(body.experiencedAt || body.testedAt || body.usedAt || body.at || publishedAt);
      const verification = evaluateExperienceReceipt(store, {
        buyerId: buyer.id,
        modelCode: String(modelCode),
        receiptId,
        at: experiencedAt,
      });
      if (verification.status !== 'VERIFIED') {
        throw new HttpError(422, 'RECEIPT_PROOF_INVALID', verification.message, verification);
      }

      const rating = Math.round(Number(body.rating ?? body.stars ?? 5));
      if (!Number.isFinite(rating) || rating < 1 || rating > 5) {
        throw new HttpError(400, 'INVALID_RATING', 'rating 必须在 1 到 5 之间');
      }
      const content = body.body || body.content || body.text || body.review;
      if (!content || !String(content).trim()) {
        throw new HttpError(400, 'REVIEW_BODY_REQUIRED', '评价正文不能为空');
      }

      const id = body.id || nextId(store, 'review', 'rvw');
      if (store.reviews[id]) throw new HttpError(409, 'REVIEW_ID_EXISTS', '评价 ID 已存在');
      const review = {
        id,
        buyerId: buyer.id,
        modelCode: String(modelCode),
        orderId: verification.receipt.orderId,
        receiptId,
        rating,
        title: body.title || null,
        body: String(content),
        status: 'PUBLISHED',
        publishedAt,
        experiencedAt,
        answerConsent: Boolean(body.answerConsent ?? body.agreesToAnswer ?? buyer.answerConsent ?? false),
        addenda: [],
        createdAt: new Date().toISOString(),
      };
      store.reviews[id] = review;
      appendEvent(store, 'review.published', { reviewId: id, buyerId: buyer.id, modelCode, receiptId });
      saveStore(store);
      const view = reviewView(store, review, { force: true });
      return sendJson(res, 201, { ok: true, ...view, review: view });
    }

    if (req.method === 'GET' && pathname === '/api/reviews') {
      let reviews = Object.values(store.reviews);
      if (url.searchParams.get('modelCode')) reviews = reviews.filter((r) => r.modelCode === url.searchParams.get('modelCode'));
      if (url.searchParams.get('buyerId')) reviews = reviews.filter((r) => r.buyerId === url.searchParams.get('buyerId'));
      const includeWithheld = url.searchParams.get('includeWithheld') === 'true';
      return sendJson(res, 200, {
        reviews: reviews.map((r) => reviewView(store, r, { force: includeWithheld })),
      });
    }

    const addendumMatch = pathname.match(/^\/api\/reviews\/([^/]+)\/addenda$/);
    if (req.method === 'POST' && addendumMatch) {
      const review = getReviewOr404(decodeURIComponent(addendumMatch[1]));
      const buyer = authBuyer(req, body);
      if (review.buyerId !== buyer.id) throw new HttpError(403, 'NOT_REVIEW_AUTHOR', '只有首测作者可以追加追记');
      if (review.status !== 'PUBLISHED') throw new HttpError(409, 'REVIEW_NOT_ACTIVE', '旧刊依据已失效，不能在其下追加新追记');

      const content = body.body || body.content || body.text;
      if (!content || !String(content).trim()) throw new HttpError(400, 'ADDENDUM_BODY_REQUIRED', '追记正文不能为空');
      const receiptId = body.receiptId || review.receiptId;
      const createdAt = isoTime(body.createdAt || body.occurredAt || body.at || new Date().toISOString());
      const receipt = effectiveReceipt(store, receiptId);
      const subjectModelCode = body.subjectModelCode || body.modelCode || receipt?.modelCode || review.modelCode;
      const verification = evaluateExperienceReceipt(store, {
        buyerId: buyer.id,
        modelCode: subjectModelCode,
        receiptId,
        at: createdAt,
      });
      if (verification.status !== 'VERIFIED') {
        throw new HttpError(422, 'ADDENDUM_PROOF_INVALID', verification.message, verification);
      }

      const addendum = {
        id: body.id || nextId(store, 'addendum', 'add'),
        kind: body.kind || body.changeType || 'FOLLOW_UP',
        side: body.side || null,
        title: body.title || null,
        body: String(content),
        receiptId,
        subjectModelCode,
        sequence: review.addenda.length + 1,
        status: 'PUBLISHED',
        createdAt,
      };
      review.addenda.push(addendum);
      appendEvent(store, 'review.addendum', { reviewId: review.id, addendumId: addendum.id, receiptId });
      saveStore(store);
      return sendJson(res, 201, { ok: true, addendum, review: reviewView(store, review, { force: true }) });
    }

    const reviewQuestionMatch = pathname.match(/^\/api\/reviews\/([^/]+)\/(?:questions?|follow-ups?|followups?)$/);
    if (req.method === 'POST' && reviewQuestionMatch) {
      const merchant = authMerchant(req);
      const review = getReviewOr404(decodeURIComponent(reviewQuestionMatch[1]));
      const state = reviewPublicState(store, review);
      if (!state.active) {
        throw new HttpError(409, 'REVIEW_NOT_ELIGIBLE_FOR_QUESTION', '该体验的收货依据已失效，询问不能发出；也不会转投其他买家或款号。', state.verification);
      }
      const existing = Object.values(store.questions).find((q) => q.reviewId === review.id);
      if (existing) {
        throw new HttpError(409, 'QUESTION_ALREADY_SENT_FOR_REVIEW', '商家只能针对同一条具体体验发一次补充询问', {
          questionId: existing.id,
          status: existing.status,
        });
      }
      const questionBody = body.body || body.question || body.content || body.text;
      if (!questionBody || !String(questionBody).trim()) throw new HttpError(400, 'QUESTION_BODY_REQUIRED', '询问内容不能为空');

      const now = new Date().toISOString();
      const question = {
        id: body.id || nextId(store, 'question', 'q'),
        reviewId: review.id,
        modelCode: review.modelCode,
        buyerId: review.buyerId,
        merchantId: merchant.id,
        body: String(questionBody),
        status: 'DELIVERED',
        createdAt: now,
        deliveredAt: now,
        openedAt: null,
        answeredAt: null,
        declinedAt: null,
        voidedAt: null,
        answer: null,
        publiclyVisible: true,
        delivery: {
          channel: 'BUYER_NORMAL_ACCOUNT_INBOX',
          buyerId: review.buyerId,
          deliveredAt: now,
        },
        events: [
          { type: 'QUESTION_CREATED_FOR_SPECIFIC_REVIEW', at: now, merchantId: merchant.id },
          { type: 'DELIVERED_TO_VERIFIED_REVIEW_AUTHOR_ACCOUNT', at: now, buyerId: review.buyerId },
        ],
      };
      store.questions[question.id] = question;
      appendEvent(store, 'question.sent', {
        questionId: question.id,
        reviewId: review.id,
        buyerId: question.buyerId,
        routedBy: 'review.receiptId -> receipt.buyerId / review.buyerId',
      });
      saveStore(store);
      return sendJson(res, 201, { ok: true, question: questionView(question, { full: true }) });
    }

    if (req.method === 'GET' && pathname === '/api/merchant/questions') {
      authMerchant(req);
      const questions = Object.values(store.questions)
        .sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt))
        .map((q) => questionView(q, { full: true }));
      return sendJson(res, 200, { questions });
    }

    if (req.method === 'GET' && pathname === '/api/questions') {
      const buyer = optionalBuyer(req, body);
      const merchantToken = req.headers['x-merchant-token'] || bearerToken(req);
      if (buyer && !merchantToken) {
        return sendJson(res, 200, {
          questions: Object.values(store.questions).filter((q) => q.buyerId === buyer.id).map((q) => questionView(q, { full: true })),
        });
      }
      authMerchant(req);
      return sendJson(res, 200, { questions: Object.values(store.questions).map((q) => questionView(q, { full: true })) });
    }

    const buyerQuestionsMatch = pathname.match(/^\/api\/buyers\/([^/]+)\/questions$/);
    if (req.method === 'GET' && buyerQuestionsMatch) {
      const buyerId = decodeURIComponent(buyerQuestionsMatch[1]);
      const buyer = optionalBuyer(req, body);
      const merchant = req.headers['x-merchant-token'] ? authMerchant(req) : null;
      if (!merchant && (!buyer || buyer.id !== buyerId)) {
        throw new HttpError(403, 'FORBIDDEN', '只能从本人买家账号查看收件箱，或由商家凭证查看');
      }
      return sendJson(res, 200, {
        buyerId,
        questions: Object.values(store.questions).filter((q) => q.buyerId === buyerId).map((q) => questionView(q, { full: true })),
      });
    }

    const questionActionMatch = pathname.match(/^\/api\/questions\/([^/]+)\/(open|answer|reply|replies|responses?|decline|refuse|reject)$/);
    if (req.method === 'POST' && questionActionMatch) {
      const questionId = decodeURIComponent(questionActionMatch[1]);
      const action = questionActionMatch[2];
      const question = getQuestionOr404(questionId);
      const buyer = authBuyer(req, body);
      requireBuyerOwnsQuestion(buyer, question);
      const now = new Date().toISOString();

      if (action === 'open') {
        if (['VOIDED'].includes(question.status)) throw new HttpError(409, 'QUESTION_VOIDED', '该询问已因收货依据更正而撤回');
        if (!question.openedAt) {
          question.openedAt = now;
          question.events.push({ type: 'OPENED_FROM_NORMAL_BUYER_ACCOUNT', at: now, buyerId: buyer.id });
          if (question.status === 'DELIVERED') question.status = 'OPENED';
          appendEvent(store, 'question.opened', { questionId, buyerId: buyer.id });
          saveStore(store);
        }
        return sendJson(res, 200, { ok: true, question: questionView(question, { full: true }) });
      }

      if (['answer', 'reply', 'replies', 'response', 'responses'].includes(action)) {
        if (question.status === 'VOIDED') {
          throw new HttpError(409, 'QUESTION_VOIDED', '收货凭证已更正为拒收，旧刊和未回询问已冻结，不能再作答');
        }
        if (question.status === 'DECLINED') {
          throw new HttpError(409, 'QUESTION_DECLINED', '该买家已拒收/拒绝此询问，不能再生成已答复回条');
        }
        if (question.status === 'ANSWERED') {
          throw new HttpError(409, 'QUESTION_ALREADY_ANSWERED', '这条询问已经作答', { answerId: question.answer?.id });
        }
        const answerBody = body.body || body.answer || body.content || body.text || body.reply;
        if (!answerBody || !String(answerBody).trim()) throw new HttpError(400, 'ANSWER_BODY_REQUIRED', '答复内容不能为空');
        const answer = {
          id: nextId(store, 'answer', 'ans'),
          buyerId: buyer.id,
          body: String(answerBody),
          createdAt: now,
          publiclyVisible: true,
        };
        question.answer = answer;
        question.status = 'ANSWERED';
        question.answeredAt = now;
        question.publiclyVisible = true;
        question.events.push({ type: 'ANSWER_RECEIVED_FROM_ROUTED_BUYER_ACCOUNT', at: now, answerId: answer.id });
        appendEvent(store, 'question.answered', { questionId, answerId: answer.id, buyerId: buyer.id });
        saveStore(store);
        const review = store.reviews[question.reviewId];
        const productPage = {
          modelCode: question.modelCode,
          url: `/p/${encodeURIComponent(question.modelCode)}`,
          reviewAnchor: `review-${question.reviewId}`,
          followUpAnchor: `followup-${question.id}`,
          answerAnchor: `answer-${answer.id}`,
          readerPath: `/p/${encodeURIComponent(question.modelCode)}#followup-${question.id}`,
        };
        return sendJson(res, 201, {
          ok: true,
          answer,
          answerReceipt: questionView(question).answerReceipt,
          productPage,
          readerPath: productPage.readerPath,
          review: review ? reviewView(store, review, { force: true }) : undefined,
        });
      }

      // decline / refuse / reject
      if (question.status === 'ANSWERED') throw new HttpError(409, 'QUESTION_ALREADY_ANSWERED', '已作答的询问不能改为拒收');
      if (question.status === 'VOIDED') throw new HttpError(409, 'QUESTION_VOIDED', '询问已因凭证更正作废');
      if (question.status !== 'DECLINED') {
        question.status = 'DECLINED';
        question.declinedAt = now;
        question.declineReason = body.reason || body.body || '买家拒收/不愿回答后续询问';
        question.answer = null;
        question.events.push({ type: 'BUYER_DECLINED_QUESTION', at: now, reason: question.declineReason });
        appendEvent(store, 'question.declined', { questionId, buyerId: buyer.id });
        saveStore(store);
      }
      return sendJson(res, 200, {
        ok: true,
        status: 'DECLINED',
        answeredReceipt: null,
        merchantReceipt: {
          type: 'QUESTION_DECLINED_NOT_ANSWERED',
          questionId,
          at: question.declinedAt,
        },
        question: questionView(question, { full: true }),
      });
    }

    const questionMatch = pathname.match(/^\/api\/questions\/([^/]+)$/);
    if (req.method === 'GET' && questionMatch) {
      const question = getQuestionOr404(decodeURIComponent(questionMatch[1]));
      const buyer = optionalBuyer(req);
      const merchant = req.headers['x-merchant-token'] || pathname.startsWith('/api/merchant');
      const full = Boolean(merchant) || buyer?.id === question.buyerId;
      return sendJson(res, 200, questionView(question, { full }));
    }

    const reviewMatch = pathname.match(/^\/api\/reviews\/([^/]+)$/);
    if (req.method === 'GET' && reviewMatch) {
      const review = getReviewOr404(decodeURIComponent(reviewMatch[1]));
      const includeAudit = url.searchParams.get('audit') === 'true' || req.headers['x-merchant-token'];
      return sendJson(res, 200, reviewView(store, review, { force: Boolean(includeAudit) }));
    }

    if (req.method === 'GET' && pathname === '/api/events') {
      return sendJson(res, 200, { events: store.events.slice(-200).reverse() });
    }

    throw new HttpError(404, 'ROUTE_NOT_FOUND', `没有路由 ${req.method} ${pathname}；GET /api 查看接口目录`);
  } catch (err) {
    const status = err.status || 500;
    if (status >= 500) console.error(err);
    return sendJson(res, status, {
      ok: false,
      error: {
        code: err.code || 'INTERNAL_ERROR',
        message: err.message || '服务器内部错误',
        details: err.details || undefined,
      },
    });
  }
});

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  server.listen(PORT, HOST, () => {
    console.log(`verified-fit-review-service listening on http://${HOST}:${PORT}`);
    console.log(`data file: ${dataFilePath()}`);
    console.log(`API directory: http://127.0.0.1:${PORT}/api`);
  });
}

export { server };
