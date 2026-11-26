import {
  BLACK,
  WHITE,
  assertColor,
  allGroups,
  boardSignature,
  collectGroup,
  parseCoord,
  placeStone,
  scorePosition,
  validateSetup,
  opponent,
} from './go.mjs';
import { DomainError, badRequest, conflict } from './errors.mjs';
import { fingerprint } from './hash.mjs';

let gameCounter = 0;

export function createGame(input = {}) {
  const size = Number(input.size ?? 5);
  if (!Number.isInteger(size) || size < 2 || size > 19) throw badRequest('BAD_SIZE', '棋盘路数必须是 2..19');
  const mode = input.mode === 'practice' ? 'practice' : 'formal';
  const setupStones = input.setup ?? [];
  if (mode === 'formal' && Array.isArray(setupStones) && setupStones.length > 0) {
    throw badRequest('FORMAL_NO_IMPORT', '正式局不能导入起势盘；结算只能以服务受理的着手为依据');
  }

  const initialBoard = validateSetup(size, setupStones);
  const now = input.now ?? new Date().toISOString();
  gameCounter += 1;
  const id = input.id || `game-${Date.now().toString(36)}-${gameCounter}`;
  const players = {
    [BLACK]: input.players?.[BLACK] || input.black || 'black-client',
    [WHITE]: input.players?.[WHITE] || input.white || 'white-client',
  };
  const creationSpecHash = fingerprint({
    id,
    leagueId: input.leagueId || 'league-5x5',
    size,
    mode,
    players,
    setup: setupStones,
  });

  return {
    id,
    leagueId: input.leagueId || 'league-5x5',
    size,
    mode,
    creationSpecHash,
    status: 'active',
    phase: setupStones.length ? 'imported_setup' : 'even_game',
    players,
    turn: input.turn === WHITE ? WHITE : BLACK,
    initialBoard,
    board: initialBoard.slice(),
    moves: [],
    consecutivePasses: 0,
    stop: null,
    stopHistory: [],
    opinions: [],
    continuationSessions: [],
    adjudication: null,
    suspension: null,
    result: null,
    createdAt: now,
    updatedAt: now,
  };
}

function assertMutable(game) {
  if (game.suspension?.suspended) {
    throw conflict('GAME_SUSPENDED', '对局已因失联等原因暂挂，需恢复后再操作', game.suspension);
  }
}

function beginScoring(game, moveSeq, now) {
  const stop = {
    moveSeq,
    signature: boardSignature(game.board, game.size),
    at: now,
    board: game.board.slice(),
  };
  game.status = 'scoring';
  game.phase = 'terminal_marking';
  game.stop = stop;
  game.stopHistory.push({ ...stop, board: stop.board.slice() });
}

function groupSummaryAt(game, coordInput) {
  const coord = parseCoord(coordInput, game.size);
  if (!game.board[coord.index]) {
    throw badRequest('CLICK_EMPTY', `所点位置 (${coord.x},${coord.y}) 没有棋子，不能代表整串`);
  }
  const group = collectGroup(game.board, game.size, coord.index);
  return {
    key: group.key,
    color: group.color,
    points: group.points,
    liberties: group.liberties,
    libertyCount: group.libertyIndices.length,
  };
}

function detectLineage(game, oldGroups, moveSeq) {
  const lineage = [];
  for (const oldGroup of oldGroups) {
    let current = null;
    for (const point of oldGroup.points) {
      const coord = parseCoord(point, game.size);
      const index = coord.index;
      if (game.board[index] === oldGroup.color) {
        current = collectGroup(game.board, game.size, index);
        break;
      }
    }

    if (!current) {
      lineage.push({ oldKey: oldGroup.key, newKey: null, state: 'captured_or_removed', invalidatedBySeq: moveSeq });
    } else if (current.key === oldGroup.key) {
      lineage.push({ oldKey: oldGroup.key, newKey: current.key, state: 'unchanged', invalidatedBySeq: moveSeq });
    } else {
      const oldSet = new Set(oldGroup.points.map((p) => `${p.x},${p.y}`));
      const contained = current.points.every((p) => oldSet.has(`${p.x},${p.y}`))
        ? 'old_string_subset_of_new_string'
        : 'changed';
      lineage.push({
        oldKey: oldGroup.key,
        newKey: current.key,
        state: 'merged_or_changed',
        relation: contained,
        newLibertyCount: current.libertyIndices.length,
        newPoints: current.points,
        invalidatedBySeq: moveSeq,
      });
    }
  }
  return lineage;
}

