import { randomUUID } from 'node:crypto';

export class DomainError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'DomainError';
    this.code = code;
    this.details = details;
  }
}

export const nowIso = () => new Date().toISOString();

/* -------------------------------------------------------------------------- */
/* 约定与任务：委托人/招募方在受理前登记，接单时快照，结账不得事后改价。              */
/* -------------------------------------------------------------------------- */

export const AGREEMENTS = {
  'agree-self-discovery-3': {
    id: 'agree-self-discovery-3',
    version: 1,
    title: '三点自助发现计件约定',
    currency: 'points',
    unitPrice: 3,
    acceptedBeforeWork: true,
    terms: [
      '必须在签发给该受理单的原型版本上逐步点击并真实改变状态。',
      '规定动作的动词和册子目标必须逐项一致；只到达外观相同的结束页不计完成。',
      '主持人代点、回放代点轨迹、设计师捷径均不算自助发现。',
      '若该试用员此前已实际看过直接说破本题的提示，或已实际掌握同一独立发现键，本发现奖不予结算。',
      '同一受理单/同一次完成无论由多少设备重传，只支付一次。',
    ],
  },
  'agree-self-discovery-2': {
    id: 'agree-self-discovery-2',
    version: 1,
    title: '两点自助发现计件约定',
    currency: 'points',
    unitPrice: 2,
    acceptedBeforeWork: true,
    terms: [
      '按任务书在签发原型上完成精确动作。',
      '提示泄题、代点回放和重复结算规则与三点约定相同。',
    ],
  },
  'agree-known-gesture-1': {
    id: 'agree-known-gesture-1',
    version: 1,
    title: '已知手势复测计件约定（一点）',
    currency: 'points',
    unitPrice: 1,
    acceptedBeforeWork: true,
    terms: [
      '该动作已由试用员此前实际掌握，本单购买的是复测执行而非首次发现。',
      '仍须在签发原型上产生真实、目标一致的状态变化。',
      '同一受理单只结算一次。',
    ],
  },
};

export const HINTS = {
  'hint-blue-answer': {
    id: 'hint-blue-answer',
    title: '直接报出蓝册两步答案',
    body: '答案：先在蓝册上点“收藏”，仍在蓝册上点“取消收藏”。',
    answerRevealing: true,
    reveals: [
      'gesture:enter-library',
      'gesture:favorite-toggle',
      'binding:collect-blue',
      'binding:uncollect-blue',
      'route:blue-roundtrip',
    ],
  },
  'hint-red-answer': {
    id: 'hint-red-answer',
    title: '直接报出红册待借标记答案',
    body: '答案：在红册上点“标记待借”，再点“取消待借”。',
    answerRevealing: true,
    reveals: ['entry:loan-action', 'binding:flag-red', 'binding:unflag-red'],
  },
  'hint-safe-scroll': {
    id: 'hint-safe-scroll',
    title: '不泄题的操作提示',
    body: '如果一屏内容较长，可以上下滚动查看；按钮文字可能较短。',
    answerRevealing: false,
    reveals: [],
  },
};

