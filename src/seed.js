import {
  addVerification,
  createExample,
  createQuestion,
  openAttempt,
  publishEdition,
  splitSense,
  submitAttempt,
} from './domain.js';

const OLD_NOW = '2026-11-24T09:00:00+08:00';
const OPEN_NOW = '2026-11-25T20:30:00+08:00';

export function createSeedDb({ scenario = 'pre-split' } = {}) {
  const db = {
    meta: {
      service: 'fangyan-she-workbook',
      scenario,
      classroomModel: 'versioned-edition',
      dictionaryVersion: 1,
      maxActiveSenses: 5,
      counters: {},
      seededAt: new Date().toISOString(),
    },
    actors: {
      ED1: { id: 'ED1', role: 'editor', name: '林编者' },
      ED2: { id: 'ED2', role: 'editor', name: '周编者' },
      TEA1: { id: 'TEA1', role: 'teacher', name: '何老师' },
      STU_A: { id: 'STU_A', role: 'student', name: '学生甲（拆义前开卷）' },
      STU_B: { id: 'STU_B', role: 'student', name: '学生乙（新义版）' },
      STU_OLD_DONE: { id: 'STU_OLD_DONE', role: 'student', name: '拆义前已交卷学生' },
    },
    headwords: {
      HW_DADENG: {
        id: 'HW_DADENG',
        text: '打灯',
        pinyin: 'dǎ dēng',
        dialect: '方言示例',
        senseIds: ['G'],
        formerTexts: [],
        createdAt: OLD_NOW,
        updatedAt: OLD_NOW,
      },
    },
    senses: {
      G: {
        id: 'G',
        headwordId: 'HW_DADENG',
        number: 1,
        code: 'G',
        label: '旧笼统义',
        definition: '用灯发出光亮；旧注未分照明与用灯光示意，两类用法都装在这一义中。',
        semanticKey: 'general-light-or-signal',
        semanticKeys: ['illumination', 'signal'],
        status: 'active',
        active: true,
        lineage: {},
        createdAt: OLD_NOW,
        updatedAt: OLD_NOW,
      },
    },
    tags: {
      TAG_ILLUM: {
        id: 'TAG_ILLUM',
        kind: 'discrimination-label',
        label: '照明',
        semanticKey: 'illumination',
      },
      TAG_CUE: {
        id: 'TAG_CUE',
        kind: 'discrimination-label',
        label: '提示',
        semanticKey: 'signal',
      },
    },
    usages: {},
    examples: {},
    questions: {},
    papers: {},
    editions: {},
    attempts: {},
    decisions: [],
    events: [],
  };

  createExample(db, {
    id: 'E1',
    headwordId: 'HW_DADENG',
    text: '天黑后的山路上，他打灯照清台阶。',
    markedText: '打灯',
    senseIds: ['G'],
    editorIds: ['ED1', 'ED2'],
    source: 'textbook-initial-certification',
    now: OLD_NOW,
  });
  createExample(db, {
    id: 'E2',
    headwordId: 'HW_DADENG',
    text: '夜航船靠近时，他在滩头打灯：灯光照亮水道，也一明一暗地提示来船。',
    markedText: '打灯',
    senseIds: ['G'],
    editorIds: ['ED1', 'ED2'],
    source: 'textbook-initial-certification',
    now: OLD_NOW,
  });

  createQuestion(db, {
    id: 'Q_OLD',
    teacherId: 'TEA1',
    exampleId: 'E1',
    usageSenseId: 'G',
    requiredTagId: 'TAG_ILLUM',
    prompt: '原有练习：选择句中“打灯”的意思。上下文只问照明义，并要求例句本身唯一体现它。',
    selectionMode: 'single',
    requireUniqueExample: true,
    options: [
      {
        id: 'OPT_QOLD_G',
        senseId: 'G',
        definition: '用灯发出光亮，也可指用灯光示意。（拆义前旧释义）',
        correct: true,
      },
      { id: 'OPT_QOLD_COVER', definition: '用东西遮盖、遮蔽。', correct: false },
      { id: 'OPT_QOLD_CALL', definition: '张嘴呼唤、喊叫。', correct: false },
    ],
    now: OLD_NOW,
  });

  publishEdition(db, {
    editionId: 'EDITION_OLD',
    teacherId: 'TEA1',
    questionIds: ['Q_OLD'],
    title: '方言练习册·拆义前旧版',
    note: '学生甲在 G 拆义前看到并打开此版。',
    now: OLD_NOW,
  });
  openAttempt(db, {
    attemptId: 'ATT_A',
    studentId: 'STU_A',
    editionId: 'EDITION_OLD',
    now: OPEN_NOW,
  });

  // 另有一份拆义前已经交卷的记录，供启动后立即检查“按所见释义复核”。
  openAttempt(db, {
    attemptId: 'ATT_OLD_DONE',
    studentId: 'STU_OLD_DONE',
    editionId: 'EDITION_OLD',
    now: OPEN_NOW,
  });
  submitAttempt(db, 'ATT_OLD_DONE', {
    clientSubmissionId: 'seed-before-split',
    answers: [{ questionId: 'Q_OLD', optionId: 'OPT_QOLD_G' }],
  });

  if (scenario === 'post-split') {
    advanceToPostSplit(db);
  }

  return db;
}

