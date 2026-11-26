import {
  HttpError,
  badRequest,
  conflict,
  notFound,
  unprocessable,
} from './errors.js';

export const MAX_ACTIVE_SENSES = 5;

export function nowIso(value) {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'string') return value;
  return new Date().toISOString();
}

export function nextId(db, prefix) {
  db.meta.counters ||= {};
  db.meta.counters[prefix] = (db.meta.counters[prefix] || 0) + 1;
  return `${prefix}_${db.meta.counters[prefix]}`;
}

export function addEvent(db, type, payload = {}, actorId = null, at = nowIso()) {
  const event = { id: nextId(db, 'EVT'), at, type, actorId, ...payload };
  db.events.push(event);
  return event;
}

export function findSpan(text, markedText) {
  if (typeof text !== 'string' || typeof markedText !== 'string' || !markedText) {
    throw badRequest('INVALID_SPAN', '必须提供例句 text 和被标片段 markedText。');
  }
  const start = text.indexOf(markedText);
  if (start < 0) {
    throw unprocessable(
      'MARKED_TEXT_NOT_FOUND',
      `被标片段“${markedText}”没有出现在例句中。`,
    );
  }
  return { start, end: start + markedText.length };
}

export function senseKeys(sense) {
  if (!sense) return [];
  if (Array.isArray(sense.semanticKeys)) return sense.semanticKeys;
  return sense.semanticKey ? [sense.semanticKey] : [];
}

export function activeSensesForHeadword(db, headwordId) {
  const hw = db.headwords[headwordId];
  if (!hw) return [];
  return hw.senseIds
    .map((id) => db.senses[id])
    .filter((s) => s && s.status === 'active');
}

export function normalizeTagRef(db, ref) {
  if (ref === undefined || ref === null || ref === '') return null;
  if (typeof ref === 'object') {
    if (ref.id && db.tags[ref.id]) return ref.id;
    if (ref.label) return normalizeTagRef(db, ref.label);
    if (ref.semanticKey) {
      const existing = Object.values(db.tags).find((t) => t.semanticKey === ref.semanticKey);
      if (existing) return existing.id;
    }
    const id = ref.id || nextId(db, 'TAG');
    db.tags[id] = {
      id,
      kind: 'discrimination-label',
      label: ref.label || ref.semanticKey || id,
      semanticKey: ref.semanticKey || ref.key || null,
      targetSenseId: ref.targetSenseId || null,
    };
    return id;
  }
  if (db.tags[ref]) return ref;
  const byLabel = Object.values(db.tags).find(
    (t) => t.label === ref || t.code === ref || t.semanticKey === ref,
  );
  if (byLabel) return byLabel.id;
  const knownKey = ref.includes('照明')
    ? 'illumination'
    : (ref.includes('提示') || ref.includes('示意') ? 'signal' : null);
  if (knownKey) {
    const byKey = Object.values(db.tags).find((t) => t.semanticKey === knownKey);
    if (byKey) return byKey.id;
    const id = knownKey === 'illumination' ? 'TAG_ILLUM' : 'TAG_CUE';
    db.tags[id] = { id, kind: 'discrimination-label', label: ref, semanticKey: knownKey };
    return id;
  }

  const id = nextId(db, 'TAG');
  db.tags[id] = { id, kind: 'discrimination-label', label: ref, semanticKey: null };
  return id;
}

export function resolveTagSense(db, tag, headwordId) {  if (!tag) return null;
  if (tag.targetSenseId) {
    const targeted = db.senses[tag.targetSenseId];
    if (targeted && targeted.status === 'active' && targeted.headwordId === headwordId) {
      return targeted;
    }
  }
  return activeSensesForHeadword(db, headwordId).find((s) =>
    senseKeys(s).includes(tag.semanticKey),
  ) || null;
}

export function certifiedSenseIds(example) {
  if (Array.isArray(example?.certifiedSenseIds) && example.certifiedSenseIds.length) {
    return [...new Set(example.certifiedSenseIds)];
  }
  return [];
}

function orderedSenseIds(db, headwordId, ids) {
  const order = db.headwords[headwordId]?.senseIds || Object.keys(db.senses);
  const unique = [...new Set(ids)];
  return unique.sort((a, b) => {
    const ia = order.indexOf(a);
    const ib = order.indexOf(b);
    return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
  });
}

function requireHeadword(db, id) {
  const hw = db.headwords[id];
  if (!hw) throw notFound('HEADWORD_NOT_FOUND', `找不到词头：${id}`);
  return hw;
}

function requireSense(db, id) {
  const sense = db.senses[id];
  if (!sense) throw notFound('SENSE_NOT_FOUND', `找不到义项：${id}`);
  return sense;
}

function requireExample(db, id) {
  const example = db.examples[id];
  if (!example) throw notFound('EXAMPLE_NOT_FOUND', `找不到例句：${id}`);
  return example;
}

/* ------------------------------ 词典编辑 ------------------------------ */

export function renameHeadword(db, headwordId, input = {}) {
  const hw = requireHeadword(db, headwordId);
  const text = String(input.text || '').trim();
  if (!text) throw badRequest('HEADWORD_TEXT_REQUIRED', '新词头字符串不能为空。');

  const actorId = input.actorId || input.editorId || null;
  if (text !== hw.text) {
    hw.formerTexts ||= [];
    hw.formerTexts.push({ text: hw.text, changedAt: nowIso(input.now), by: actorId });
    hw.text = text;
    hw.updatedAt = nowIso(input.now);
    db.meta.dictionaryVersion += 1;
    addEvent(db, 'headword.renamed', { headwordId, text }, actorId, input.now);
  }
  return hw;
}