export function builtInTasks() {
  return [
    {
      id: 'task-blue-roundtrip',
      version: 3,
      clientName: '青轴原型设计工作室',
      title: '从空收藏夹自行把蓝册加入后再移出',
      brief: '开始时收藏夹为空。请独立找到蓝册，收藏它，再取消收藏，使收藏夹恢复为空。',
      prototypeId: 'books-v1',
      agreementId: 'agree-self-discovery-3',
      mode: 'unmoderated',
      initialScreenId: 'favorites-empty',
      initialState: { favorites: [], loanFlags: [], flags: {} },
      steps: [
        {
          ordinal: 1,
          verb: 'collect',
          targetType: 'book',
          targetId: 'book-blue',
          label: '收藏蓝册',
          discoveryKeys: ['gesture:enter-library', 'binding:collect-blue'],
        },
        {
          ordinal: 2,
          verb: 'uncollect',
          targetType: 'book',
          targetId: 'book-blue',
          label: '在蓝册上取消收藏',
          discoveryKeys: ['binding:uncollect-blue'],
        },
      ],
      requiredDiscoveryKeys: [
        'gesture:enter-library',
        'gesture:favorite-toggle',
        'binding:collect-blue',
        'binding:uncollect-blue',
      ],
      teachesKeys: [
        'gesture:enter-library',
        'gesture:favorite-toggle',
        'binding:collect-blue',
        'binding:uncollect-blue',
        'route:blue-roundtrip',
      ],
      exclusionHintIds: ['hint-blue-answer'],
      allowedHintIds: ['hint-blue-answer', 'hint-safe-scroll'],
      finalState: { favorites: [] },
    },
    {
      id: 'task-red-loan-flag',
      version: 1,
      clientName: '青轴原型设计工作室',
      title: '给红册打“待借”标记后再取消',
      brief: '请找到红册的待借标记，执行标记和取消标记。该任务与收藏答案互不泄题。',
      prototypeId: 'books-v1',
      agreementId: 'agree-self-discovery-2',
      mode: 'unmoderated',
      initialScreenId: 'library',
      initialState: { favorites: [], loanFlags: [], flags: {} },
      steps: [
        {
          ordinal: 1,
          verb: 'flag-loan',
          targetType: 'book',
          targetId: 'book-red',
          label: '标记红册待借',
          discoveryKeys: ['entry:loan-action', 'binding:flag-red'],
        },
        {
          ordinal: 2,
          verb: 'unflag-loan',
          targetType: 'book',
          targetId: 'book-red',
          label: '取消红册待借标记',
          discoveryKeys: ['binding:unflag-red'],
        },
      ],
      requiredDiscoveryKeys: ['entry:loan-action', 'binding:flag-red', 'binding:unflag-red'],
      teachesKeys: ['entry:loan-action', 'binding:flag-red', 'binding:unflag-red'],
      exclusionHintIds: ['hint-red-answer'],
      allowedHintIds: ['hint-red-answer', 'hint-safe-scroll'],
      finalState: { loanFlags: [] },
    },
    {
      id: 'task-green-roundtrip-known-variant',
      version: 2,
      clientName: '青轴原型设计工作室',
      title: '绿册收藏往返（同一收藏手势的变体）',
      brief: '从空收藏夹开始，收藏绿册后再取消收藏。它与蓝册题共享收藏手势。',
      prototypeId: 'books-v1',
      agreementId: 'agree-self-discovery-3',
      knownGestureAgreementId: 'agree-known-gesture-1',
      mode: 'unmoderated',
      initialScreenId: 'favorites-empty',
      initialState: { favorites: [], loanFlags: [], flags: {} },
      steps: [
        { ordinal: 1, verb: 'collect', targetType: 'book', targetId: 'book-green', label: '收藏绿册' },
        { ordinal: 2, verb: 'uncollect', targetType: 'book', targetId: 'book-green', label: '在绿册上取消收藏' },
      ],
      requiredDiscoveryKeys: [
        'gesture:enter-library',
        'gesture:favorite-toggle',
        'binding:collect-green',
        'binding:uncollect-green',
      ],
      teachesKeys: ['binding:collect-green', 'binding:uncollect-green'],
      exclusionHintIds: ['hint-green-answer'],
      allowedHintIds: ['hint-safe-scroll'],
      finalState: { favorites: [] },
    },
  ];
}

/* -------------------------------------------------------------------------- */
/* 原型：服务端保存版本化状态机，终端只渲染当前屏、可点动作和任务状态。                 */
/* -------------------------------------------------------------------------- */

export const PROTOTYPES = {
  'books-v1': {
    id: 'books-v1',
    version: 1,
    name: '少数画面的书架原型',
    books: [
      { id: 'book-blue', name: '蓝册', swatch: '#2563eb' },
      { id: 'book-green', name: '绿册', swatch: '#16a34a' },
      { id: 'book-red', name: '红册', swatch: '#dc2626' },
      { id: 'book-ochre', name: '黄册', swatch: '#d97706' },
    ],
    screens: {
      'favorites-empty': {
        title: '我的收藏',
        body: '收藏夹还是空的。',
      },
      library: {
        title: '册子书架',
        body: '每本册子有各自的操作，同名按钮不会共享目标。',
      },
      favorites: {
        title: '我的收藏',
        body: '这里列出已收藏的册子。',
      },
      notice: {
        title: '工作室公告',
        body: '原型中的“确定”可能出现在不同画面，并对应不同动作编号。',
      },
      finish: {
        title: '操作完成',
        body: '结束页只代表一个画面；受理台仍会复核完整动作轨迹。',
        appearanceKey: 'plain-finish',
      },
      'finish-lookalike': {
        title: '操作完成',
        body: '结束页只代表一个画面；受理台仍会复核完整动作轨迹。',
        appearanceKey: 'plain-finish',
      },
    },
  },
};