function invalidateOpinionsAfterMove(game, move, now) {
  if (game.phase !== 'continuation' || !game.stop) return;
  const newSignature = boardSignature(game.board, game.size);
  if (newSignature === game.stop.signature) return;

  const invalidatedIds = [];
  const lineage = [];
  for (const opinion of game.opinions) {
    if (opinion.status !== 'active' || opinion.boardSignature !== game.stop.signature) continue;
    opinion.status = 'superseded';
    opinion.invalidatedBySeq = move.seq;
    opinion.invalidatedAt = now;
    invalidatedIds.push(opinion.id);
    lineage.push(...detectLineage(game, opinion.groups, move.seq));
  }

  move.invalidatedOpinionIds = invalidatedIds;
  move.invalidatesOldOpinions = invalidatedIds.length > 0;
  move.groupLineage = lineage;

  const session = game.continuationSessions.at(-1);
  if (session && session.firstChangingMoveSeq === null) {
    session.firstChangingMoveSeq = move.seq;
    session.invalidatedOpinionIds = invalidatedIds;
  }
}

export function applyHand(game, input = {}) {
  assertMutable(game);
  if (game.status !== 'active') {
    throw conflict('GAME_NOT_ACTIVE', `当前状态 ${game.status}，不能直接落子或停手`, {
      status: game.status,
      stop: game.stop && { moveSeq: game.stop.moveSeq, signature: game.stop.signature },
    });
  }

  const color = assertColor(input.color);
  if (game.turn !== color) {
    throw conflict('NOT_YOUR_TURN', `现在轮到 ${game.turn}，收到 ${color} 的着手`, { turn: game.turn });
  }

  const now = input.now ?? new Date().toISOString();
  const seq = game.moves.length + 1;
  const beforeSignature = boardSignature(game.board, game.size);

  if (input.type === 'pass' || input.pass === true) {
    const move = {
      seq,
      type: 'pass',
      color,
      at: now,
      beforeSignature,
      afterSignature: beforeSignature,
      boardAfter: game.board.slice(),
    };
    game.moves.push(move);
    game.consecutivePasses += 1;
    game.turn = opponent(color);
    game.updatedAt = now;

    if (game.consecutivePasses >= 2) beginScoring(game, seq, now);
    return move;
  }

  if (input.x === undefined || input.y === undefined) {
    throw badRequest('BAD_MOVE', '落子需要 {x, y}，或使用 pass 接口');
  }

  // The opponent's most recent accepted hand carries the position before it.
  const koSignature = game.moves.length ? game.moves.at(-1).beforeSignature : null;
  const result = placeStone(game.board, game.size, { x: input.x, y: input.y }, color, { koSignature });

  const move = {
    seq,
    type: 'place',
    color,
    x: input.x,
    y: input.y,
    at: now,
    captured: result.captured,
    beforeSignature,
    afterSignature: result.signature,
    boardAfter: result.board,
  };

  game.board = result.board;
  game.moves.push(move);
  game.consecutivePasses = 0;
  game.turn = opponent(color);
  game.updatedAt = now;

  invalidateOpinionsAfterMove(game, move, now);
  return move;
}

export function submitMarking(game, input = {}) {
  assertMutable(game);
  if (game.status !== 'scoring') {
    throw conflict('MARKING_NOT_OPEN', '只有连续两手停着后的停局盘面才能提交死活标记', { status: game.status });
  }
  const party = assertColor(input.as || input.party);
  if (!Array.isArray(input.dead)) throw badRequest('BAD_MARKING', 'dead 必须是所点棋子坐标数组；无死串可提交空数组');

  const groupMap = new Map();
  const clicks = [];
  for (const rawCoord of input.dead) {
    const summary = groupSummaryAt(game, rawCoord);
    clicks.push({ x: rawCoord.x, y: rawCoord.y, representedGroupKey: summary.key });
    groupMap.set(summary.key, summary);
  }

  const now = input.now ?? new Date().toISOString();
  const boardSignatureValue = game.stop.signature;
  for (const old of game.opinions) {
    if (old.party === party && old.boardSignature === boardSignatureValue && old.status === 'active') {
      old.status = 'superseded';
      old.supersededAt = now;
    }
  }

  const groups = [...groupMap.values()];
  const opinion = {
    id: `mark-${game.moves.length + 1}-${party}-${game.opinions.length + 1}`,
    kind: 'full_dead_list',
    party,
    boardSignature: boardSignatureValue,
    stopMoveSeq: game.stop.moveSeq,
    clicks,
    groupKeys: groups.map((g) => g.key),
    groups,
    status: 'active',
    at: now,
    requestId: input.requestId || null,
  };
  opinion.receiptHash = fingerprint({
    gameId: game.id,
    party,
    boardSignature: opinion.boardSignature,
    groupKeys: opinion.groupKeys,
    at: now,
  });
  game.opinions.push(opinion);
  game.updatedAt = now;

  return { submission: opinion, markingState: getMarkingState(game) };
}