export function splitSense(db, senseId, input = {}) {
  const oldSense = requireSense(db, senseId);
  const actorId = input.editorId || input.actorId || null;
  const requested = Array.isArray(input.senses)
    ? input.senses
    : (input.newSenses || input.replacementSenses || []);

  // 允许网络重试：同一个旧义已经按相同编号拆开时，直接回返已有结果。
  if (oldSense.status === 'split') {
    const children = (oldSense.splitInto || []).map((id) => db.senses[id]).filter(Boolean);
    const sameRequest = requested.length === 0 ||
      requested.every((s, i) => !s.id || s.id === children[i]?.id);
    if (sameRequest && children.length >= 2) {
      return { oldSense, senses: children, idempotentReplay: true };
    }
    throw conflict(
      'SENSE_ALREADY_SPLIT',
      `义项 ${senseId} 已拆为 ${oldSense.splitInto?.join('、')}，不能再次拆分。`,
      { splitInto: oldSense.splitInto },
    );
  }
  if (oldSense.status !== 'active') {
    throw conflict('SENSE_NOT_ACTIVE', `义项 ${senseId} 当前状态为 ${oldSense.status}，不能拆分。`);
  }
  if (requested.length < 2) {
    throw badRequest('SPLIT_TOO_FEW_SENSES', '拆分旧义至少要给出两个新义项。');
  }

  const hw = requireHeadword(db, oldSense.headwordId);
  const activeOthers = hw.senseIds
    .map((id) => db.senses[id])
    .filter((s) => s && s.status === 'active' && s.id !== oldSense.id);
  if (activeOthers.length + requested.length > MAX_ACTIVE_SENSES) {
    throw unprocessable(
      'TOO_MANY_ACTIVE_SENSES',
      `活跃义项不能超过 ${MAX_ACTIVE_SENSES} 个；本题五个义项以内即可。`,
    );
  }

  const maxNumber = Math.max(0, ...Object.values(db.senses).map((s) => Number(s.number) || 0));
  const created = requested.map((spec, index) => {
    if (!spec.label || !spec.definition) {
      throw badRequest('SENSE_FIELDS_REQUIRED', '每个新义项都必须有 label 和 definition。');
    }
    const id = spec.id || `${oldSense.id}_${(spec.code || index + 1)}`;
    if (db.senses[id]) throw conflict('SENSE_ID_EXISTS', `义项编号已存在：${id}`);
    const semanticKey = spec.semanticKey || spec.key || null;
    const sense = {
      id,
      headwordId: hw.id,
      number: spec.number || maxNumber + index + 1,
      code: spec.code || id,
      label: spec.label,
      definition: spec.definition,
      semanticKey,
      semanticKeys: spec.semanticKeys || (semanticKey ? [semanticKey] : []),
      status: 'active',
      active: true,
      lineage: { kind: 'split-child', parentSenseId: oldSense.id },
      createdAt: nowIso(input.now),
      updatedAt: nowIso(input.now),
    };
    db.senses[id] = sense;
    if (!hw.senseIds.includes(id)) hw.senseIds.push(id);
    return sense;
  });

  oldSense.status = 'split';
  oldSense.active = false;
  oldSense.splitInto = created.map((s) => s.id);
  oldSense.lineage = {
    ...(oldSense.lineage || {}),
    splitAt: nowIso(input.now),
    splitBy: actorId,
    reason: input.reason || null,
  };
  oldSense.updatedAt = nowIso(input.now);

  db.meta.dictionaryVersion += 1;
  addEvent(
    db,
    'sense.split',
    { senseId: oldSense.id, splitInto: oldSense.splitInto, reason: input.reason || null },
    actorId,
    input.now,
  );
  return { oldSense, senses: created, idempotentReplay: false };
}

function retargetExampleUsagesAfterMerge(db, example, sourceIds, targetSense) {
  for (const usageId of [...example.usageIds]) {
    const usage = db.usages[usageId];
    if (!usage || usage.status !== 'active' || !sourceIds.includes(usage.senseId)) continue;

    const targetUsageId = `U_${example.id}_${targetSense.id}`;
    let targetUsage = db.usages[targetUsageId];
    if (!targetUsage) {
      targetUsage = {
        id: targetUsageId,
        exampleId: example.id,
        headwordId: example.headwordId,
        span: usage.span,
        markedText: usage.markedText,
        senseId: targetSense.id,
        status: 'active',
        mergedFromUsageId: usage.id,
        createdAt: nowIso(),
        updatedAt: nowIso(),
      };
      db.usages[targetUsageId] = targetUsage;
      example.usageIds.push(targetUsageId);
    } else {
      targetUsage.status = 'active';
      targetUsage.senseId = targetSense.id;
      targetUsage.updatedAt = nowIso();
    }
    usage.status = 'retired';
    usage.retiredReason = 'sense-merged';
    usage.replacedByUsageId = targetUsage.id;
    usage.updatedAt = nowIso();
  }

  example.certifiedSenseIds = orderedSenseIds(
    db,
    example.headwordId,
    example.usageIds
      .map((id) => db.usages[id])
      .filter((u) => u && u.status === 'active')
      .map((u) => u.senseId),
  );
  example.updatedAt = nowIso();
}

export function mergeSenses(db, input = {}) {
  const actorId = input.editorId || input.actorId || null;
  const sourceIds = [...new Set(input.sourceSenseIds || input.senseIds || [])];
  if (sourceIds.length < 1) {
    throw badRequest('MERGE_SOURCES_REQUIRED', '请给出要合并的 sourceSenseIds。');
  }
  const sources = sourceIds.map((id) => requireSense(db, id));
  const headwordId = sources[0].headwordId;
  if (sources.some((s) => s.headwordId !== headwordId)) {
    throw badRequest('MERGE_HEADWORD_MISMATCH', '只能合并同一个词头下的义项。');
  }
  if (sources.some((s) => s.status !== 'active')) {
    throw conflict('MERGE_SOURCE_NOT_ACTIVE', '只有活跃义项可以合并；历史义项已冻结保留。');
  }

  const hw = requireHeadword(db, headwordId);
  let target;
  if (input.targetSenseId) {
    target = requireSense(db, input.targetSenseId);
    if (target.headwordId !== headwordId) {
      throw badRequest('MERGE_TARGET_HEADWORD_MISMATCH', '目标义项必须属于同一词头。');
    }
    if (target.status !== 'active') {
      throw conflict('MERGE_TARGET_NOT_ACTIVE', '合并目标义项不是活跃状态。');
    }
  } else if (input.targetSense) {
    const spec = input.targetSense;
    if (!spec.label || !spec.definition) {
      throw badRequest('SENSE_FIELDS_REQUIRED', '合并后的新义项必须有 label 和 definition。');
    }
    const id = spec.id || nextId(db, 'SENSE');
    if (db.senses[id]) throw conflict('SENSE_ID_EXISTS', `义项编号已存在：${id}`);
    const maxNumber = Math.max(0, ...Object.values(db.senses).map((s) => Number(s.number) || 0));
    const semanticKey = spec.semanticKey || spec.key || null;
    target = {
      id,
      headwordId,
      number: spec.number || maxNumber + 1,
      code: spec.code || id,
      label: spec.label,
      definition: spec.definition,
      semanticKey,
      semanticKeys: spec.semanticKeys || (semanticKey ? [semanticKey] : []),
      status: 'active',
      active: true,
      lineage: { kind: 'merge-target' },
      mergedFrom: [],
      createdAt: nowIso(input.now),
      updatedAt: nowIso(input.now),
    };
    db.senses[id] = target;
    hw.senseIds.push(id);
  } else {
    throw badRequest('MERGE_TARGET_REQUIRED', '必须提供 targetSenseId 或 targetSense。');
  }

  const mergedSourceIds = sourceIds.filter((id) => id !== target.id);
  for (const sourceId of mergedSourceIds) {
    const source = db.senses[sourceId];
    source.status = 'merged';
    source.active = false;
    source.mergedInto = target.id;
    source.lineage = {
      ...(source.lineage || {}),
      mergedAt: nowIso(input.now),
      mergedBy: actorId,
      reason: input.reason || null,
    };
    source.updatedAt = nowIso(input.now);
    target.mergedFrom ||= [];
    if (!target.mergedFrom.includes(sourceId)) target.mergedFrom.push(sourceId);
    for (const key of senseKeys(source)) {
      if (!senseKeys(target).includes(key)) {
        target.semanticKeys ||= [];
        target.semanticKeys.push(key);
      }
    }
  }

  for (const example of Object.values(db.examples).filter((e) => e.headwordId === headwordId)) {
    retargetExampleUsagesAfterMerge(db, example, mergedSourceIds, target);
  }

  // 只迁移当前草稿；editions 是不可变快照，绝不在此改写。
  for (const q of Object.values(db.questions).filter((x) => x.headwordId === headwordId)) {
    let changed = false;
    if (mergedSourceIds.includes(q.usageSenseId)) {
      q.usageSenseId = target.id;
      const activeUsage = Object.values(db.usages).find(
        (u) => u.exampleId === q.exampleId && u.senseId === target.id && u.status === 'active',
      );
      if (activeUsage) q.usageId = activeUsage.id;
      changed = true;
    }
    for (const opt of q.options || []) {
      if (mergedSourceIds.includes(opt.senseId)) {
        opt.senseId = target.id;
        changed = true;
      }
    }
    if (changed) {
      q.lineageMigrations ||= [];
      q.lineageMigrations.push({ at: nowIso(input.now), mergedSourceIds, targetSenseId: target.id });
      q.updatedAt = nowIso(input.now);
      evaluateQuestion(db, q);
    }
  }

  db.meta.dictionaryVersion += 1;
  addEvent(
    db,
    'sense.merged',
    { sourceSenseIds: mergedSourceIds, targetSenseId: target.id, reason: input.reason || null },
    actorId,
    input.now,
  );
  return target;
}