function bookName(prototype, id) {
  return prototype.books.find((book) => book.id === id)?.name ?? id;
}

export function initialPrototypeState(prototypeId) {
  if (prototypeId === 'books-v1') {
    return { favorites: [], loanFlags: [], flags: {} };
  }
  throw new DomainError('UNKNOWN_PROTOTYPE', `未知原型：${prototypeId}`);
}

/** 返回当前画面真正可点的动作。动作身份是 actionId + targetId，而不是按钮文字。 */
export function availableActions(prototype, screenId, state) {
  const actions = [];
  const has = (list, id) => list.includes(id);

  if (screenId === 'favorites-empty') {
    actions.push({ actionId: 'go-library', label: '去书库', kind: 'navigation', to: 'library' });
  }

  if (screenId === 'library') {
    actions.push({ actionId: 'go-favorites', label: '查看收藏', kind: 'navigation', to: 'favorites' });
    actions.push({ actionId: 'open-notice', label: '公告', kind: 'navigation', to: 'notice' });

    for (const book of prototype.books) {
      const favored = has(state.favorites, book.id);
      actions.push(
        favored
          ? {
              actionId: `uncollect:${book.id}`,
              label: '取消收藏',
              kind: 'state-change',
              verb: 'uncollect',
              targetType: 'book',
              targetId: book.id,
              targetName: book.name,
            }
          : {
              actionId: `collect:${book.id}`,
              label: '收藏',
              kind: 'state-change',
              verb: 'collect',
              targetType: 'book',
              targetId: book.id,
              targetName: book.name,
            },
      );

      const flagged = has(state.loanFlags, book.id);
      if (book.id === 'book-red' || state.flags?.loanActionsDiscovered) {
        actions.push(
          flagged
            ? {
                actionId: `unflag-loan:${book.id}`,
                label: '取消待借',
                kind: 'state-change',
                verb: 'unflag-loan',
                targetType: 'book',
                targetId: book.id,
                targetName: book.name,
              }
            : {
                actionId: `flag-loan:${book.id}`,
                label: '标记待借',
                kind: 'state-change',
                verb: 'flag-loan',
                targetType: 'book',
                targetId: book.id,
                targetName: book.name,
              },
        );
      }
    }

    if (state.flags?.blueRoundtrip) {
      actions.push({
        actionId: 'open-genuine-finish',
        label: '确定',
        kind: 'navigation',
        to: 'finish',
        note: '蓝册两步状态均已发生后出现',
      });
    }
  }

  if (screenId === 'favorites') {
    actions.push({ actionId: 'go-library', label: '去书库', kind: 'navigation', to: 'library' });
    if (state.favorites.length === 0) {
      actions.push({ actionId: 'go-library-empty', label: '去书库加一本', kind: 'navigation', to: 'library' });
    }
    for (const bookId of state.favorites) {
      actions.push({
        actionId: `uncollect:${bookId}`,
        label: '取消收藏',
        kind: 'state-change',
        verb: 'uncollect',
        targetType: 'book',
        targetId: bookId,
        targetName: bookName(prototype, bookId),
      });
    }
  }

  if (screenId === 'notice') {
    actions.push({ actionId: 'ok-notice', label: '确定', kind: 'navigation', to: 'library' });
  }

  if (screenId === 'finish') {
    actions.push({ actionId: 'ok-finish', label: '确定', kind: 'acknowledge', to: 'finish' });
  }

  if (screenId === 'finish-lookalike') {
    actions.push({ actionId: 'ok-lookalike', label: '确定', kind: 'acknowledge', to: 'finish-lookalike' });
  }

  if (['favorites-empty', 'library', 'favorites'].includes(screenId)) {
    actions.push({
      actionId: 'shortcut-lookalike-finish',
      label: '设计师捷径：直达相同结束页',
      kind: 'designer-shortcut',
      to: 'finish-lookalike',
      designerShortcut: true,
    });
  }

  return actions;
}

