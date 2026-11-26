import crypto from 'node:crypto';

// ---------- small pure helpers ----------

export function normSound(value) {
  if (value === null || value === undefined) return '';
  return String(value).normalize('NFKC').trim().toLowerCase();
}

export function sameSound(a, b) {
  const na = normSound(a);
  const nb = normSound(b);
  return na !== '' && na === nb;
}

export function stableStringify(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value ?? null);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(',')}}`;
}

export function canonicalHash(value) {
  return crypto.createHash('sha256').update(stableStringify(value)).digest('hex');
}

export function newId(prefix) {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, '')}`;
}

export function dayNumber(value, fallback = null) {
  if (value === null || value === undefined || value === '') return fallback;
  const n = Number(value);
  return Number.isInteger(n) ? n : fallback;
}

export function uniqueSorted(values) {
  return [...new Set(values.filter((v) => v !== null && v !== undefined && v !== ''))].sort(
    (a, b) => (typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b))),
  );
}

// ---------- setup normalization ----------

export function normalizeHints(hints, assignmentId) {
  if (!Array.isArray(hints)) return [];
  return hints.map((h, i) => {
    if (typeof h === 'string' || typeof h === 'number') {
      return { id: `hint_${assignmentId}_${i + 1}`, kind: 'pronunciation', text: String(h) };
    }
    const id = h.id ?? h.hintId ?? `hint_${assignmentId}_${i + 1}`;
    return {
      id: String(id),
      kind: h.kind ?? 'pronunciation',
      text: h.text ?? h.label ?? h.prompt ?? String(id),
      ...(h.kpId ? { kpId: h.kpId } : {}),
    };
  });
}

export function makeItem({ assignment, card }) {
  return {
    assignmentId: assignment.id,
    unitId: assignment.unitId,
    cardId: assignment.cardId,
    glyph: card?.glyph ?? assignment.glyph ?? assignment.cardId,
    kpId: assignment.kpId,
    expected: assignment.expected,
    hints: normalizeHints(assignment.hints, assignment.id),
  };
}

// ---------- receipt analysis ----------

function answerOrder(answer, index) {
  const explicit = answer.sequence ?? answer.order ?? answer.at ?? answer.answerAt ?? answer.t;
  const n = Number(explicit);
  return Number.isFinite(n) ? n : index * 10000;
}

function normalizeHintRef(raw, item, answer) {
  if (raw === null || raw === undefined || raw === '') return null;
  if (typeof raw === 'string' || typeof raw === 'number') {
    const id = String(raw);
    const known = item.hints.find((h) => h.id === id || h.text === raw);
    return {
      hintId: known?.id ?? id,
      kpId: known?.kpId ?? item.kpId,
      known: Boolean(known),
      afterAnswer: false,
    };
  }

  const hintId = String(raw.hintId ?? raw.id ?? raw.hint ?? raw.text ?? '');
  const known = item.hints.find((h) => h.id === hintId || h.text === raw.text || h.text === hintId);
  const timing = raw.when ?? raw.timing ?? raw.phase;
  let afterAnswer =
    raw.afterAnswer === true ||
    raw.revealedAfterAnswer === true ||
    timing === 'after_answer' ||
    timing === 'after-answer' ||
    timing === 'after';

  const hintAt = Number(raw.at ?? raw.revealedAt ?? raw.sequence);
  const answerAt = Number(answer.answerAt ?? answer.at ?? answer.sequence);
  if (Number.isFinite(hintAt) && Number.isFinite(answerAt) && hintAt > answerAt) afterAnswer = true;
  if (raw.beforeAnswer === false || raw.revealedBeforeAnswer === false) afterAnswer = true;

  return {
    hintId: known?.id ?? hintId,
    kpId: raw.kpId ?? known?.kpId ?? item.kpId,
    known: Boolean(known),
    afterAnswer,
    at: Number.isFinite(hintAt) ? hintAt : null,
  };
}

function resolveItem(round, answer) {
  const assignmentId = answer.assignmentId ?? answer.itemId ?? answer.assignment;
  if (assignmentId) {
    const exact = round.items.find((i) => i.assignmentId === assignmentId);
    if (exact) return { item: exact };
  }

  const cardId = answer.cardId ?? answer.card ?? answer.cardID;
  if (cardId !== undefined) {
    const matches = round.items.filter((i) => String(i.cardId) === String(cardId));
    if (matches.length === 1) return { item: matches[0] };
    if (matches.length > 1) {
      if (answer.assignmentId) {
        const exact = matches.find((i) => i.assignmentId === answer.assignmentId);
        if (exact) return { item: exact };
      }
      return { ambiguous: true, matches };
    }
  }
  return { missing: true };
}