/* ------------------------------- 例句核义 ------------------------------ */

export function createExample(db, input = {}) {
  const hw = requireHeadword(db, input.headwordId || 'HW_DADENG');
  const id = input.id || nextId(db, 'E');
  if (db.examples[id]) throw conflict('EXAMPLE_ID_EXISTS', `例句编号已存在：${id}`);
  const text = String(input.text || '').trim();
  const markedText = input.markedText || input.target || hw.text;
  const span = input.span || findSpan(text, markedText);
  const example = {
    id,
    headwordId: hw.id,
    text,
    markedText,
    span,
    usageIds: [],
    certifiedSenseIds: [],
    certifiedBy: [],
    verifications: [],
    certifications: [],
    consensusStatus: 'unverified',
    status: 'unverified',
    createdAt: nowIso(input.now),
    updatedAt: nowIso(input.now),
  };
  db.examples[id] = example;

  const senseIds = input.senseIds || input.certifiedSenseIds;
  if (Array.isArray(senseIds) && senseIds.length) {
    certifyExample(db, id, senseIds, {
      editorIds: input.editorIds || (input.editorId ? [input.editorId] : []),
      source: input.source || 'direct-certification',
      now: input.now,
    });
  }
  addEvent(db, 'example.created', { exampleId: id }, input.actorId || null, input.now);
  return db.examples[id];
}

export function certifyExample(db, exampleId, senseIdsInput, meta = {}) {
  const example = requireExample(db, exampleId);
  if (!Array.isArray(senseIdsInput) || senseIdsInput.length === 0) {
    throw badRequest('CERTIFIED_SENSES_REQUIRED', '核定用法至少包含一个义项。');
  }
  for (const sid of senseIdsInput) {
    const sense = db.senses[sid];
    if (!sense) throw badRequest('SENSE_NOT_FOUND', `核定用法引用了不存在的义项：${sid}`);
    if (sense.headwordId !== example.headwordId) {
      throw badRequest('SENSE_HEADWORD_MISMATCH', `义项 ${sid} 不属于例句的词头。`);
    }
  }
  const senseIds = orderedSenseIds(db, example.headwordId, senseIdsInput);
  const span = example.span || findSpan(example.text, example.markedText);

  for (const sid of senseIds) {
    const usageId = `U_${example.id}_${sid}`;
    let usage = db.usages[usageId];
    if (!usage) {
      usage = {
        id: usageId,
        exampleId: example.id,
        headwordId: example.headwordId,
        span,
        markedText: example.markedText,
        senseId: sid,
        status: 'active',
        createdAt: nowIso(meta.now),
        updatedAt: nowIso(meta.now),
      };
      db.usages[usageId] = usage;
      example.usageIds.push(usageId);
    }
    usage.status = 'active';
    usage.span = span;
    usage.markedText = example.markedText;
    usage.updatedAt = nowIso(meta.now);
    if (!example.usageIds.includes(usageId)) example.usageIds.push(usageId);
  }

  for (const usageId of example.usageIds) {
    const usage = db.usages[usageId];
    if (usage && !senseIds.includes(usage.senseId) && usage.status === 'active') {
      usage.status = 'retired';
      usage.retiredReason = meta.source === 'two-editor-consensus'
        ? 'new-consensus-recertification'
        : 'recertified';
      usage.updatedAt = nowIso(meta.now);
    }
  }

  example.certifiedSenseIds = senseIds;
  example.certifiedBy = meta.editorIds || [];
  example.consensusStatus = 'verified';
  example.status = 'verified';
  example.updatedAt = nowIso(meta.now);
  const certification = {
    id: nextId(db, 'CERT'),
    at: nowIso(meta.now),
    senseIds,
    editorIds: meta.editorIds || [],
    source: meta.source || 'direct-certification',
    verificationIds: meta.verificationIds || [],
    note: meta.note || null,
  };
  example.certifications.push(certification);
  addEvent(
    db,
    'example.certified',
    { exampleId, senseIds, source: certification.source },
    (meta.editorIds || [])[0] || null,
    meta.now,
  );
  return example;
}