/** 纯状态转移；线上点击和事后轨迹重放都走这里。 */
export function transition(prototype, state, screenId, input) {
  const action = availableActions(prototype, screenId, state).find(
    (candidate) => candidate.actionId === input.actionId,
  );

  if (!action) {
    throw new DomainError('ACTION_NOT_AVAILABLE', '该动作在当前画面/状态下不可点', {
      screenId,
      actionId: input.actionId,
    });
  }

  if (input.targetId && action.targetId && input.targetId !== action.targetId) {
    throw new DomainError('TARGET_BINDING_MISMATCH', '点击目标与签发动作绑定的目标不一致', {
      actionId: action.actionId,
      expectedTargetId: action.targetId,
      actualTargetId: input.targetId,
    });
  }

  const nextState = structuredClone(state);
  nextState.flags ??= {};

  const targetId = input.targetId ?? action.targetId ?? null;

  switch (action.verb) {
    case 'collect':
      if (!nextState.favorites.includes(targetId)) nextState.favorites.push(targetId);
      if (targetId === 'book-blue') nextState.flags.sawCollectBlue = true;
      break;
    case 'uncollect':
      nextState.favorites = nextState.favorites.filter((id) => id !== targetId);
      if (targetId === 'book-blue' && nextState.flags.sawCollectBlue) {
        nextState.flags.blueRoundtrip = true;
      }
      break;
    case 'flag-loan':
      if (!nextState.loanFlags.includes(targetId)) nextState.loanFlags.push(targetId);
      nextState.loanActionsDiscovered = true;
      break;
    case 'unflag-loan':
      nextState.loanFlags = nextState.loanFlags.filter((id) => id !== targetId);
      if (targetId === 'book-red') nextState.flags.redRoundtrip = true;
      break;
    default:
      break;
  }

  if (action.actionId === 'flag-loan:book-red' || action.actionId === 'unflag-loan:book-red') {
    nextState.loanActionsDiscovered = true;
  }

  const nextScreenId = action.to ?? screenId;
  const event = {
    eventId: input.eventId ?? randomUUID(),
    seq: input.seq ?? null,
    actionId: action.actionId,
    label: action.label,
    kind: action.kind,
    verb: action.verb ?? null,
    targetType: action.targetType ?? null,
    targetId,
    targetName: action.targetName ?? null,
    screenBefore: screenId,
    screenAfter: nextScreenId,
    designerShortcut: Boolean(action.designerShortcut),
    deviceId: input.deviceId ?? null,
    actorRole: input.actorRole ?? 'tester',
    provenance: input.provenance ?? 'live-click',
    at: input.at ?? nowIso(),
  };

  return { state: nextState, screenId: nextScreenId, event };
}

export function terminalView({ task, prototype, claim, session, eligibility }) {
  const screen = prototype.screens[session.screenId];
  const actions = availableActions(prototype, session.screenId, session.state);
  const matched = matchSteps(task, session.events);
  const doneCount = matched.filter((step) => step.matchedEvent).length;

  return {
    sessionId: session.id,
    claimId: claim.id,
    receiptNo: claim.receiptNo,
    testerId: claim.testerId,
    prototype: { id: prototype.id, version: prototype.version, name: prototype.name },
    currentScreenId: session.screenId,
    screen,
    actions,
    state: session.state,
    task: {
      id: task.id,
      title: task.title,
      brief: task.brief,
      status: doneCount === task.steps.length ? 'steps-complete' : 'in-progress',
      doneCount,
      totalCount: task.steps.length,
      steps: matched,
    },
    eligibility,
    hintNotice: eligibility?.excluded
      ? `发现奖已因此前实际记录失效：${eligibility.reason}`
      : '尚未发现泄题记录。',
  };
}

export function matchSteps(task, events) {
  let cursor = 0;
  return task.steps.map((step) => {
    let matchedEvent = null;
    while (cursor < events.length) {
      const event = events[cursor];
      cursor += 1;
      if (event.verb === step.verb && event.targetId === step.targetId) {
        matchedEvent = event;
        break;
      }
    }
    return { ...step, done: Boolean(matchedEvent), matchedEvent };
  });
}

/* -------------------------------------------------------------------------- */
/* 发包策略：互不泄题的发现键才放进同一个无主持任务包。                                */
/* -------------------------------------------------------------------------- */

