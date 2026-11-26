import { assertColor } from './go.mjs';
import { badRequest, conflict, notFound } from './errors.mjs';
import { fingerprint } from './hash.mjs';
import { getMarkingState } from './game.mjs';

export function defaultLeague(now = new Date().toISOString()) {
  return {
    id: 'league-5x5',
    name: '五路小联赛',
    publishedAt: now,
    requiredRemoveVotes: 2,
    panelSize: 3,
    deadlineSeconds: 24 * 60 * 60,
    scoringRule: '空点四邻连通区仅接一色时归该色，加盘上存活子；无贴目，同分和',
    referees: [
      { id: 'ref-zhang', name: '张衡裁判', order: 1, onDuty: true },
      { id: 'ref-li', name: '李直裁判', order: 2, onDuty: true },
      { id: 'ref-wang', name: '王正裁判', order: 3, onDuty: false },
      { id: 'ref-zhao', name: '赵明裁判', order: 4, onDuty: true },
      { id: 'ref-qian', name: '钱慎裁判', order: 5, onDuty: true },
    ],
    // 开赛时公布的回避名单；gameId 为 * 时表示所有局回避。
    recusals: [
      {
        refereeId: 'ref-zhao',
        gameId: 'demo-dispute',
        reason: '开赛公布：赵明裁判与本局黑方同社，回避该对局号',
        publishedAt: now,
      },
    ],
    dutyEvents: [],
  };
}

export function isRecused(league, refereeId, game) {
  return league.recusals.filter(
    (r) => r.refereeId === refereeId && (r.gameId === '*' || r.gameId === game.id),
  );
}

export function eligibleReferees(league, game) {
  return league.referees
    .filter((ref) => ref.onDuty)
    .filter((ref) => isRecused(league, ref.id, game).length === 0)
    .sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
}

export function setRefereeDuty(league, refereeId, onDuty, input = {}) {
  const referee = league.referees.find((r) => r.id === refereeId);
  if (!referee) throw notFound('REFEREE_NOT_FOUND', `没有具名裁判：${refereeId}`);
  const now = input.now ?? new Date().toISOString();
  const from = referee.onDuty;
  referee.onDuty = Boolean(onDuty);
  const event = {
    at: now,
    refereeId,
    refereeName: referee.name,
    from,
    to: referee.onDuty,
    reason: input.reason || (referee.onDuty ? '值班开始' : '值班结束'),
  };
  league.dutyEvents.push(event);
  return { referee, event };
}

function makeCaseId(game, index) {
  return `case-${game.id}-${index}`.replace(/[^a-zA-Z0-9_-]/g, '_');
}

export function openAdjudication(game, league, input = {}) {
  if (game.adjudication) return { adjudication: game.adjudication, created: false };

  const marking = getMarkingState(game);
  if (marking.disputes.length === 0) {
    throw conflict('NO_DISPUTED_GROUP', '当前没有双方意见不一致的争议串，无需裁定场');
  }

  const now = input.now ?? new Date().toISOString();
  const deadlineSeconds = Number(input.deadlineSeconds ?? league.deadlineSeconds ?? 86400);
  if (!Number.isFinite(deadlineSeconds) || deadlineSeconds < 0) {
    throw badRequest('BAD_DEADLINE', 'deadlineSeconds 必须是非负秒数');
  }
  const panelSize = Number(input.panelSize ?? league.panelSize ?? 3);
  const eligible = eligibleReferees(league, game);
  if (eligible.length < league.requiredRemoveVotes) {
    throw conflict('NOT_ENOUGH_INDEPENDENT_REFEREEES', '无回避且可值班的裁判不足两名，可暂挂或调整值班', {
      eligible: eligible.map((r) => r.id),
    });
  }

  const cases = marking.disputes.map((dispute, index) => {
    const panel = eligible.slice(0, panelSize).map((r) => r.id);
    return {
      id: input.caseId || makeCaseId(game, index + 1),
      gameId: game.id,
      groupKey: dispute.key,
      group: dispute.group,
      boardSignature: game.stop.signature,
      status: 'open',
      deadlineAt: new Date(new Date(now).getTime() + deadlineSeconds * 1000).toISOString(),
      requiredRemoveVotes: league.requiredRemoveVotes,
      panelSize,
      panelRefereeIds: panel,
      releasedRefereeIds: [],
      votes: [],
      audit: [],
      events: [
        {
          at: now,
          type: 'case_opened',
          eligibleRefereeIds: eligible.map((r) => r.id),
          initialPanelRefereeIds: panel,
          deadlineSeconds,
        },
      ],
      resolution: { state: 'pending', note: '等待无回避值班裁判意见' },
    };
  });

  game.adjudication = {
    openedAt: now,
    deadlineAt: cases[0]?.deadlineAt,
    cases,
  };
  game.status = 'adjudicating';
  game.phase = 'sealed_ruling';
  game.updatedAt = now;
  return { adjudication: game.adjudication, created: true };
}