export function addVerification(db, exampleId, input = {}) {
  const example = requireExample(db, exampleId);
  const editorId = input.editorId || input.actorId;
  if (!editorId) throw badRequest('EDITOR_REQUIRED', '核义请求必须带 editorId。');
  if (!db.actors[editorId] || db.actors[editorId].role !== 'editor') {
    throw badRequest('NOT_EDITOR', `${editorId} 不是登记编者。`);
  }
  const senseIds = [...new Set(input.senseIds || [])];
  if (!senseIds.length) throw badRequest('VERIFICATION_SENSES_REQUIRED', '请给出编者核定的 senseIds。');
  for (const sid of senseIds) requireSense(db, sid);

  const verification = {
    id: nextId(db, 'VER'),
    editorId,
    senseIds: orderedSenseIds(db, example.headwordId, senseIds),
    note: input.note || null,
    at: nowIso(input.now),
  };
  example.verifications.push(verification);

  const latestByEditor = new Map();
  for (const v of example.verifications) latestByEditor.set(v.editorId, v);
  const groups = new Map();
  for (const v of latestByEditor.values()) {
    const key = [...v.senseIds].sort().join('|');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(v);
  }
  const consensus = [...groups.values()].find((vers) => vers.length >= 2);
  let certified = false;
  if (consensus) {
    certifyExample(db, exampleId, consensus[0].senseIds, {
      editorIds: consensus.map((v) => v.editorId),
      verificationIds: consensus.map((v) => v.id),
      source: 'two-editor-consensus',
      now: input.now,
    });
    certified = true;
  } else {
    example.consensusStatus = 'pending';
  }
  return { verification, consensus: certified, example };
}

/* ------------------------------- 出题与判定 ----------------------------- */

function normalizeOptions(db, questionId, inputOptions) {
  if (!Array.isArray(inputOptions) || inputOptions.length < 2) {
    throw badRequest('OPTIONS_REQUIRED', '一道练习至少需要两个选项。');
  }
  const ids = new Set();
  return inputOptions.map((opt, index) => {
    const id = opt.optionId || opt.id || `${questionId}_OPT_${index + 1}`;
    if (ids.has(id)) throw badRequest('DUPLICATE_OPTION_ID', `选项编号重复：${id}`);
    ids.add(id);
    const definition = String(opt.definition ?? opt.text ?? '').trim();
    if (!definition) throw badRequest('OPTION_DEFINITION_REQUIRED', '每个选项都必须有释义/文本。');
    return {
      id,
      optionId: id,
      senseId: opt.senseId || null,
      definition,
      text: definition,
      correct: Boolean(opt.correct || opt.isCorrect || false),
    };
  });
}

export function createQuestion(db, input = {}) {
  const teacherId = input.teacherId || input.actorId || 'TEA1';
  const example = requireExample(db, input.exampleId);
  const id = input.id || input.questionId || nextId(db, 'Q');
  if (db.questions[id]) throw conflict('QUESTION_ID_EXISTS', `练习编号已存在：${id}`);

  let usageId = input.usageId || null;
  let usageSenseId = input.usageSenseId || input.senseId || null;
  if (usageId) {
    const usage = db.usages[usageId];
    if (!usage || usage.exampleId !== example.id) {
      throw badRequest('USAGE_NOT_FOUND', 'usageId 不属于该例句。');
    }
    usageSenseId = usage.senseId;
  } else if (usageSenseId) {
    const usage = Object.values(db.usages).find(
      (u) => u.exampleId === example.id && u.senseId === usageSenseId && u.status === 'active',
    );
    if (usage) usageId = usage.id;
  } else if (example.certifiedSenseIds.length === 1) {
    usageSenseId = example.certifiedSenseIds[0];
    const usage = Object.values(db.usages).find(
      (u) => u.exampleId === example.id && u.senseId === usageSenseId && u.status === 'active',
    );
    usageId = usage?.id || null;
  }

  const options = normalizeOptions(db, id, input.options);
  const explicitCorrectId = input.correctOptionId || null;
  if (explicitCorrectId) {
    for (const opt of options) opt.correct = opt.id === explicitCorrectId;
  }
  const q = {
    id,
    teacherId,
    headwordId: example.headwordId,
    exampleId: example.id,
    usageId,
    usageSenseId,
    requiredTagId: normalizeTagRef(
      db,
      input.requiredTagId ?? input.tagId ?? input.requiredTag ??
      input.discriminationLabel ?? input.tagLabel ?? input.discrimination_tag,
    ),
    prompt: input.prompt || '请选择例句中被标片段的正确释义。',
    selectionMode: input.selectionMode || 'single',
    requireUniqueExample: input.requireUniqueExample !== false,
    options,
    correctOptionIds: options.filter((o) => o.correct).map((o) => o.id),
    status: 'draft',
    publishedEditionIds: [],
    createdAt: nowIso(input.now),
    updatedAt: nowIso(input.now),
  };
  db.questions[id] = q;
  evaluateQuestion(db, q);
  addEvent(db, 'question.created', { questionId: id }, teacherId, input.now);
  return q;
}

export function updateQuestion(db, questionId, patch = {}) {
  const q = db.questions[questionId];
  if (!q) throw notFound('QUESTION_NOT_FOUND', `找不到练习：${questionId}`);

  const tagRef = patch.requiredTagId ?? patch.tagId ?? patch.requiredTag ??
    patch.discriminationLabel ?? patch.tagLabel ?? patch.discrimination_tag;
  if (tagRef !== undefined) {
    q.requiredTagId = tagRef === null ? null : normalizeTagRef(db, tagRef);
  }
  if (patch.prompt !== undefined) q.prompt = patch.prompt;
  if (patch.selectionMode !== undefined) q.selectionMode = patch.selectionMode;
  if (patch.requireUniqueExample !== undefined) {
    q.requireUniqueExample = Boolean(patch.requireUniqueExample);
  }
  if (patch.exampleId !== undefined) {
    const example = requireExample(db, patch.exampleId);
    q.exampleId = example.id;
    q.headwordId = example.headwordId;
    q.usageId = null;
    q.usageSenseId = null;
  }
  if (patch.usageId !== undefined || patch.usageSenseId !== undefined) {
    const usageId = patch.usageId;
    const usageSenseId = patch.usageSenseId || patch.senseId;
    if (usageId) {
      const usage = db.usages[usageId];
      if (!usage || usage.exampleId !== q.exampleId) {
        throw badRequest('USAGE_NOT_FOUND', 'usageId 不属于该例句。');
      }
      q.usageId = usage.id;
      q.usageSenseId = usage.senseId;
    } else if (usageSenseId) {
      q.usageSenseId = usageSenseId;
      const usage = Object.values(db.usages).find(
        (u) => u.exampleId === q.exampleId && u.senseId === usageSenseId && u.status === 'active',
      );
      q.usageId = usage?.id || null;
    }
  }
  if (patch.options !== undefined) {
    q.options = normalizeOptions(db, q.id, patch.options);
    const explicitCorrectId = patch.correctOptionId || null;
    if (explicitCorrectId) for (const o of q.options) o.correct = o.id === explicitCorrectId;
    q.correctOptionIds = q.options.filter((o) => o.correct).map((o) => o.id);
  }
  q.updatedAt = nowIso();
  evaluateQuestion(db, q);
  addEvent(db, 'question.updated', { questionId: q.id }, patch.actorId || patch.teacherId || null);
  return q;
}

