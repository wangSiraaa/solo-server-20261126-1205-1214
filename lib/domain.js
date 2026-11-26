export const RECEIPT_TYPES = {
  ARRIVAL: 'ARRIVAL',
  REJECTION: 'REJECTION',
  RETURN: 'RETURN',
  EXCHANGE_DELIVERY: 'EXCHANGE_DELIVERY',
};

export const ACCEPTED_RECEIPT_TYPES = new Set([
  RECEIPT_TYPES.ARRIVAL,
  RECEIPT_TYPES.EXCHANGE_DELIVERY,
]);

export const RECEIPT_TYPE_LABELS = {
  ARRIVAL: '到件/正常签收凭证',
  DELIVERY: '到件/正常签收凭证',
  DELIVERY_ACCEPTED: '到件/正常签收凭证',
  REJECTION: '拒收凭证',
  REJECTED: '拒收凭证',
  RETURN: '退件/换货收回凭证',
  EXCHANGE_PICKUP: '退件/换货收回凭证',
  EXCHANGE_DELIVERY: '换件补交签收凭证',
  REPLACEMENT_DELIVERY: '换件补交签收凭证',
};

export function isoTime(value) {
  if (!value) return new Date().toISOString();
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) {
    const err = new Error('invalid ISO timestamp');
    err.code = 'INVALID_TIME';
    throw err;
  }
  return d.toISOString();
}

export function effectiveReceipt(store, receiptId) {
  let current = store.receipts[receiptId];
  if (!current) return null;
  const seen = new Set();
  while (current && current.status === 'CORRECTED' && current.correctedByReceiptId) {
    if (seen.has(current.id)) break;
    seen.add(current.id);
    const next = store.receipts[current.correctedByReceiptId];
    if (!next) break;
    current = next;
  }
  return current;
}

export function intervalForReceipt(store, receipt) {
  if (!receipt || !ACCEPTED_RECEIPT_TYPES.has(receipt.type)) return null;
  const start = new Date(receipt.occurredAt);
  const returns = Object.values(store.receipts)
    .filter((r) => {
      const effective = effectiveReceipt(store, r.id);
      return effective
        && effective.type === RECEIPT_TYPES.RETURN
        && effective.unitId === receipt.unitId
        && new Date(effective.occurredAt).getTime() >= start.getTime();
    })
    .sort((a, b) => new Date(a.occurredAt) - new Date(b.occurredAt));

  const endReceipt = returns[0] ? effectiveReceipt(store, returns[0].id) : null;
  const end = endReceipt ? new Date(endReceipt.occurredAt) : null;
  return {
    unitId: receipt.unitId,
    start: start.toISOString(),
    end: end ? end.toISOString() : null,
    returnReceiptId: endReceipt?.id || null,
    currentlyHeld: !end || end.getTime() > Date.now(),
  };
}