export function planPackage(tasks, orderedTaskIds) {
  const selected = orderedTaskIds.map((id) => tasks.find((task) => task.id === id)).filter(Boolean);
  const conflicts = [];

  for (let i = 0; i < selected.length; i += 1) {
    const earlier = selected[i];
    const earlierTeaches = new Set(earlier.teachesKeys ?? earlier.requiredDiscoveryKeys ?? []);
    for (let j = i + 1; j < selected.length; j += 1) {
      const later = selected[j];
      const shared = (later.requiredDiscoveryKeys ?? []).filter((key) => earlierTeaches.has(key));
      if (shared.length) {
        conflicts.push({
          earlierTaskId: earlier.id,
          laterTaskId: later.id,
          sharedDiscoveryKeys: shared,
          decision: 'hold-later-discovery-award',
          alternatives: [
            '把后一项从同一人的无主持发现包中撤下，另邀未掌握该手势的试用员',
            `若双方事前已接受已知手势单价，可改发 ${later.knownGestureAgreementId ?? 'agree-known-gesture-1'}`,
          ],
        });
      }
    }
  }

  return {
    orderedTaskIds: selected.map((task) => task.id),
    safeForOneUnmoderatedPackage: conflicts.length === 0,
    conflicts,
    policy: '只把发现键集合互不泄露的任务封入同一无主持包；共享手势的变体不向同一人计首次发现。',
  };
}

/* -------------------------------------------------------------------------- */
/* 资格：按试用员身份读回其本人的提示暴露、带教和已实际掌握记录。                        */
/* -------------------------------------------------------------------------- */

export function assessEligibility(task, history, atIso = nowIso()) {
  const priorExposures = history.exposures ?? [];
  const priorTaught = history.taught ?? [];
  const knownKeys = new Set(history.knownKeys ?? []);
  const required = task.requiredDiscoveryKeys ?? [];

  const exposureMatches = priorExposures.filter((exposure) => {
    if (exposure.testerId !== history.testerId) return false;
    if (exposure.scope === 'current-claim-after-submit') return false;
    if (exposure.shownAt > atIso) return false;
    if ((task.exclusionHintIds ?? []).includes(exposure.hintId)) return true;
    return (exposure.reveals ?? []).some((key) => required.includes(key));
  });

  const taughtMatches = priorTaught.filter((record) =>
    (record.taughtKeys ?? []).some((key) => required.includes(key)),
  );

  const knownMatches = required.filter((key) => knownKeys.has(key));
  const excluded = exposureMatches.length > 0 || taughtMatches.length > 0 || knownMatches.length > 0;

  let reason = null;
  if (exposureMatches.length) reason = 'prior_exclusion_hint_actually_shown';
  else if (taughtMatches.length) reason = 'action_directly_taught';
  else if (knownMatches.length) reason = 'discovery_already_known_from_actual_prior_work';

  return {
    independent: !excluded,
    excluded,
    reason,
    exposureIds: exposureMatches.map((item) => item.id),
    sourceReceiptNos: exposureMatches.map((item) => item.sourceReceiptNo).filter(Boolean),
    taughtRecordIds: taughtMatches.map((item) => item.id),
    knownMatches,
    requiredDiscoveryKeys: required,
  };
}

/* -------------------------------------------------------------------------- */
/* 完成审核：服务端只信签发版本上逐步产生的有效轨迹，结束页外观不作为证据。              */
/* -------------------------------------------------------------------------- */

function deepEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function evaluateCompletion({ task, prototype, claim, session, eligibility, submittedAt = nowIso() }) {
  const checks = [];
  const addCheck = (id, ok, detail) => checks.push({ id, ok, detail });

  addCheck(
    'issued-prototype-version',
    claim.prototypeId === prototype.id && claim.prototypeVersion === prototype.version,
    `受理单签发 ${claim.prototypeId}@v${claim.prototypeVersion}；实际会话 ${prototype.id}@v${prototype.version}`,
  );

  addCheck(
    'session-belongs-to-claim',
    session.claimId === claim.id && session.testerId === claim.testerId,
    '会话、受理单与试用员身份一致',
  );

  const hostEvents = session.events.filter((event) => event.actorRole === 'host' || event.provenance === 'host-proxy');
  addCheck(
    'no-host-proxy-events',
    hostEvents.length === 0 && session.provenance !== 'host-proxy' && !session.sourceDemonstrationId,
    hostEvents.length
      ? `发现 ${hostEvents.length} 个主持人代点事件`
      : session.sourceDemonstrationId
        ? `轨迹回放自代点示范 ${session.sourceDemonstrationId}`
        : '全部事件来自本人受理会话',
  );

  // 从任务初始状态逐项重放，验证不是只报了一个结束页。
  let replayState = structuredClone(task.initialState ?? initialPrototypeState(prototype.id));
  let replayScreenId = task.initialScreenId ?? prototype.initialScreenId;
  const replayErrors = [];

  for (const event of session.events) {
    try {
      const result = transition(prototype, replayState, replayScreenId, {
        actionId: event.actionId,
        targetId: event.targetId,
        eventId: event.eventId,
        deviceId: event.deviceId,
        actorRole: event.actorRole,
      });
      if (event.screenBefore && event.screenBefore !== replayScreenId) {
        replayErrors.push({ seq: event.seq, code: 'SCREEN_SEQUENCE_MISMATCH' });
      }
      replayState = result.state;
      replayScreenId = result.screenId;
    } catch (error) {
      replayErrors.push({ seq: event.seq, code: error.code, message: error.message });
    }
  }

  addCheck(
    'every-click-took-effect',
    replayErrors.length === 0 && deepEqual(replayState, session.state),
    replayErrors.length
      ? `重放失败：${JSON.stringify(replayErrors)}`
      : `服务端重放 ${session.events.length} 个事件，最终状态一致`,
  );

  const targetTampering = (session.rejectedEvents ?? []).filter(
    (event) => event.code === 'TARGET_BINDING_MISMATCH',
  );
  addCheck(
    'no-target-retargeting',
    targetTampering.length === 0,
    targetTampering.length
      ? `有 ${targetTampering.length} 次把动作改绑到其他册子的请求`
      : '未发现中途改目标',
  );

  const shortcutEvents = session.events.filter((event) => event.designerShortcut);
  addCheck(
    'no-designer-shortcut',
    shortcutEvents.length === 0,
    shortcutEvents.length ? `使用了设计师捷径：${shortcutEvents.map((e) => e.actionId).join(', ')}` : '未走设计师直达捷径',
  );

  const stepMatches = matchSteps(task, session.events);
  const missing = stepMatches.filter((step) => !step.matchedEvent);
  addCheck(
    'exact-ordered-verbs-and-targets',
    missing.length === 0,
    missing.length
      ? `缺少规定动作：${missing.map((step) => `${step.verb}@${step.targetId}`).join(' -> ')}`
      : task.steps.map((step) => `${step.verb}@${step.targetId}`).join(' -> '),
  );

  const finalStateOk = Object.entries(task.finalState ?? {}).every(([key, value]) =>
    deepEqual(session.state[key], value),
  );
  addCheck(
    'required-final-state',
    finalStateOk,
    `要求 ${JSON.stringify(task.finalState ?? {})}；实际 ${JSON.stringify({
      favorites: session.state.favorites,
      loanFlags: session.state.loanFlags,
    })}`,
  );

  addCheck(
    'appearance-is-not-proof',
    true,
    `最终画面 ${session.screenId}（外观键 ${prototype.screens[session.screenId]?.appearanceKey ?? 'standard'}）未被单独采信`,
  );

  const behaviorPassed = checks.every((check) => check.ok);

  // 提交后才拆封的泄题提示不能追溯打击此前已经完成的独立轨迹；
  // 提交前实际展示的排除提示则由 assessEligibility 计入。
  const eligibilityAtSubmit = eligibility;
  const discoveryPayable = behaviorPassed && eligibilityAtSubmit.independent;

  const payableAmount = discoveryPayable ? claim.agreementSnapshot.unitPrice : 0;

  return {
    behaviorPassed,
    discoveryPayable,
    payableAmount,
    currency: claim.agreementSnapshot.currency,
    eligibility: eligibilityAtSubmit,
    checks,
    summary: !behaviorPassed
      ? '轨迹未通过：相似结束页不能替代精确、生效的步骤。'
      : !eligibilityAtSubmit.independent
        ? `行为轨迹成立，但发现奖资格已排除：${eligibilityAtSubmit.reason}`
        : `自助轨迹与资格均成立，按事先约定可结算 ${payableAmount} ${claim.agreementSnapshot.currency}。`,
    submittedAt,
  };
}