export function assessQuestion(db, question) {
  const q = typeof question === 'string' ? db.questions[question] : question;
  if (!q) throw notFound('QUESTION_NOT_FOUND', '找不到待评估练习。');
  const example = db.examples[q.exampleId];
  const tag = db.tags[q.requiredTagId];
  const blockers = [];
  const warnings = [];

  if (q.selectionMode && q.selectionMode !== 'single') {
    blockers.push({
      code: 'TEACHER_REDESIGN_REQUIRED_MODE',
      message: '本服务交付的是“例句唯一体现所辨义项”的单选题；兼义/多选形式须由教师重编题干与答案规则后另发新版。',
      details: { selectionMode: q.selectionMode },
    });
  }

  if (!example) blockers.push({ code: 'EXAMPLE_NOT_FOUND', message: '练习引用的例句不存在。' });
  if (q.requiredTagId && !tag) {
    blockers.push({ code: 'TAG_NOT_FOUND', message: `辨义标签不存在：${q.requiredTagId}` });
  }

  const targetSense = tag ? resolveTagSense(db, tag, q.headwordId) : null;
  if (tag && !targetSense) {
    blockers.push({
      code: 'MISSING_DICTIONARY_EVIDENCE',
      message: `现有词典中没有能支撑辨义标签“${tag.label}”的活跃义项。`,
      details: { requiredTagId: tag.id, semanticKey: tag.semanticKey },
    });
  }

  const certified = example ? certifiedSenseIds(example) : [];
  if (targetSense && example) {
    if (!certified.includes(targetSense.id)) {
      blockers.push({
        code: 'EXAMPLE_TAG_MISMATCH',
        message: `例句核定用法为 ${certified.join('、') || '（未核义）'}，不体现标签“${tag.label}”所要求的 ${targetSense.id}。`,
        details: { certifiedSenseIds: certified, requiredSenseId: targetSense.id },
      });
    }

    if (
      q.selectionMode === 'single' &&
      q.requireUniqueExample !== false &&
      certified.length !== 1
    ) {
      blockers.push({
        code: 'AMBIGUOUS_EXAMPLE_FOR_UNIQUE_ANSWER',
        message: `本练习要求例句本身唯一体现答案，但该句核定用法有 ${certified.join('、') || '零个'}，不能保留同一个唯一答案。`,
        details: { certifiedSenseIds: certified },
      });
    }

    if (q.usageSenseId && q.usageSenseId !== targetSense.id) {
      blockers.push({
        code: 'USAGE_TAG_MISMATCH',
        message: `练习引用的用法是 ${q.usageSenseId}，而标签“${tag.label}”对应 ${targetSense.id}。`,
        details: { usageSenseId: q.usageSenseId, requiredSenseId: targetSense.id },
      });
    }
  }

  const correctOptions = (q.options || []).filter((o) => o.correct);
  if (q.selectionMode === 'single' && correctOptions.length !== 1) {
    blockers.push({
      code: 'INVALID_UNIQUE_ANSWER_KEY',
      message: `单选题必须恰好有一个正确选项，当前有 ${correctOptions.length} 个。`,
    });
  }
  if (targetSense && correctOptions.length === 1 && correctOptions[0].senseId &&
      correctOptions[0].senseId !== targetSense.id) {
    blockers.push({
      code: 'CORRECT_OPTION_SENSE_MISMATCH',
      message: '正确选项标记的义项与辨义标签对应的词典义项不一致。',
      details: { optionSenseId: correctOptions[0].senseId, requiredSenseId: targetSense.id },
    });
  }
  const definitions = (q.options || []).map((o) => o.definition.trim());
  if (new Set(definitions).size !== definitions.length) {
    blockers.push({ code: 'DUPLICATE_OPTIONS', message: '选项释义重复，无法形成可靠选择。' });
  }

  const eligible = blockers.length === 0;
  const primary = blockers[0];
  return {
    subjectType: 'question',
    subjectId: q.id,
    eligible,
    verdict: eligible
      ? ((q.publishedEditionIds || []).length ? 'continue_with_new_edition' : 'continue')
      : 'teacher_rewrite',
    reasonCode: primary?.code || 'ELIGIBLE_UNIQUE',
    reason: primary?.message || '例句、标签、用法和唯一正确选项一致，可进入新版。',
    blockers,
    warnings,
    evidence: {
      headwordId: q.headwordId,
      exampleId: q.exampleId,
      usageId: q.usageId,
      usageSenseId: q.usageSenseId,
      requiredTagId: q.requiredTagId,
      tagLabel: tag?.label || null,
      semanticKey: tag?.semanticKey || null,
      resolvedSenseId: targetSense?.id || null,
      certifiedSenseIds: certified,
      selectionMode: q.selectionMode,
      requireUniqueExample: q.requireUniqueExample !== false,
    },
    dictionaryVersion: db.meta.dictionaryVersion,
  };
}

export function evaluateQuestion(db, question) {
  const q = typeof question === 'string' ? db.questions[question] : question;
  if (!q) throw notFound('QUESTION_NOT_FOUND', '找不到待评估练习。');
  const result = assessQuestion(db, q);
  const decision = {
    id: nextId(db, 'DEC'),
    at: nowIso(),
    ...result,
  };
  db.decisions.push(decision);
  q.latestDecisionId = decision.id;
  q.eligibility = {
    eligible: decision.eligible,
    verdict: decision.verdict,
    reasonCode: decision.reasonCode,
    decisionId: decision.id,
    evaluatedAt: decision.at,
  };
  return decision;
}

export function evaluateCandidate(db, input = {}) {
  const exampleId = input.exampleId;
  const tagId = input.requiredTagId || input.tagId;
  const candidate = {
    id: input.id || `CAND_${exampleId}_${tagId}_${input.selectionMode || 'single'}`,
    headwordId: input.headwordId || db.examples[exampleId]?.headwordId,
    exampleId,
    requiredTagId: tagId,
    usageId: input.usageId || null,
    usageSenseId: input.usageSenseId || null,
    selectionMode: input.selectionMode || 'single',
    requireUniqueExample: input.requireUniqueExample !== false,
    options: input.options || [{ correct: true, definition: 'candidate' }],
    publishedEditionIds: [],
  };
  return assessQuestion(db, candidate);
}