function activeVoteFromReferee(kase, refereeId) {
  return kase.votes.find((v) => v.refereeId === refereeId && v.status === 'received') || null;
}

export function syncCasePanel(kase, league, game, now = new Date().toISOString()) {
  const changes = [];
  if (kase.resolution?.state && kase.resolution.state !== 'pending') return changes;

  for (const refereeId of [...kase.panelRefereeIds]) {
    const referee = league.referees.find((r) => r.id === refereeId);
    const recusals = isRecused(league, refereeId, game);
    const vote = activeVoteFromReferee(kase, refereeId);
    const eligibleNow = Boolean(referee?.onDuty) && recusals.length === 0;

    if (!eligibleNow && !vote) {
      kase.panelRefereeIds = kase.panelRefereeIds.filter((id) => id !== refereeId);
      if (!kase.releasedRefereeIds.includes(refereeId)) kase.releasedRefereeIds.push(refereeId);
      const event = {
        at: now,
        type: 'panel_released_due_to_duty_or_recusal',
        refereeId,
        onDuty: referee?.onDuty ?? false,
        recusalReasons: recusals.map((r) => r.reason),
      };
      kase.events.push(event);
      changes.push(event);
    }
  }

  const eligible = eligibleReferees(league, game);
  for (const referee of eligible) {
    if (kase.panelRefereeIds.length >= kase.panelSize) break;
    if (kase.panelRefereeIds.includes(referee.id)) continue;
    if (kase.releasedRefereeIds.includes(referee.id)) continue;
    kase.panelRefereeIds.push(referee.id);
    const event = { at: now, type: 'panel_replaced', refereeId: referee.id, refereeName: referee.name };
    kase.events.push(event);
    changes.push(event);
  }

  kase.panelShortage = kase.panelRefereeIds.length < kase.requiredRemoveVotes;
  return changes;
}