export function advanceToPostSplit(db) {
  splitSense(db, 'G', {
    editorId: 'ED1',
    reason: '词典修订：旧 G 兼含照明与灯语提示，拆为 L、S。',
    senses: [
      {
        id: 'L',
        number: 2,
        code: 'L',
        label: '照明',
        semanticKey: 'illumination',
        definition: '点亮或举着灯，使某处有光、便于看清。',
      },
      {
        id: 'S',
        number: 3,
        code: 'S',
        label: '提示',
        semanticKey: 'signal',
        definition: '用灯光、明灭或灯语向人示意、发出提示。',
      },
    ],
  });

  // 例句一：两名编者分别核为 L，形成新的两人共识。
  addVerification(db, 'E1', { editorId: 'ED1', senseIds: ['L'] });
  addVerification(db, 'E1', { editorId: 'ED2', senseIds: ['L'] });

  // 例句二：两名编者均核为 L、S，即确实兼两义。
  addVerification(db, 'E2', { editorId: 'ED1', senseIds: ['L', 'S'] });
  addVerification(db, 'E2', { editorId: 'ED2', senseIds: ['L', 'S'] });

  createQuestion(db, {
    id: 'Q_NEW',
    teacherId: 'TEA1',
    exampleId: 'E1',
    usageSenseId: 'L',
    requiredTagId: 'TAG_ILLUM',
    prompt: '新练习：例句本身必须唯一支持“照明”辨义。',
    selectionMode: 'single',
    requireUniqueExample: true,
    options: [
      // 故意把“提示”放在 A，把“照明”放在 B：旧 A 与新 A 不是同一释义。
      { id: 'OPT_QNEW_S', senseId: 'S', definition: '用灯光明灭或灯语示意、提示。', correct: false },
      { id: 'OPT_QNEW_L', senseId: 'L', definition: '点亮或举着灯，使某处有光；照明。', correct: true },
      { id: 'OPT_QNEW_COVER', definition: '用东西遮盖、遮蔽。', correct: false },
    ],
  });

  publishEdition(db, {
    editionId: 'EDITION_NEW',
    teacherId: 'TEA1',
    questionIds: ['Q_NEW'],
    title: '方言练习册·新义版',
    note: '按 L/S 新义封题；旧版 EDITION_OLD 仍留给 ATT_A。',
  });
  openAttempt(db, {
    attemptId: 'ATT_B',
    studentId: 'STU_B',
    editionId: 'EDITION_NEW',
  });

  return db;
}