export function buildDecisionReport(db, query = {}) {
  const headwordId = query.headwordId || 'HW_DADENG';
  const examples = Object.values(db.examples).filter((e) => e.headwordId === headwordId);
  const tags = Object.values(db.tags);
  const rows = [];
  for (const example of examples) {
    for (const tag of tags) {
      const decision = evaluateCandidate(db, { exampleId: example.id, tagId: tag.id });
      rows.push({
        candidateId: `${example.id}__${tag.id}`,
        exampleId: example.id,
        tagId: tag.id,
        tagLabel: tag.label,
        certifiedSenseIds: example.certifiedSenseIds,
        eligible: decision.eligible,
        verdict: decision.verdict,
        reasonCode: decision.reasonCode,
        reason: decision.reason,
        evidence: decision.evidence,
      });
    }
  }
  return {
    classroomModel: 'versioned-edition',
    headwordId,
    dictionaryVersion: db.meta.dictionaryVersion,
    generatedAt: nowIso(),
    rule: '唯一答案题要求：标签能映射活跃义项，该义项在例句核定用法中，且例句核定用法集合只有这一个义项。',
    rows,
  };
}

/* -------------------------------- 发行版本 ------------------------------ */

function snapshotQuestionForEdition(db, q, editionId) {
  const example = db.examples[q.exampleId];
  const tag = q.requiredTagId ? db.tags[q.requiredTagId] : null;
  const usage = q.usageId ? db.usages[q.usageId] : null;
  return {
    id: q.id,
    questionId: q.id,
    sourceQuestionUpdatedAt: q.updatedAt,
    prompt: q.prompt,
    selectionMode: q.selectionMode,
    requireUniqueExample: q.requireUniqueExample !== false,
    example: {
      id: example.id,
      text: example.text,
      markedText: example.markedText,
      span: { ...example.span },
      // 发行时冻结核定用法；之后编者改义不影响本版。
      certifiedSenseIds: [...example.certifiedSenseIds],
    },
    usage: usage ? {
      id: usage.id,
      senseId: usage.senseId,
      span: { ...usage.span },
      markedText: usage.markedText,
    } : { id: q.usageId, senseId: q.usageSenseId },
    requiredTag: tag ? { id: tag.id, label: tag.label, semanticKey: tag.semanticKey } : null,
    options: q.options.map((opt, index) => ({
      ...opt,
      number: String.fromCharCode(65 + index),
      optionId: opt.id,
      id: opt.id,
      definition: opt.definition,
      text: opt.definition,
    })),
    correctOptionIds: [...q.correctOptionIds],
  };
}

export function publishEdition(db, input = {}) {
  const teacherId = input.teacherId || input.actorId || 'TEA1';
  const questionIds = input.questionIds ||
    (input.paperId && db.papers[input.paperId]?.questionIds) || [];
  if (!Array.isArray(questionIds) || questionIds.length === 0) {
    throw badRequest('EDITION_QUESTIONS_REQUIRED', '发行版本至少要包含一道题。');
  }

  const editionId = input.editionId || input.id || nextId(db, 'EDITION');
  if (db.editions[editionId]) {
    const existing = db.editions[editionId];
    const same = JSON.stringify(existing.questionIds) === JSON.stringify(questionIds);
    if (same) return { edition: existing, idempotentReplay: true };
    throw conflict('EDITION_ID_EXISTS', `版本编号已存在且题目不同：${editionId}`);
  }

  const questions = questionIds.map((id) => db.questions[id]);
  if (questions.some((q) => !q)) throw notFound('QUESTION_NOT_FOUND', '版本中包含不存在的题目。');

  const decisions = questions.map((q) => evaluateQuestion(db, q));
  const blocked = decisions.filter((d) => !d.eligible);
  if (blocked.length) {
    throw unprocessable(
      'EDITION_NOT_READY',
      '题目未通过唯一性/标签/词典证据检查，不能按唯一答案发行；请教师重编或恢复证据。',
      { decisions: blocked },
    );
  }

  const headwordIds = [...new Set(questions.map((q) => q.headwordId))];
  if (headwordIds.length !== 1) {
    throw badRequest('EDITION_MULTIPLE_HEADWORDS', '当前示例服务的版本请保持同一词头。');
  }
  const hw = requireHeadword(db, headwordIds[0]);
  const snapshots = questions.map((q) => snapshotQuestionForEdition(db, q, editionId));
  const senseIds = new Set(hw.senseIds);
  for (const sq of snapshots) {
    senseIds.add(sq.usage?.senseId);
    for (const opt of sq.options) if (opt.senseId) senseIds.add(opt.senseId);
  }
  const glossary = {};
  for (const sid of senseIds) {
    if (sid && db.senses[sid]) glossary[sid] = structuredClone(db.senses[sid]);
  }

  const edition = {
    id: editionId,
    paperId: input.paperId || null,
    title: input.title || `方言练习册 ${editionId}`,
    classroomModel: 'versioned-edition',
    status: 'published',
    immutable: true,
    version: input.version || 1,
    questionIds: [...questionIds],
    questions: snapshots,
    headword: {
      id: hw.id,
      text: hw.text,
      pinyin: hw.pinyin,
      dialect: hw.dialect || null,
    },
    glossary,
    dictionaryVersionAtPublish: db.meta.dictionaryVersion,
    answerKey: Object.fromEntries(snapshots.map((q) => [q.id, q.correctOptionIds])),
    publishedBy: teacherId,
    publishedAt: nowIso(input.now),
    note: input.note || null,
  };
  db.editions[editionId] = edition;
  for (const q of questions) {
    q.status = 'published';
    q.publishedEditionIds ||= [];
    q.publishedEditionIds.push(editionId);
  }
  addEvent(db, 'edition.published', { editionId, questionIds }, teacherId, input.now);
  return { edition, idempotentReplay: false };
}

export function toStudentEdition(edition) {
  const copy = structuredClone(edition);
  delete copy.answerKey;
  for (const q of copy.questions || []) {
    delete q.correctOptionIds;
    for (const opt of q.options || []) delete opt.correct;
  }
  return copy;
}

/* -------------------------------- 学生做题 ------------------------------ */

export function openAttempt(db, input = {}) {
  const editionId = input.editionId;
  const edition = db.editions[editionId];
  if (!edition) throw notFound('EDITION_NOT_FOUND', `找不到发行版本：${editionId}`);
  const id = input.attemptId || input.id || nextId(db, 'ATT');
  if (db.attempts[id]) {
    const existing = db.attempts[id];
    if (input.studentId && existing.studentId !== input.studentId) {
      throw conflict('ATTEMPT_OWNER_MISMATCH', `作答号 ${id} 已属于其他学生。`);
    }
    return { attempt: existing, idempotentReplay: true };
  }
  const studentId = input.studentId || 'STU_ANON';
  const attempt = {
    id,
    studentId,
    editionId,
    classroomModel: 'versioned-edition',
    status: 'open',
    openedAt: nowIso(input.now),
    seenAt: nowIso(input.now),
    snapshot: toStudentEdition(edition),
    displayedOptionIds: Object.fromEntries(
      edition.questions.map((q) => [q.id, q.options.map((o) => o.id)]),
    ),
    answers: null,
    submission: null,
    review: null,
    idempotency: {},
  };
  db.attempts[id] = attempt;
  addEvent(db, 'attempt.opened', { attemptId: id, editionId }, studentId, input.now);
  return { attempt, idempotentReplay: false };
}