export function submitRefereeVote(game, league, caseId, input = {}) {
  const kase = game.adjudication?.cases?.find((c) => c.id === caseId);
  if (!kase) throw notFound('CASE_NOT_FOUND', `找不到裁定案：${caseId}`);

  const now = input.now ?? new Date().toISOString();
  const refereeId = input.refereeId;
  const referee = league.referees.find((r) => r.id === refereeId);
  if (!referee) throw notFound('REFEREE_NOT_FOUND', `没有具名裁判：${refereeId}`);

  const decision = input.decision === 'remove' || input.decision === 'keep' ? input.decision : null;
  if (!decision) throw badRequest('BAD_DECISION', 'decision 必须是 remove（同意移去）或 keep（按原样保留）');

  syncCasePanel(kase, league, game, now);

  const recusals = isRecused(league, refereeId, game);
  if (recusals.length > 0) {
    const audit = { at: now, type: 'opinion_rejected', refereeId, decision, reason: 'recused', detail: recusals };
    kase.audit.push(audit);
    game.updatedAt = now;
    return {
      statusCode: 409,
      body: { error: { code: 'REFEREE_RECUSED', message: '该裁判在开赛回避名单内，意见不生效', detail: recusals } },
      audit,
    };
  }

  if (!referee.onDuty) {
    const audit = { at: now, type: 'opinion_rejected', refereeId, decision, reason: 'off_duty_at_submission' };
    kase.audit.push(audit);
    game.updatedAt = now;
    return {
      statusCode: 409,
      body: { error: { code: 'REFEREE_OFF_DUTY', message: '该裁判提交时不在可值班名单，意见不生效' } },
      audit,
    };
  }

  if (new Date(now).getTime() > new Date(kase.deadlineAt).getTime()) {
    const vote = {
      id: `late-${kase.votes.length + 1}-${fingerprint({ refereeId, now }).slice(0, 8)}`,
      caseId: kase.id,
      gameId: game.id,
      groupKey: kase.groupKey,
      boardSignature: kase.boardSignature,
      refereeId,
      refereeName: referee.name,
      decision,
      reason: input.reason || '',
      clientOpinionId: input.clientOpinionId || input.oldOpinionId || null,
      at: now,
      requestId: input.requestId || null,
      status: 'late',
      effective: false,
      eligibility: { onDutyAtSubmission: true, recused: false, beforeDeadline: false },
    };
    kase.votes.push(vote);
    kase.audit.push({ at: now, type: 'opinion_rejected', refereeId, voteId: vote.id, reason: 'expired' });
    game.updatedAt = now;
    return {
      statusCode: 409,
      body: { error: { code: 'OPINION_EXPIRED', message: '已超过裁定期限，迟到旧意见仅留档，不生效' }, vote },
      vote,
    };
  }

  if (kase.resolution?.state && kase.resolution.state !== 'pending') {
    const audit = { at: now, type: 'opinion_rejected', refereeId, decision, reason: `case_${kase.resolution.state}` };
    kase.audit.push(audit);
    game.updatedAt = now;
    return {
      statusCode: 409,
      body: { error: { code: 'CASE_ALREADY_RESOLVED', message: '裁定案已结束，新意见不改变结果', resolution: kase.resolution } },
      audit,
    };
  }

  if (!kase.panelRefereeIds.includes(refereeId)) {
    const audit = { at: now, type: 'opinion_rejected', refereeId, decision, reason: 'not_selected_panel' };
    kase.audit.push(audit);
    game.updatedAt = now;
    return {
      statusCode: 409,
      body: { error: { code: 'REFEREE_NOT_SELECTED', message: '该裁判未被选入当前裁定小组，意见留档但暂不计数' } },
      audit,
    };
  }

  const previous = activeVoteFromReferee(kase, refereeId);
  if (previous) previous.status = 'superseded';

  const vote = {
    id: `vote-${kase.votes.length + 1}-${fingerprint({ refereeId, decision, now }).slice(0, 8)}`,
    caseId: kase.id,
    gameId: game.id,
    groupKey: kase.groupKey,
    boardSignature: kase.boardSignature,
    refereeId,
    refereeName: referee.name,
    decision,
    reason: input.reason || '',
    clientOpinionId: input.clientOpinionId || input.oldOpinionId || null,
    at: now,
    requestId: input.requestId || null,
    status: 'received',
    effective: null,
    eligibility: {
      onDutyAtSubmission: true,
      recused: false,
      beforeDeadline: new Date(now).getTime() <= new Date(kase.deadlineAt).getTime(),
      inPanelAtSubmission: true,
    },
  };
  kase.votes.push(vote);
  game.updatedAt = now;

  const evaluation = evaluateCase(kase, league, game, { now });
  return { statusCode: 200, body: { vote, evaluation }, vote, evaluation };
}