export function getMarkingState(game) {
  if (!game.stop) return { open: false, complete: false, consensus: false, disputes: [] };
  const signature = game.stop.signature;
  const active = game.opinions.filter((o) => o.status === 'active' && o.boardSignature === signature);
  const blackSubmission = active.find((o) => o.party === BLACK) || null;
  const whiteSubmission = active.find((o) => o.party === WHITE) || null;
  const complete = Boolean(blackSubmission && whiteSubmission);

  const groupDetails = new Map();
  for (const submission of [blackSubmission, whiteSubmission]) {
    for (const group of submission?.groups || []) groupDetails.set(group.key, group);
  }

  const blackKeys = new Set(blackSubmission?.groupKeys || []);
  const whiteKeys = new Set(whiteSubmission?.groupKeys || []);
  const agreedDeadKeys = [...blackKeys].filter((key) => whiteKeys.has(key));
  const disputeKeys = [...new Set([...blackKeys, ...whiteKeys])].filter((key) => !(blackKeys.has(key) && whiteKeys.has(key)));

  const disputes = disputeKeys.map((key) => ({
    key,
    group: groupDetails.get(key) || null,
    blackOpinion: blackKeys.has(key) ? 'dead' : 'alive_by_omission',
    whiteOpinion: whiteKeys.has(key) ? 'dead' : 'alive_by_omission',
    status: 'disputed',
  }));

  return {
    open: game.status === 'scoring' || game.status === 'adjudicating',
    stoppedBoardSignature: signature,
    complete,
    consensus: complete && disputes.length === 0,
    awaiting: [!blackSubmission && BLACK, !whiteSubmission && WHITE].filter(Boolean),
    blackSubmissionId: blackSubmission?.id || null,
    whiteSubmissionId: whiteSubmission?.id || null,
    receipts: [blackSubmission?.receiptHash, whiteSubmission?.receiptHash].filter(Boolean),
    agreedDeadKeys,
    agreedDeadGroups: agreedDeadKeys.map((key) => groupDetails.get(key)).filter(Boolean),
    disputes,
  };
}

export function verifyEndorsement(game, input = {}) {
  const currentSignature = game.stop?.signature || boardSignature(game.board, game.size);
  const reasons = [];

  if (input.boardSignature && input.boardSignature !== currentSignature) {
    reasons.push('BOARD_SIGNATURE_MISMATCH');
  }

  if (input.groupKey) {
    const currentKeys = new Set(allGroups(game.board, game.size).map((group) => group.key));
    if (!currentKeys.has(input.groupKey)) reasons.push('GROUP_KEY_CHANGED_OR_MISSING');
  }

  if (input.receiptHash && !game.opinions.some((opinion) => opinion.receiptHash === input.receiptHash)) {
    reasons.push('RECEIPT_NOT_BOUND_TO_THIS_GAME');
  }

  return {
    applies: reasons.length === 0,
    reasons,
    currentBoardSignature: currentSignature,
    checked: {
      boardSignature: input.boardSignature || null,
      groupKey: input.groupKey || null,
      receiptHash: input.receiptHash || null,
    },
  };
}

export function openContinuation(game, input = {}) {
  assertMutable(game);
  if (game.status !== 'scoring') {
    throw conflict('CONTINUATION_NOT_AVAILABLE', '只有双停后的处置盘面可以开续弈场', { status: game.status });
  }
  if (game.adjudication) {
    throw conflict('ADJUDICATION_ALREADY_OPEN', '本局已开封盘裁定场，不能同时改开续弈场');
  }
  const now = input.now ?? new Date().toISOString();
  game.status = 'active';
  game.phase = 'continuation';
  game.consecutivePasses = 0;
  game.continuationSessions.push({
    openedAt: now,
    openedBy: input.by || 'club',
    note: input.note || '意见不合，开续弈场从争议盘面依次补下',
    fromSignature: game.stop.signature,
    fromStopMoveSeq: game.stop.moveSeq,
    firstChangingMoveSeq: null,
    invalidatedOpinionIds: [],
  });
  game.updatedAt = now;
  return game.continuationSessions.at(-1);
}

export function setSuspension(game, input = {}) {
  const suspended = Boolean(input.suspended);
  const now = input.now ?? new Date().toISOString();
  if (suspended) {
    if (game.status === 'settled') throw conflict('GAME_SETTLED', '已入榜对局不能暂挂');
    game.suspension = {
      suspended: true,
      at: now,
      reason: input.reason || '一方失联，暂挂等待恢复',
      pendingWith: input.pendingWith || null,
    };
  } else {
    game.suspension = null;
    game.updatedAt = now;
  }
  return game.suspension;
}