function normalizeAnswerInput(payload) {
  if (Array.isArray(payload.answers)) return payload.answers;
  if (payload.answers && typeof payload.answers === 'object') {
    return Object.entries(payload.answers).map(([questionId, value]) => {
      if (value && typeof value === 'object') return { questionId, ...value };
      return { questionId, optionId: value };
    });
  }
  if (payload.questionId) return [payload];
  return [];
}

// 幂等比较时也只在该 attempt 的冻结快照内解析编号，避免把旧 A 与新版 A 混为一谈。
function resolvedAnswerSignature(attempt, answers) {
  const refs = answers.map((a) => {
    const questionId = a.questionId || a.id;
    const q = attempt.snapshot.questions.find((x) => x.id === questionId);
    let optionId = a.optionId || a.option_id || null;
    const numberRef = a.optionNumber || a.number ||
      (!a.optionId && !a.option_id && typeof a.answer === 'string' && /^[A-Z]$/.test(a.answer)
        ? a.answer
        : null);
    if ((!optionId || numberRef) && numberRef && q) {
      const byNumber = q.options.find((o) => o.number === String(numberRef).toUpperCase());
      if (byNumber) optionId = byNumber.id;
    }
    return [questionId, optionId || `UNRESOLVED:${optionId || numberRef || 'empty'}`];
  });
  return JSON.stringify(refs.sort(([a], [b]) => String(a).localeCompare(String(b))));
}

export function submitAttempt(db, attemptId, payload = {}, options = {}) {
  const attempt = db.attempts[attemptId];
  if (!attempt) throw notFound('ATTEMPT_NOT_FOUND', `找不到作答记录：${attemptId}`);

  const expectedEditionId = options.expectedEditionId || null;
  if (expectedEditionId && expectedEditionId !== attempt.editionId) {
    throw conflict(
      'EDITION_MISMATCH',
      `该作答属于 ${attempt.editionId}，不能把旧选项号提交/套用到路径中的 ${expectedEditionId}。`,
      { attemptEditionId: attempt.editionId, requestEditionId: expectedEditionId },
    );
  }
  if (payload.editionId && payload.editionId !== attempt.editionId) {
    throw conflict(
      'EDITION_MISMATCH',
      `断线补交的作答号属于 ${attempt.editionId}；请求却指向 ${payload.editionId}。请按原所见快照判卷。`,
      { attemptEditionId: attempt.editionId, requestEditionId: payload.editionId },
    );
  }

  const idempotencyKey = payload.clientSubmissionId ||
    options.idempotencyKey ||
    payload.idempotencyKey ||
    null;

  if (attempt.status === 'submitted') {
    const requestedAnswers = normalizeAnswerInput(payload);
    const requestedSignature = resolvedAnswerSignature(attempt, requestedAnswers);
    if (idempotencyKey && attempt.idempotency?.[idempotencyKey]) {
      const record = attempt.idempotency[idempotencyKey];
      if (record.answerSignature && record.answerSignature !== requestedSignature) {
        throw conflict(
          'IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_ANSWERS',
          '同一个 clientSubmissionId 不能重发不同答案；请使用新的提交号或核对断线前内容。',
          { reviewId: attempt.review?.id },
        );
      }
      return { review: attempt.review, idempotentReplay: true };
    }
    if (requestedAnswers.length && attempt.submission &&
        requestedSignature === resolvedAnswerSignature(attempt, attempt.submission.answers)) {
      return { review: attempt.review, idempotentReplay: true };
    }
    throw conflict(
      'ATTEMPT_ALREADY_SUBMITTED',
      '这份作答已经交卷；不同答案不能覆盖旧成绩。相同断线重试会幂等返回原复核。',
      { reviewId: attempt.review?.id },
    );
  }

  const inputAnswers = normalizeAnswerInput(payload);
  if (!inputAnswers.length) throw badRequest('ANSWERS_REQUIRED', '提交内容必须包含 answers。');

  const normalized = [];
  const seenQuestions = new Set();
  for (const raw of inputAnswers) {
    const questionId = raw.questionId || raw.id;
    const sq = attempt.snapshot.questions.find((q) => q.id === questionId);
    if (!sq) {
      throw badRequest(
        'UNKNOWN_QUESTION_IN_ATTEMPT',
        `本题没有向作答 ${attempt.id} 展示过题目 ${questionId}。`,
      );
    }
    if (seenQuestions.has(questionId)) {
      throw badRequest('DUPLICATE_ANSWER', `题目 ${questionId} 出现了重复答案。`);
    }
    seenQuestions.add(questionId);

    let byId = null;
    let byNumber = null;
    const optionRef = raw.optionId || raw.option_id || null;
    const numberRef = raw.optionNumber || raw.number ||
      (!raw.optionId && typeof raw.answer === 'string' && /^[A-Z]$/.test(raw.answer) ? raw.answer : null);

    if (optionRef) byId = sq.options.find((o) => o.id === optionRef) || null;
    if (numberRef) byNumber = sq.options.find((o) => o.number === String(numberRef).toUpperCase()) || null;

    if (optionRef && !byId) {
      throw badRequest(
        'OPTION_NOT_VISIBLE_IN_ATTEMPT',
        `选项 ${optionRef} 不在该学员当时所见的 ${attempt.editionId} 快照中；不能把旧选项编号套到新版。`,
        {
          questionId,
          attemptEditionId: attempt.editionId,
          visibleOptionIds: sq.options.map((o) => o.id),
          visibleNumbers: sq.options.map((o) => o.number),
        },
      );
    }
    if (numberRef && !byNumber) {
      throw badRequest(
        'OPTION_NUMBER_NOT_VISIBLE',
        `选项号 ${numberRef} 不在该学员所见快照中。`,
        { questionId, visibleNumbers: sq.options.map((o) => o.number) },
      );
    }
    if (byId && byNumber && byId.id !== byNumber.id) {
      throw badRequest(
        'OPTION_REFERENCE_MISMATCH',
        'optionId 与 optionNumber 在该学员所见版本中指向不同选项；为避免串版，已拒绝。',
        { byId: byId.id, byNumber: byNumber.id },
      );
    }
    const selected = byId || byNumber;
    if (!selected) {
      throw badRequest('OPTION_REFERENCE_REQUIRED', `题目 ${questionId} 缺少 optionId 或 optionNumber。`);
    }
    normalized.push({ questionId, optionId: selected.id, optionNumber: selected.number });
  }

  const fullEdition = db.editions[attempt.editionId];
  if (!fullEdition) throw new HttpError(500, 'EDITION_SNAPSHOT_MISSING', '判卷所需版本快照缺失。');

  const items = fullEdition.questions.map((fq) => {
    const answer = normalized.find((a) => a.questionId === fq.id) || null;
    const studentQuestion = attempt.snapshot.questions.find((q) => q.id === fq.id);
    const selected = answer
      ? studentQuestion.options.find((o) => o.id === answer.optionId)
      : null;
    const correct = fq.options.find((o) => fq.correctOptionIds.includes(o.id)) || null;
    const isCorrect = Boolean(selected && correct && selected.id === correct.id);
    return {
      questionId: fq.id,
      exampleId: fq.example.id,
      isCorrect,
      answered: Boolean(selected),
      selected: selected ? {
        optionId: selected.id,
        optionNumber: selected.number,
        senseId: selected.senseId || null,
        definition: selected.definition,
      } : null,
      correct: correct ? {
        optionId: correct.id,
        optionNumber: correct.number,
        senseId: correct.senseId || null,
        definition: correct.definition,
      } : null,
      frozenUsage: fq.usage,
      frozenRequiredTag: fq.requiredTag,
    };
  });

  const correctCount = items.filter((i) => i.isCorrect).length;
  const review = {
    id: nextId(db, 'REV'),
    attemptId: attempt.id,
    studentId: attempt.studentId,
    editionId: attempt.editionId,
    gradedAt: nowIso(),
    total: items.length,
    answeredCount: normalized.length,
    correctCount,
    score: items.length ? correctCount / items.length : 0,
    gradingBasis: 'OPTIONS_AND_DEFINITIONS_DISPLAYED_TO_STUDENT',
    editionImmutable: true,
    note: 'optionNumber 只在该 attempt 冻结的所见快照内解析；当前词典或新版编号不改变本复核。',
    items,
  };

  attempt.status = 'submitted';
  attempt.submittedAt = review.gradedAt;
  attempt.submission = {
    at: review.gradedAt,
    clientSubmissionId: idempotencyKey,
    answers: normalized,
  };
  attempt.review = review;
  if (idempotencyKey) {
    attempt.idempotency[idempotencyKey] = {
      reviewId: review.id,
      at: review.gradedAt,
      answerSignature: resolvedAnswerSignature(attempt, normalized),
    };
  }
  addEvent(db, 'attempt.submitted', {
    attemptId: attempt.id,
    editionId: attempt.editionId,
    correctCount,
    total: items.length,
  }, attempt.studentId);
  return { review, idempotentReplay: false };
}