export function evaluateCase(kase, league, game, input = {}) {
  const now = input.now ?? new Date().toISOString();
  syncCasePanel(kase, league, game, now);

  if (kase.resolution?.state && kase.resolution.state !== 'pending') return kase;

  for (const vote of kase.votes) {
    if (vote.status !== 'received') {
      vote.effective = false;
      vote.effectiveReason = `vote_${vote.status}`;
      continue;
    }
    const referee = league.referees.find((r) => r.id === vote.refereeId);
    const recusals = isRecused(league, vote.refereeId, game);
    const beforeDeadline = new Date(vote.at).getTime() <= new Date(kase.deadlineAt).getTime();
    const sameBoard = vote.boardSignature === kase.boardSignature;
    const sameGroup = vote.groupKey === kase.groupKey;
    const eligibleAtSubmission = vote.eligibility?.onDutyAtSubmission !== false;
    const effective = Boolean(
      referee &&
        recusals.length === 0 &&
        beforeDeadline &&
        sameBoard &&
        sameGroup &&
        eligibleAtSubmission,
    );
    vote.effective = effective;
    vote.effectiveReason = effective
      ? 'independent_on_duty_timely_same_group_board'
      : [
          recusals.length ? 'recused' : null,
          !beforeDeadline ? 'expired' : null,
          !sameBoard ? 'stale_board' : null,
          !sameGroup ? 'wrong_group' : null,
          !eligibleAtSubmission ? 'off_duty_at_submission' : null,
        ]
          .filter(Boolean)
          .join(',') || 'ineffective';
  }

  const effectiveVotes = kase.votes.filter((v) => v.effective && v.status === 'received');
  const removeVotes = effectiveVotes.filter((v) => v.decision === 'remove').sort((a, b) => a.at.localeCompare(b.at));
  const keepVotes = effectiveVotes.filter((v) => v.decision === 'keep').sort((a, b) => a.at.localeCompare(b.at));
  const deadlinePassed = new Date(now).getTime() >= new Date(kase.deadlineAt).getTime();
  const allCurrentPanelVoted =
    kase.panelRefereeIds.length >= kase.requiredRemoveVotes &&
    kase.panelRefereeIds.every((refereeId) => effectiveVotes.some((vote) => vote.refereeId === refereeId));

  if (removeVotes.length >= kase.requiredRemoveVotes) {
    const pair = removeVotes.slice(0, kase.requiredRemoveVotes);
    kase.status = 'resolved_removed';
    kase.resolution = {
      state: 'removed',
      resolvedAt: now,
      effectiveVoteIds: pair.map((v) => v.id),
      additionalEffectiveRemoveVoteIds: removeVotes.slice(kase.requiredRemoveVotes).map((v) => v.id),
      effectiveKeepVoteIds: keepVotes.map((v) => v.id),
      note: `两名无回避值班裁判 ${pair.map((v) => v.refereeName).join('、')} 同意移去该争议串`,
    };
    return kase;
  }

  if (deadlinePassed || allCurrentPanelVoted) {
    const split = removeVotes.length > 0 && keepVotes.length > 0;
    kase.status = 'resolved_left';
    kase.resolution = {
      state: 'left',
      resolvedAt: now,
      deadlineAt: kase.deadlineAt,
      decidedBeforeDeadline: !deadlinePassed && allCurrentPanelVoted,
      effectiveVoteIds: effectiveVotes.map((v) => v.id),
      effectiveRemoveVoteIds: removeVotes.map((v) => v.id),
      effectiveKeepVoteIds: keepVotes.map((v) => v.id),
      split,
      insufficientQuorum: effectiveVotes.length < kase.requiredRemoveVotes,
      note: split
        ? '裁判意见分裂且无两份无回避 remove 意见，按原样留盘并注明分歧'
        : allCurrentPanelVoted && !deadlinePassed
          ? '裁定小组已全部投票，但无两份无回避裁判同意移去，按原样留盘并注明'
          : '裁定期满仍无两名无回避裁判同意移去，按原样留盘并注明',
    };
    return kase;
  }

  kase.status = 'open';
  kase.resolution = {
    state: 'pending',
    effectiveVoteIds: effectiveVotes.map((v) => v.id),
    effectiveRemoveVoteIds: removeVotes.map((v) => v.id),
    effectiveKeepVoteIds: keepVotes.map((v) => v.id),
    deadlineAt: kase.deadlineAt,
    note: `尚待足够裁定；当前 ${removeVotes.length} 份 remove、${keepVotes.length} 份 keep 生效`,
  };
  return kase;
}

export function evaluateAdjudication(game, league, input = {}) {
  if (!game.adjudication) throw conflict('NO_ADJUDICATION', '本局尚未开封盘裁定场');
  for (const kase of game.adjudication.cases) evaluateCase(kase, league, game, input);
  game.updatedAt = input.now ?? new Date().toISOString();
  return game.adjudication;
}

export function leagueStandings(results) {
  const table = new Map();
  const ensure = (player) => {
    if (!table.has(player)) {
      table.set(player, { player, played: 0, wins: 0, draws: 0, losses: 0, leaguePoints: 0, scoreFor: 0, scoreAgainst: 0 });
    }
    return table.get(player);
  };

  for (const result of results) {
    if (!result.ranked) continue;
    const black = result.players?.black || result.black || 'black-client';
    const white = result.players?.white || result.white || 'white-client';
    const b = ensure(black);
    const w = ensure(white);
    b.played += 1;
    w.played += 1;
    b.scoreFor += result.score.total.black;
    b.scoreAgainst += result.score.total.white;
    w.scoreFor += result.score.total.white;
    w.scoreAgainst += result.score.total.black;

    if (result.winner === 'draw') {
      b.draws += 1;
      w.draws += 1;
      b.leaguePoints += 1;
      w.leaguePoints += 1;
    } else if (result.winner === 'black') {
      b.wins += 1;
      w.losses += 1;
      b.leaguePoints += 2;
    } else {
      w.wins += 1;
      b.losses += 1;
      w.leaguePoints += 2;
    }
  }

  return [...table.values()]
    .map((row) => ({ ...row, scoreDifference: row.scoreFor - row.scoreAgainst }))
    .sort((a, b) => b.leaguePoints - a.leaguePoints || b.scoreDifference - a.scoreDifference || a.player.localeCompare(b.player));
}

// assertColor re-export keeps future request validation close to this module.
export { assertColor };