/**
 * Analyze one device receipt against the immutable round snapshot.
 * The answer array order is the local practice order unless explicit ordering exists.
 */
export function analyzeReceipt(round, receipt) {
  const rawAnswers = Array.isArray(receipt.answers)
    ? receipt.answers
    : Array.isArray(receipt.events)
      ? receipt.events.filter((e) => (e.type ?? e.kind) === 'answer').map((e) => e.answer ?? e)
      : [];

  const ordered = rawAnswers
    .map((answer, index) => ({ answer, order: answerOrder(answer, index), index }))
    .sort((a, b) => a.order - b.order || a.index - b.index);

  const hintKpsBeforeNow = new Set();
  const answers = [];

  for (const { answer, index, order } of ordered) {
    const resolution = resolveItem(round, answer);
    if (!resolution.item) {
      answers.push({
        order,
        originalIndex: index,
        cardId: answer.cardId ?? answer.card ?? null,
        assignmentId: answer.assignmentId ?? null,
        eligible: false,
        reason: resolution.ambiguous ? 'ambiguous_card_assignment' : 'card_not_in_original_round',
        ambiguous: Boolean(resolution.ambiguous),
      });
      continue;
    }

    const item = resolution.item;
    const rawHints = [
      ...(Array.isArray(answer.revealedHints) ? answer.revealedHints : []),
      ...(Array.isArray(answer.hints) ? answer.hints : []),
      ...(Array.isArray(answer.revealedHintIds) ? answer.revealedHintIds : []),
      ...(Array.isArray(answer.hintIds) ? answer.hintIds : []),
    ];
    const hintFlag = answer.hintRevealed ?? answer.hintsRevealed ?? answer.usedHint ?? answer.sawHint;
    if (hintFlag === true && rawHints.length === 0) rawHints.push('__hint_revealed__');

    const hints = rawHints
      .map((h) => normalizeHintRef(h, item, answer))
      .filter(Boolean)
      .map((h) => ({ ...h, kpId: h.kpId ?? item.kpId }));

    // Hints recorded without explicit after-answer timing are conservatively
    // treated as revealed before the correct spoken answer.
    for (const hint of hints.filter((h) => !h.afterAnswer)) hintKpsBeforeNow.add(hint.kpId);

    const firstGuess =
      answer.firstGuess ?? answer.initialGuess ?? answer.guess ?? answer.firstAttempt ?? null;
    // The receipt may only carry the first guessed pronunciation. In that case
    // that short string is also the answer used for server-side scoring.
    const finalAnswer =
      answer.finalAnswer ??
      answer.spoken ??
      answer.answer ??
      answer.answerText ??
      answer.text ??
      answer.pronunciation ??
      answer.spokenSyllable ??
      answer.syllable ??
      answer.said ??
      firstGuess ??
      null;
    const hasFinal = finalAnswer !== null && normSound(finalAnswer) !== '';
    const serverCorrect = hasFinal ? sameSound(finalAnswer, item.expected) : null;
    const deviceCorrect =
      typeof answer.correct === 'boolean'
        ? answer.correct
        : typeof answer.wasCorrect === 'boolean'
          ? answer.wasCorrect
          : typeof answer.isCorrect === 'boolean'
            ? answer.isCorrect
            : null;
    const correct = serverCorrect ?? deviceCorrect;
    const correctnessSource = hasFinal
      ? 'pronunciation_string'
      : deviceCorrect === null
        ? 'missing'
        : 'receipt_attested';

    const hintBeforeAnswer = hintKpsBeforeNow.has(item.kpId);
    const independentCorrect = correct === true && !hintBeforeAnswer;

    const normalized = {
      order,
      originalIndex: index,
      eligible: true,
      assignmentId: item.assignmentId,
      cardId: item.cardId,
      kpId: item.kpId,
      expected: item.expected,
      glyph: item.glyph,
      firstGuess,
      firstGuessNormalized: normSound(firstGuess) || null,
      firstGuessCorrect: firstGuess === null ? null : sameSound(firstGuess, item.expected),
      finalAnswer,
      finalAnswerNormalized: normSound(finalAnswer) || null,
      correct,
      serverCorrect,
      deviceCorrect,
      correctnessSource,
      revealedHints: hints.map((h) => h.hintId).sort(),
      hintDetails: hints,
      hintUsed: hints.length > 0,
      hintBeforeAnswer,
      independentCorrect,
    };
    answers.push(normalized);

    // An explicitly post-answer hint can still occur before the next card.
    for (const hint of hints.filter((h) => h.afterAnswer)) hintKpsBeforeNow.add(hint.kpId);
  }

  const kpClaims = {};
  for (const kpId of round.eligibleKpIds ?? uniqueSorted(round.items.map((i) => i.kpId))) {
    const related = answers.filter((a) => a.eligible && a.kpId === kpId);
    const independent = related.filter((a) => a.independentCorrect);
    const correct = related.filter((a) => a.correct === true);
    const incorrect = related.filter((a) => a.correct === false);
    const hinted = related.filter((a) => a.hintUsed || a.hintBeforeAnswer);

    let state = 'not_answered';
    if (independent.length > 0) state = 'independent';
    else if (correct.length > 0) state = 'hint_assisted';
    else if (incorrect.length > 0) state = 'incorrect';
    else if (related.length > 0) state = 'unknown';

    kpClaims[kpId] = {
      state,
      sufficientForOneRound: state === 'independent',
      independentCorrectCount: independent.length,
      correctCount: correct.length,
      incorrectCount: incorrect.length,
      hintUsedCount: hinted.length,
      answerAssignmentIds: related.map((a) => a.assignmentId),
      evidence: related.map((a) => ({
        assignmentId: a.assignmentId,
        cardId: a.cardId,
        glyph: a.glyph,
        firstGuess: a.firstGuess,
        finalAnswer: a.finalAnswer,
        correct: a.correct,
        revealedHints: a.revealedHints,
        hintBeforeAnswer: a.hintBeforeAnswer,
        independentCorrect: a.independentCorrect,
      })),
      reasons: reasonsForState(state, related),
    };
  }

  const factsForHash = {
    studentId: receipt.studentId ?? round.studentId ?? null,
    worksheetId: round.worksheetId ?? receipt.worksheetId ?? null,
    answers: answers
      .filter((a) => a.eligible)
      .map((a) => ({
        assignmentId: a.assignmentId,
        cardId: a.cardId,
        firstGuess: a.firstGuessNormalized,
        finalAnswer: a.finalAnswerNormalized,
        correct: a.correct,
        revealedHints: a.revealedHints,
        hintBeforeAnswer: a.hintBeforeAnswer,
        independentCorrect: a.independentCorrect,
      })),
    extraCards: answers.filter((a) => !a.eligible).map((a) => a.cardId),
  };

  return {
    answers,
    kpClaims,
    factsHash: canonicalHash(factsForHash),
    extraCardCount: answers.filter((a) => !a.eligible).length,
  };
}