export function evaluateExperienceReceipt(store, input) {
  const { buyerId, modelCode, receiptId, at } = input;
  const raw = store.receipts[receiptId];
  if (!raw) {
    return { status: 'UNVERIFIED', code: 'RECEIPT_NOT_FOUND', message: '收货凭证不存在' };
  }

  const receipt = effectiveReceipt(store, receiptId) || raw;
  const corrected = receipt.id !== raw.id;
  const atTime = at ? new Date(at).getTime() : Date.now();

  if (receipt.buyerId !== buyerId || raw.buyerId !== buyerId) {
    return {
      status: 'UNVERIFIED',
      code: 'BUYER_MISMATCH',
      message: '凭证上的收货人与评价作者不一致',
      receipt,
      rawReceipt: raw,
    };
  }

  if (receipt.type === RECEIPT_TYPES.REJECTION) {
    return {
      status: 'UNVERIFIED',
      code: corrected ? 'RECEIPT_CORRECTED_TO_REJECTION' : 'DELIVERY_REJECTED',
      message: corrected
        ? '该收货登记已更正为拒收，不能证明买家曾收到此款'
        : '该凭证是拒收凭证，不能证明买家曾收到此款',
      receipt,
      rawReceipt: raw,
    };
  }

  if (!ACCEPTED_RECEIPT_TYPES.has(receipt.type)) {
    return {
      status: 'UNVERIFIED',
      code: 'RECEIPT_NOT_AN_ACCEPTED_DELIVERY',
      message: `凭证类型 ${receipt.type} 不能单独证明收用`,
      receipt,
      rawReceipt: raw,
    };
  }

  if (receipt.modelCode !== modelCode) {
    return {
      status: 'UNVERIFIED',
      code: 'MODEL_MISMATCH',
      message: `凭证实际款号为 ${receipt.modelCode}，不能挂到 ${modelCode} 下`,
      receipt,
      rawReceipt: raw,
    };
  }

  const interval = intervalForReceipt(store, receipt);
  if (!interval) {
    return {
      status: 'UNVERIFIED',
      code: 'POSSESSION_INTERVAL_MISSING',
      message: '无法确定实际持有区间',
      receipt,
      rawReceipt: raw,
    };
  }

  const start = new Date(interval.start).getTime();
  const end = interval.end ? new Date(interval.end).getTime() : null;
  if (atTime < start || (end !== null && atTime > end)) {
    return {
      status: 'UNVERIFIED',
      code: 'NOT_HELD_AT_EXPERIENCE_TIME',
      message: '标注的体验/发文时间不在该件实际可试用的持有区间内',
      receipt,
      rawReceipt: raw,
      interval,
    };
  }

  return {
    status: 'VERIFIED',
    code: 'VERIFIED',
    message: '收货凭证与实际收用区间可核对',
    receipt,
    rawReceipt: corrected ? raw : undefined,
    interval,
  };
}

export function findActiveFirstReview(store, buyerId, modelCode, excludeId = null) {
  return Object.values(store.reviews).find((r) =>
    r.buyerId === buyerId
    && r.modelCode === modelCode
    && r.id !== excludeId
    && r.status === 'PUBLISHED');
}

export function reviewPublicState(store, review) {
  const verification = evaluateExperienceReceipt(store, {
    buyerId: review.buyerId,
    modelCode: review.modelCode,
    receiptId: review.receiptId,
    at: review.experiencedAt || review.publishedAt,
  });

  const active = review.status === 'PUBLISHED' && verification.status === 'VERIFIED';
  return { review, verification, active };
}

export function questionPublicView(question) {
  if (!question) return null;
  const answered = question.status === 'ANSWERED' && question.answer;
  if (!answered || question.publiclyVisible === false) {
    return {
      id: question.id,
      status: question.status,
      publicAnswer: null,
    };
  }
  return {
    id: question.id,
    anchor: `followup-${question.id}`,
    urlSuffix: `#followup-${question.id}`,
    merchantBody: question.body,
    createdAt: question.createdAt,
    answeredAt: question.answeredAt,
    answer: {
      id: question.answer.id,
      body: question.answer.body,
      createdAt: question.answer.createdAt,
      anchor: `answer-${question.answer.id}`,
    },
  };
}