export function getReview(db, attemptId) {
  const attempt = db.attempts[attemptId];
  if (!attempt) throw notFound('ATTEMPT_NOT_FOUND', `找不到作答记录：${attemptId}`);
  if (!attempt.review) throw conflict('ATTEMPT_NOT_SUBMITTED', '这份作答尚未交卷，还没有复核结果。');
  const currentDictionary = {
    headword: (() => {
      const hw = db.headwords[attempt.snapshot.headword?.id];
      return hw ? { id: hw.id, currentText: hw.text, snapshotText: attempt.snapshot.headword?.text } : null;
    })(),
    senses: {},
  };
  const senseIds = new Set();
  for (const item of attempt.review.items) {
    if (item.selected?.senseId) senseIds.add(item.selected.senseId);
    if (item.correct?.senseId) senseIds.add(item.correct.senseId);
    if (item.frozenUsage?.senseId) senseIds.add(item.frozenUsage.senseId);
  }
  for (const sid of senseIds) {
    if (db.senses[sid]) {
      const s = db.senses[sid];
      currentDictionary.senses[sid] = {
        id: s.id,
        number: s.number,
        label: s.label,
        definition: s.definition,
        status: s.status,
        splitInto: s.splitInto || null,
        mergedInto: s.mergedInto || null,
      };
    }
  }
  return { ...structuredClone(attempt.review), currentDictionary };
}

/* --------------------------------- 索引 -------------------------------- */

export function buildIndex(db, query = {}) {
  const q = String(query.q || query.keyword || '').trim();
  let entries = Object.values(db.headwords).map((hw) => ({
    headwordId: hw.id,
    headword: hw.text,
    pinyin: hw.pinyin,
    dialect: hw.dialect || null,
    activeSenseIds: hw.senseIds.filter((sid) => db.senses[sid]?.status === 'active'),
    allSenseIds: hw.senseIds,
    formerTexts: (hw.formerTexts || []).map((f) => f.text),
    dictionaryVersion: db.meta.dictionaryVersion,
    updatedAt: hw.updatedAt,
  }));
  if (q) {
    entries = entries.filter((e) =>
      e.headword.includes(q) ||
      (e.pinyin || '').toLowerCase().includes(q.toLowerCase()) ||
      e.formerTexts.some((x) => x.includes(q)) ||
      e.activeSenseIds.some((sid) => {
        const s = db.senses[sid];
        return s && (`${s.label}${s.definition}${s.code || ''}`).includes(q);
      }),
    ).map((e) => ({
      ...e,
      matchType: e.headword.includes(q) ? 'current-headword' :
        e.formerTexts.some((x) => x.includes(q)) ? 'former-headword-cross-reference' :
        'sense-text',
    }));
  }
  return { generatedAt: nowIso(), count: entries.length, entries };
}

export function getSenseImpact(db, senseId) {
  const sense = requireSense(db, senseId);
  const exampleIds = Object.values(db.usages)
    .filter((u) => u.senseId === senseId)
    .map((u) => u.exampleId);
  const questionIds = Object.values(db.questions)
    .filter((q) => q.usageSenseId === senseId || q.options.some((o) => o.senseId === senseId))
    .map((q) => q.id);
  const editionIds = Object.values(db.editions)
    .filter((ed) => JSON.stringify(ed.glossary).includes(`"${senseId}"`))
    .map((ed) => ed.id);
  const attemptIds = Object.values(db.attempts)
    .filter((a) => editionIds.includes(a.editionId))
    .map((a) => a.id);
  return {
    senseId,
    status: sense.status,
    splitInto: sense.splitInto || null,
    mergedInto: sense.mergedInto || null,
    exampleIds: [...new Set(exampleIds)],
    questionIds,
    editionIds,
    attemptIds,
    rule: 'editionIds/attemptIds 中的旧版和已展示选项继续使用冻结快照判卷。',
  };
}