function reasonsForState(state, related) {
  if (state === 'independent') {
    return ['correct_final_pronunciation', 'no_relevant_hint_before_correct_answer'];
  }
  if (state === 'hint_assisted') {
    const crossCard = related.some((a) => !a.hintUsed && a.hintBeforeAnswer);
    return crossCard
      ? ['correct_after_knowledge_point_hint_in_same_round', 'cross_card_independence_blocked']
      : ['correct_but_hint_was_revealed'];
  }
  if (state === 'incorrect') return ['no_correct_pronunciation_in_receipt'];
  if (state === 'unknown') return ['answer_correctness_could_not_be_verified'];
  return ['knowledge_point_not_answered_in_receipt'];
}

// ---------- multi-device reconciliation ----------

function answerFactSignature(a) {
  return {
    firstGuess: a.firstGuessNormalized,
    finalAnswer: a.finalAnswerNormalized,
    correct: a.correct,
    revealedHints: [...a.revealedHints].sort(),
    hintBeforeAnswer: a.hintBeforeAnswer,
  };
}

export function reconcileRound(round, submissions) {
  let considered = submissions;
  let adjudicated = false;
  let adjudication = null;

  if (round.adjudication?.submissionId) {
    const chosen = submissions.find((s) => s.id === round.adjudication.submissionId);
    if (chosen) {
      considered = [chosen];
      adjudicated = true;
      adjudication = round.adjudication;
    }
  }

  const kpResults = {};
  const conflicts = [];
  const discrepancies = [];

  const kpIds = round.eligibleKpIds ?? uniqueSorted(round.items.map((i) => i.kpId));
  for (const kpId of kpIds) {
    const copies = considered
      .map((sub) => ({ sub, claim: sub.analysis?.kpClaims?.[kpId] }))
      .filter((x) => x.claim);

    const touched = copies.filter((x) => x.claim.state !== 'not_answered');
    const states = uniqueSorted(touched.map((x) => x.claim.state));

    // Compare same-item facts across device copies.
    const byAssignment = new Map();
    for (const { sub, claim } of touched) {
      for (const ev of claim.evidence ?? []) {
        const key = ev.assignmentId;
        if (!byAssignment.has(key)) byAssignment.set(key, []);
        const normalizedAnswer = sub.analysis.answers.find((a) => a.assignmentId === key);
        byAssignment.get(key).push({
          submissionId: sub.id,
          deviceId: sub.deviceId,
          copyId: sub.copyId,
          signature: normalizedAnswer ? answerFactSignature(normalizedAnswer) : null,
        });
      }
    }

    const itemConflicts = [];
    for (const [assignmentId, observations] of byAssignment) {
      const signatures = uniqueSorted(observations.map((o) => stableStringify(o.signature)));
      if (signatures.length > 1) {
        const conflict = {
          kpId,
          assignmentId,
          kind: 'receipt_fact_mismatch',
          observations,
        };
        itemConflicts.push(conflict);
        discrepancies.push(conflict);
      }
    }

    let state = 'not_answered';
    if (touched.length === 0) state = 'not_answered';
    else if (adjudicated) state = touched[0].claim.state;
    else if (itemConflicts.length > 0 || states.length > 1) state = 'conflicted';
    else state = states[0];

    const evidenceSubmissionIds = uniqueSorted(touched.map((x) => x.sub.id));
    const result = {
      state,
      sufficientForOneRound: state === 'independent',
      candidateForMastery: state === 'independent',
      contributes: false, // daily-cap selection happens in rebuildEvidence
      adjudicated,
      evidenceSubmissionIds,
      deviceIds: uniqueSorted(touched.map((x) => x.sub.deviceId).filter(Boolean)),
      copyIds: uniqueSorted(touched.map((x) => x.sub.copyId).filter(Boolean)),
      itemConflicts: itemConflicts.map((c) => ({
        kpId: c.kpId,
        assignmentId: c.assignmentId,
        kind: c.kind,
        submissions: c.observations.map((o) => ({
          submissionId: o.submissionId,
          deviceId: o.deviceId,
          copyId: o.copyId,
          facts: o.signature,
        })),
      })),
      reasons:
        state === 'conflicted'
          ? ['device_copies_disagree_about_independence_evidence', 'new_server_issued_repair_round_required']
          : touched[0]?.claim.reasons ?? reasonsForState('not_answered', []),
    };
    kpResults[kpId] = result;

    if (state === 'conflicted') {
      conflicts.push({ kpId, itemConflicts: result.itemConflicts });
    }
  }

  const hasConflict = conflicts.length > 0;
  return {
    status: considered.length === 0 ? 'issued' : hasConflict ? 'conflicted' : 'confirmed',
    reconciledAt: new Date().toISOString(),
    submissionCount: submissions.length,
    consideredSubmissionCount: considered.length,
    adjudicated,
    adjudication,
    kpResults,
    conflicts,
    discrepancies,
    repairKpIds: conflicts.map((c) => c.kpId),
  };
}