export function reviewView(store, review, { force = false } = {}) {
  const { verification, active } = reviewPublicState(store, review);
  const question = Object.values(store.questions).find((q) => q.reviewId === review.id) || null;

  if (!active && !force) {
    return {
      id: review.id,
      modelCode: review.modelCode,
      status: 'WITHHELD',
      anchor: `review-${review.id}`,
      withheldAt: review.withheldAt || null,
      withholdReason: review.withholdReason || verification.message,
      verification,
      question: question ? { id: question.id, status: question.status } : null,
      modelWasNotMoved: true,
    };
  }

  return {
    id: review.id,
    status: active ? 'PUBLISHED' : review.status,
    modelCode: review.modelCode,
    orderId: review.orderId,
    receiptId: review.receiptId,
    anchor: `review-${review.id}`,
    urlSuffix: `/p/${encodeURIComponent(review.modelCode)}#review-${review.id}`,
    rating: review.rating,
    title: review.title,
    body: review.body,
    author: {
      buyerId: review.buyerId,
      displayName: store.buyers[review.buyerId]?.displayName || '匿名买家',
    },
    publishedAt: review.publishedAt,
    experiencedAt: review.experiencedAt || review.publishedAt,
    answerConsent: review.answerConsent ?? null,
    verification: {
      status: verification.status,
      code: verification.code,
      message: verification.message,
      receiptId: verification.receipt?.id || review.receiptId,
      voucherNo: verification.receipt?.voucherNo || null,
      receiptType: verification.receipt?.type || null,
      typeLabel: RECEIPT_TYPE_LABELS[verification.receipt?.type] || null,
      unitId: verification.receipt?.unitId || null,
      actualModelCode: verification.receipt?.modelCode || review.modelCode,
      interval: verification.interval || null,
      correctedFromReceiptId: verification.rawReceipt?.id || null,
      checkEndpoint: `/api/receipts/${review.receiptId}`,
    },
    addenda: (review.addenda || []).map((a) => {
      const v = evaluateExperienceReceipt(store, {
        buyerId: review.buyerId,
        modelCode: a.subjectModelCode || review.modelCode,
        receiptId: a.receiptId,
        at: a.createdAt,
      });
      return {
        ...a,
        status: a.status === 'WITHHELD' || v.status !== 'VERIFIED' ? 'WITHHELD' : 'PUBLISHED',
        anchor: `addendum-${a.id}`,
        verification: {
          status: v.status,
          code: v.code,
          message: v.message,
          receiptId: v.receipt?.id || a.receiptId,
          voucherNo: v.receipt?.voucherNo || null,
          unitId: v.receipt?.unitId || null,
          actualModelCode: v.receipt?.modelCode || a.subjectModelCode || review.modelCode,
          interval: v.interval || null,
        },
      };
    }),
    followUp: questionPublicView(question),
    questionForAudit: force ? question : undefined,
  };
}

export function productPageData(store, modelCode) {
  const product = store.products[modelCode] || { modelCode, name: modelCode };
  const all = Object.values(store.reviews)
    .filter((r) => r.modelCode === modelCode)
    .sort((a, b) => new Date(a.publishedAt) - new Date(b.publishedAt));

  const visible = [];
  const notices = [];
  for (const review of all) {
    const view = reviewView(store, review);
    if (view.status === 'PUBLISHED') visible.push(view);
    else notices.push(view);
  }

  const crossModelAddenda = [];
  for (const review of Object.values(store.reviews)) {
    for (const addendum of review.addenda || []) {
      const subject = addendum.subjectModelCode
        || effectiveReceipt(store, addendum.receiptId)?.modelCode
        || review.modelCode;
      if (subject === modelCode && review.modelCode !== modelCode && addendum.status !== 'WITHHELD') {
        crossModelAddenda.push({
          reviewId: review.id,
          sourceModelCode: review.modelCode,
          addendumId: addendum.id,
          anchor: `addendum-${addendum.id}`,
          sourceUrl: `/p/${encodeURIComponent(review.modelCode)}#addendum-${addendum.id}`,
          title: addendum.title,
          body: addendum.body,
          createdAt: addendum.createdAt,
          receiptId: addendum.receiptId,
        });
      }
    }
  }

  const followUpIndex = visible.flatMap((r) =>
    r.followUp?.answer
      ? [{
          reviewId: r.id,
          questionId: r.followUp.id,
          answerId: r.followUp.answer.id,
          answeredAt: r.followUp.answeredAt,
          anchor: r.followUp.anchor,
          url: `/p/${encodeURIComponent(modelCode)}#${r.followUp.anchor}`,
          excerpt: r.followUp.answer.body.slice(0, 80),
        }]
      : []);

  return {
    product,
    modelCode,
    url: `/p/${encodeURIComponent(modelCode)}`,
    reviews: visible,
    followUpIndex,
    crossModelAddenda,
    correctionNotices: notices,
    reviewCount: visible.length,
  };
}