export function moveHistoryHash(game) {
  return fingerprint({
    gameId: game.id,
    size: game.size,
    mode: game.mode,
    initialBoard: game.initialBoard,
    moves: game.moves.map((m) => ({
      seq: m.seq,
      type: m.type,
      color: m.color,
      x: m.x ?? null,
      y: m.y ?? null,
      captured: m.captured || [],
      beforeSignature: m.beforeSignature,
      afterSignature: m.afterSignature,
    })),
  });
}

export function replayAt(game, step) {
  const max = game.moves.length;
  const target = step === undefined || step === null ? max : Number(step);
  if (!Number.isInteger(target) || target < 0 || target > max) {
    throw badRequest('BAD_STEP', `step 必须是 0..${max} 的整数`);
  }
  if (target === 0) {
    return { step: 0, totalSteps: max, board: game.initialBoard.slice(), lastMove: null };
  }
  const move = game.moves[target - 1];
  return { step: target, totalSteps: max, board: move.boardAfter.slice(), lastMove: publicMove(move) };
}

export function publicMove(move) {
  const { boardAfter, ...rest } = move;
  return rest;
}

export function buildSettlement(game, input = {}) {
  if (game.status === 'settled' && game.result) return game.result;
  assertMutable(game);

  if (!game.stop) throw conflict('NOT_TERMINAL', '尚未连续两手停着，不能结算');
  const marking = getMarkingState(game);
  if (!marking.complete) {
    throw conflict('AWAITING_PLAYER_OPINIONS', '双方尚未都提交停局盘面意见，不能入榜', {
      awaiting: marking.awaiting,
    });
  }

  const deadSources = new Map();
  for (const key of marking.agreedDeadKeys) deadSources.set(key, { source: 'common_agreement' });

  const adjudicationSummary = [];
  if (marking.disputes.length > 0) {
    if (!game.adjudication) {
      throw conflict('DISPUTES_UNRESOLVED', '存在死活分歧；需续弈后重新签认，或开封盘裁定场', {
        disputes: marking.disputes.map((d) => d.key),
      });
    }
    for (const dispute of marking.disputes) {
      const c = game.adjudication.cases.find(
        (item) => item.groupKey === dispute.key && item.boardSignature === game.stop.signature,
      );
      if (!c || !c.resolution || c.resolution.state === 'pending') {
        throw conflict('ADJUDICATION_PENDING', '争议串尚未形成可生效裁定', { groupKey: dispute.key, caseId: c?.id });
      }
      adjudicationSummary.push({
        caseId: c.id,
        groupKey: dispute.key,
        resolution: c.resolution.state,
        note: c.resolution.note,
        effectiveVoteIds: c.resolution.effectiveVoteIds || [],
      });
      if (c.resolution.state === 'removed') {
        deadSources.set(dispute.key, {
          source: 'adjudication_remove',
          caseId: c.id,
          effectiveVoteIds: c.resolution.effectiveVoteIds || [],
        });
      }
    }
  }

  const deadKeys = [...deadSources.keys()];
  const score = scorePosition(game.board, game.size, deadKeys);
  const now = input.now ?? new Date().toISOString();
  const result = {
    gameId: game.id,
    leagueId: game.leagueId,
    players: { ...game.players },
    ranked: game.mode === 'formal',
    mode: game.mode,
    settledAt: now,
    rule: 'four-neighbor strings/liberties; empty-region area scoring; no komi',
    acceptedMoveCount: game.moves.length,
    placementCount: game.moves.filter((m) => m.type === 'place').length,
    passCount: game.moves.filter((m) => m.type === 'pass').length,
    stopMoveSeq: game.stop.moveSeq,
    stoppedBoardSignature: game.stop.signature,
    finalBoardSignature: boardSignature(game.board, game.size),
    moveHistoryHash: moveHistoryHash(game),
    playerReceipts: marking.receipts,
    deadGroups: score.removedGroups.map((g) => ({ ...g, basis: deadSources.get(g.key) })),
    keptDisputedGroups: marking.disputes
      .filter((d) => !deadKeys.includes(d.key))
      .map((d) => ({ key: d.key, caseId: adjudicationSummary.find((a) => a.groupKey === d.key)?.caseId || null })),
    score: {
      stones: score.stones,
      territory: score.territory,
      total: score.totals,
      territories: score.territories,
      scoredBoardSignature: score.scoredBoardSignature,
    },
    winner: score.winner,
    leaguePoints: score.winner === 'draw' ? { black: 1, white: 1 } : score.winner === BLACK ? { black: 2, white: 0 } : { black: 0, white: 2 },
    settlementBasis: {
      blackMarkingId: marking.blackSubmissionId,
      whiteMarkingId: marking.whiteSubmissionId,
      agreedDeadKeys: marking.agreedDeadKeys,
      adjudication: adjudicationSummary,
    },
    requestId: input.requestId || null,
  };
  result.resultHash = fingerprint({ ...result, resultHash: undefined });
  return result;
}