// ---------- evidence ledger, daily cap, mastery and gates ----------

export function rebuildEvidence(db) {
  const candidates = [];

  for (const round of db.rounds ?? []) {
    const results = round.reconciliation?.kpResults ?? {};
    for (const [kpId, result] of Object.entries(results)) {
      if (result.state !== 'independent') continue;
      candidates.push({
        id: `ev_${round.id}_${kpId}`,
        studentId: round.studentId,
        kpId,
        roundId: round.id,
        day: round.day,
        worksheetId: round.worksheetId,
        mode: round.mode,
        adjudicated: Boolean(round.adjudication),
        submissionIds: result.evidenceSubmissionIds ?? [],
        confirmedAt: round.confirmedAt ?? round.reconciledAt ?? round.updatedAt ?? null,
        selected: false,
        countsTowardMastery: false,
        reason: null,
      });
    }
  }

  candidates.sort((a, b) => {
    const ta = a.confirmedAt ? Date.parse(a.confirmedAt) : Number.MAX_SAFE_INTEGER;
    const tb = b.confirmedAt ? Date.parse(b.confirmedAt) : Number.MAX_SAFE_INTEGER;
    return ta - tb || String(a.roundId).localeCompare(String(b.roundId)) || String(a.kpId).localeCompare(String(b.kpId));
  });

  const groups = new Map();
  for (const ev of candidates) {
    const key = `${ev.studentId}|${ev.kpId}|${ev.day}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(ev);
  }

  for (const group of groups.values()) {
    group[0].selected = true;
    group[0].countsTowardMastery = true;
    group[0].reason = 'independent_round_for_day';
    for (const extra of group.slice(1)) {
      extra.selected = false;
      extra.countsTowardMastery = false;
      extra.reason = 'daily_max_one_round';
    }
  }

  db.evidence = candidates;
  db.mastery = db.mastery ?? {};
  const mastery = {};
  for (const ev of candidates.filter((e) => e.countsTowardMastery)) {
    const key = `${ev.studentId}|${ev.kpId}`;
    if (!mastery[key]) {
      mastery[key] = {
        studentId: ev.studentId,
        kpId: ev.kpId,
        stage: 0,
        qualifyingRounds: [],
        qualifyingDays: [],
        worksheetIds: [],
      };
    }
    const m = mastery[key];
    m.stage += 1;
    m.qualifyingRounds.push(ev.roundId);
    m.qualifyingDays.push(ev.day);
    m.worksheetIds.push(ev.worksheetId);
  }
  for (const m of Object.values(mastery)) {
    m.qualifyingRounds = uniqueSorted(m.qualifyingRounds);
    m.qualifyingDays = uniqueSorted(m.qualifyingDays);
    m.worksheetIds = uniqueSorted(m.worksheetIds);
  }
  db.masteryByStudentKp = mastery;
  return candidates;
}

export function getMastery(db, studentId, kpId) {
  return db.masteryByStudentKp?.[`${studentId}|${kpId}`] ?? {
    studentId,
    kpId,
    stage: 0,
    qualifyingRounds: [],
    qualifyingDays: [],
    worksheetIds: [],
  };
}

export function evaluateGate(db, student, gate, asOfDay) {
  const day = dayNumber(asOfDay, dayNumber(db.meta?.currentDay, null));
  const requirements = (gate.requirements ?? []).map((reqInput) => {
    const req = typeof reqInput === 'string' ? { kpId: reqInput, independentRounds: 2 } : reqInput;
    const required = req.independentRounds ?? req.count ?? 2;
    const mastery = getMastery(db, student.id, req.kpId);
    const earned = mastery.stage;
    return {
      kpId: req.kpId,
      requiredIndependentRounds: required,
      earnedIndependentRounds: earned,
      earnedDays: mastery.qualifyingDays,
      earnedRounds: mastery.qualifyingRounds,
      met: earned >= required,
      stillNeeds: Math.max(0, required - earned),
    };
  });

  const requirementsMet = requirements.every((r) => r.met);
  const dayReached = day === null ? true : day >= gate.opensOnDay;
  const open = requirementsMet && dayReached && gate.enabled !== false;

  let status = 'open';
  if (gate.enabled === false) status = 'disabled';
  else if (!dayReached) status = 'scheduled_locked';
  else if (!requirementsMet) status = 'requirements_locked';

  return {
    gateId: gate.id,
    name: gate.name ?? gate.id,
    applicationLevelId: gate.applicationLevelId ?? gate.id,
    opensOnDay: gate.opensOnDay,
    asOfDay: day,
    open,
    status,
    requirements,
    reasons: [
      ...(!dayReached ? [`before_day_${gate.opensOnDay}`] : []),
      ...requirements.filter((r) => !r.met).map((r) => `missing_${r.stillNeeds}_independent_round_for_${r.kpId}`),
    ],
  };
}

export function getPracticeDays(db, student) {
  const days = new Set((student.practiceDays ?? []).map(Number).filter(Number.isInteger));
  for (const pack of db.packs ?? []) {
    if (pack.classId && student.classId && pack.classId !== student.classId) continue;
    if (pack.day !== null && pack.day !== undefined) days.add(Number(pack.day));
  }
  for (const round of db.rounds ?? []) {
    if (round.studentId === student.id && Number.isInteger(Number(round.day))) days.add(Number(round.day));
  }
  return [...days].sort((a, b) => a - b);
}

export function buildOpportunities(db, student, query = {}) {
  const practiceDays = getPracticeDays(db, student);
  const asOfDay = dayNumber(query.day ?? query.asOfDay, practiceDays.length ? Math.max(...practiceDays) : null);
  const gates = query.gateId
    ? (db.gates ?? []).filter((g) => g.id === query.gateId)
    : (db.gates ?? []).filter((g) => g.enabled !== false);

  const gateReports = gates.map((gate) => {
    const report = evaluateGate(db, student, gate, asOfDay);
    const requirements = report.requirements.map((req) => {
      const earnedDays = new Set(req.earnedDays);
      const contributableDays = practiceDays.filter((d) => !earnedDays.has(d));
      const nextPracticeDay = contributableDays.find((d) => asOfDay === null || d >= asOfDay) ?? null;
      const openPackDays = (db.packs ?? [])
        .filter((p) => (p.kpIds ?? []).includes(req.kpId))
        .filter((p) => !p.classId || !student.classId || p.classId === student.classId)
        .map((p) => p.day);
      return {
        ...req,
        practiceDays,
        contributableDays,
        nextPracticeDay,
        packDays: uniqueSorted(openPackDays),
        canMakeup: req.stillNeeds > 0,
        makeupEndpoint: 'POST /api/makeup-orders',
      };
    });
    return { ...report, requirements };
  });

  const makeupOrders = (db.makeupOrders ?? [])
    .filter((o) => o.studentId === student.id)
    .map((o) => ({
      ...o,
      claimEndpoint: o.status === 'open' ? 'POST /api/rounds/claim' : null,
    }));

  return {
    studentId: student.id,
    asOfDay,
    practiceDays,
    gates: gateReports,
    makeupOrders,
    evidence: (db.evidence ?? []).filter((e) => e.studentId === student.id),
    nextActions: [
      ...gateReports.filter((g) => !g.open).flatMap((g) =>
        g.requirements
          .filter((r) => r.canMakeup)
          .map((r) => ({
            action: 'create_makeup_order',
            endpoint: 'POST /api/makeup-orders',
            body: { studentId: student.id, gateId: g.gateId, kpId: r.kpId, day: r.nextPracticeDay },
          })),
      ),
      ...makeupOrders
        .filter((o) => o.status === 'open')
        .map((o) => ({
          action: 'claim_makeup_round',
          endpoint: 'POST /api/rounds/claim',
          body: { studentId: student.id, makeupOrderId: o.id, deviceId: 'device-1' },
        })),
    ],
  };
}

// ---------- adaptive selection ----------

export function selectAdaptiveItems(db, { student, previousRound = null, focusKpIds = [], count = 2 }) {
  const assignments = db.assignments ?? [];
  const cardsById = new Map((db.cards ?? []).map((c) => [c.id, c]));
  const previousCardIds = new Set((previousRound?.items ?? []).map((i) => i.cardId));
  const previousAssignmentIds = new Set((previousRound?.items ?? []).map((i) => i.assignmentId));

  const weakKpIds = [];
  for (const kpId of focusKpIds) weakKpIds.push(kpId);
  const results = previousRound?.reconciliation?.kpResults ?? {};
  for (const [kpId, result] of Object.entries(results)) {
    if (result.state !== 'independent' && !weakKpIds.includes(kpId)) weakKpIds.push(kpId);
  }

  const picked = [];
  const pickedAssignmentIds = new Set();

  function pickForKp(kpId, { alternateOnly = true } = {}) {
    let candidates = assignments
      .filter((a) => a.kpId === kpId)
      .filter((a) => !pickedAssignmentIds.has(a.id))
      .sort((a, b) => String(a.id).localeCompare(String(b.id)));

    const alternate = candidates.filter((a) => !previousCardIds.has(a.cardId) && !previousAssignmentIds.has(a.id));
    if (alternateOnly && alternate.length > 0) candidates = alternate;
    else if (alternateOnly && candidates.length > 0) {
      // Keep all candidates if no alternate exists.
    }
    const chosen = candidates[0];
    if (chosen) {
      picked.push(makeItem({ assignment: chosen, card: cardsById.get(chosen.cardId) }));
      pickedAssignmentIds.add(chosen.id);
    }
  }

  // One immediate alternate-card probe per weak/focus KP.
  for (const kpId of weakKpIds) pickForKp(kpId, { alternateOnly: true });

  // Fill the round from the deterministic course pool.
  const fillCandidates = assignments
    .filter((a) => !pickedAssignmentIds.has(a.id))
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  for (const assignment of fillCandidates) {
    if (picked.length >= count) break;
    picked.push(makeItem({ assignment, card: cardsById.get(assignment.cardId) }));
    pickedAssignmentIds.add(assignment.id);
  }

  return {
    items: picked,
    focusKpIds: uniqueSorted(weakKpIds),
    kpIds: uniqueSorted(picked.map((i) => i.kpId)),
  };
}