export function applyCorrectionSideEffects(store, correctedReceipt, correction) {
  const affectedReviews = [];
  const voidedQuestions = [];
  const withheldAddenda = [];

  // A correction may itself be corrected later. Freeze publications that use
  // any receipt in the same correction chain; never re-attribute them.
  const chainIds = new Set([correctedReceipt.id, correction.id]);
  let cursor = correctedReceipt;
  const seen = new Set();
  while (cursor?.correctsReceiptId && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    chainIds.add(cursor.correctsReceiptId);
    cursor = store.receipts[cursor.correctsReceiptId];
    if (cursor) chainIds.add(cursor.id);
  }
  for (const candidate of Object.values(store.receipts)) {
    if (candidate.correctsReceiptId && chainIds.has(candidate.correctsReceiptId)) chainIds.add(candidate.id);
  }

  for (const review of Object.values(store.reviews)) {
    const touchesReviewProof = chainIds.has(review.receiptId);
    const touchedAddenda = (review.addenda || []).filter((a) => chainIds.has(a.receiptId));

    if (touchedAddenda.length) {
      for (const addendum of touchedAddenda) {
        const v = evaluateExperienceReceipt(store, {
          buyerId: review.buyerId,
          modelCode: addendum.subjectModelCode || review.modelCode,
          receiptId: addendum.receiptId,
          at: addendum.createdAt,
        });
        if (v.status !== 'VERIFIED' && addendum.status !== 'WITHHELD') {
          addendum.status = 'WITHHELD';
          addendum.withheldAt = correction.occurredAt;
          addendum.withholdReason = v.message;
          addendum.invalidatedByCorrectionId = correction.id;
          withheldAddenda.push({ reviewId: review.id, addendumId: addendum.id, reason: v.code });
        }
      }
    }

    if (!touchesReviewProof) continue;

    const verification = evaluateExperienceReceipt(store, {
      buyerId: review.buyerId,
      modelCode: review.modelCode,
      receiptId: review.receiptId,
      at: review.experiencedAt || review.publishedAt,
    });

    if (verification.status !== 'VERIFIED' && review.status === 'PUBLISHED') {
      review.status = 'WITHHELD';
      review.withheldAt = correction.occurredAt;
      review.withholdReason = verification.message;
      review.withholdCode = verification.code;
      review.invalidatedByCorrectionId = correction.id;
      affectedReviews.push({
        reviewId: review.id,
        modelCode: review.modelCode,
        status: 'WITHHELD',
        reason: verification.code,
        modelMovedTo: null,
      });

      for (const q of Object.values(store.questions).filter((x) => x.reviewId === review.id)) {
        if (['DELIVERED', 'OPEN', 'PENDING'].includes(q.status)) {
          q.status = 'VOIDED';
          q.voidedAt = correction.occurredAt;
          q.voidReason = 'UNDERLYING_RECEIPT_CORRECTED';
          q.voidExplanation = '旧刊收货依据已更正；询问随旧刊冻结，不会转投另一款或另一个买家。';
          q.events.push({
            type: 'QUESTION_VOIDED_BY_RECEIPT_CORRECTION',
            at: correction.occurredAt,
            correctionId: correction.id,
          });
          voidedQuestions.push(q.id);
        } else if (q.status === 'ANSWERED' && q.answer) {
          q.publiclyVisible = false;
          q.answer.publiclyVisible = false;
        }
      }
    }
  }

  return { affectedReviews, voidedQuestions, withheldAddenda };
}
