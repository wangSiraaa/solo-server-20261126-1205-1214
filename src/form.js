export const FORM_ID = 'night-school-registration';
export const POLICY = 'pinned-form-version';

const clone = (value) => structuredClone(value);

function question(key, title, extra = {}) {
  return {
    key,
    title,
    type: 'text',
    required: false,
    ...extra,
  };
}

/**
 * The initial published form. Every draft stores the exact formVersion it was
 * created with, so publishing v2 never rewrites the meaning of an old draft.
 */
export function buildFormV1() {
  return {
    formId: FORM_ID,
    version: 1,
    status: 'published',
    policy: POLICY,
    title: '夜校实作课报名表（第一版）',
    publishedAt: '2026-11-26T09:00:00.000Z',
    terms: {
      code: 'night-lab-terms-v1',
      title: '第一版实作台与结伴条款',
      body: '结伴报名仅可使用班级公布且当前同时为空的可结伴台号；递交成功后台位即固定入班，重复递交不产生新席位。',
    },
    questions: [
      question('guestName', '报名客人姓名', {
        type: 'text',
        required: true,
        meaningKey: 'person.primary.name',
      }),
      question('contact', '联系方式', {
        type: 'text',
        required: true,
        meaningKey: 'person.primary.contact',
      }),
      question('attendanceMode', '报名方式', {
        type: 'choice',
        required: true,
        meaningKey: 'registration.mode',
        options: [
          { value: 'solo', label: '独自报一门课' },
          { value: 'pair', label: '与一位同伴报相邻的两个操作台' },
        ],
      }),
      question('visitedLab', '来过实作室', {
        type: 'boolean',
        required: false,
        meaningKey: 'labVisit.anyPriorVisit.v1',
        description: '旧题：截至填表时，是否曾到过本校实作室（不论参加导览还是实际上机）。',
      }),
      question('generalConsent', '我同意夜校按本版本报名表载明的课程条款处理报名与台位', {
        type: 'consent',
        required: true,
        mustBeTrue: true,
        meaningKey: 'consent.generalTerms.v1',
      }),
    ],
    sections: [
      {
        key: 'pairPractice',
        title: '结伴实作',
        description: '仅当报名方式选择“与一位同伴结伴”时显示。',
        showWhen: {
          all: [{ question: 'attendanceMode', equals: 'pair' }],
        },
        questions: [
          question('partnerName', '同伴姓名', {
            type: 'text',
            required: true,
            meaningKey: 'person.companion.name',
          }),
          question('partnerContact', '同伴联系方式或邀请接收地址', {
            type: 'text',
            required: true,
            meaningKey: 'person.companion.contact',
            targetRole: 'companion',
          }),
          question('pairWorkPlan', '结伴实作内容与分工', {
            type: 'textarea',
            required: true,
            meaningKey: 'pair.practicePlan',
          }),
          question('pairConsent', '我同意与该同伴结伴，并在递交成功后固定使用一组可结伴台位', {
            type: 'consent',
            required: true,
            mustBeTrue: true,
            meaningKey: 'consent.pairSeats.v1',
          }),
        ],
      },
    ],
  };
}

/**
 * Admin action for the acceptance story: split the old broad “来过实作室”
 * question into two narrower stable-meaning questions.
 */
export function publishSplitVisitForm(activeForm) {
  if (!activeForm || activeForm.formId !== FORM_ID) {
    throw new Error('A valid active night-school form is required');
  }
  if (activeForm.version >= 2 && activeForm.splitFrom?.sourceQuestionKey === 'visitedLab') {
    return clone(activeForm);
  }

  const next = clone(activeForm);
  next.version = activeForm.version + 1;
  next.publishedAt = new Date().toISOString();
  next.title = `夜校实作课报名表（第 ${next.version} 版：实作室经历拆分）`;
  next.splitFrom = {
    sourceQuestionKey: 'visitedLab',
    strategy: 'split-into-two',
    publishedAt: next.publishedAt,
    note: '旧题含义较宽；新表拆分为导览经历与上台操作经历。旧草稿仍固定使用旧版本。',
  };

  const index = next.questions.findIndex((q) => q.key === 'visitedLab');
  const replacement = [
    question('visitedOrientation', '参加过实作室入门导览', {
      type: 'boolean',
      required: false,
      meaningKey: 'labVisit.orientationOnly.v2',
      description: '是否参加过入门导览或安全说明。',
    }),
    question('operatedStationBefore', '曾在实作台独立操作', {
      type: 'boolean',
      required: false,
      meaningKey: 'labVisit.handsOnStation.v2',
      description: '是否曾在操作台实际完成练习。',
    }),
  ];

  if (index >= 0) {
    next.questions.splice(index, 1, ...replacement);
  } else {
    next.questions.splice(3, 0, ...replacement);
  }
  return next;
}

export function findQuestion(form, key) {
  const top = form.questions.find((q) => q.key === key);
  if (top) return { question: top, section: null };
  for (const section of form.sections ?? []) {
    const q = section.questions.find((item) => item.key === key);
    if (q) return { question: q, section };
  }
  return null;
}

export function listAllQuestions(form) {
  const result = form.questions.map((q) => ({ question: q, section: null }));
  for (const section of form.sections ?? []) {
    for (const q of section.questions) result.push({ question: q, section });
  }
  return result;
}

function criterionMatches(criterion, answers) {
  const actual = answers?.[criterion.question];
  if (Object.hasOwn(criterion, 'equals') || Object.hasOwn(criterion, 'eq')) {
    const expected = criterion.equals ?? criterion.eq;
    return actual === expected;
  }
  if (criterion.in) return Array.isArray(criterion.in) && criterion.in.includes(actual);
  if (criterion.notEquals !== undefined) return actual !== criterion.notEquals;
  return Boolean(actual);
}

export function evaluateShowWhen(showWhen, answers) {
  if (!showWhen) return true;
  if (showWhen.all) return showWhen.all.every((c) => criterionMatches(c, answers));
  if (showWhen.any) return showWhen.any.some((c) => criterionMatches(c, answers));
  return criterionMatches(showWhen, answers);
}

export function isSectionVisible(section, answers) {
  return evaluateShowWhen(section.showWhen, answers);
}

export function showWhenTriggerKeys(showWhen) {
  if (!showWhen) return [];
  if (showWhen.question) return [showWhen.question];
  const groups = [...(showWhen.all ?? []), ...(showWhen.any ?? [])];
  return groups.map((c) => c.question).filter(Boolean);
}

export function isBlankAnswer(value) {
  if (value === undefined || value === null) return true;
  if (typeof value === 'string') return value.trim() === '';
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

export function normalizeAnswerValue(q, value) {
  if (value === undefined || value === null) return value;
  if (q.type === 'boolean' || q.type === 'consent') {
    if (typeof value === 'boolean') return value;
    if (value === 'true' || value === 'on' || value === '1') return true;
    if (value === 'false' || value === 'off' || value === '0' || value === '') return false;
  }
  if (q.type === 'text' || q.type === 'textarea') {
    return typeof value === 'string' ? value.trim() : value;
  }
  return value;
}

export function validateQuestionAnswer(q, value) {
  if (isBlankAnswer(value)) {
    return q.required ? '此题为必填项' : null;
  }
  if ((q.type === 'boolean' || q.type === 'consent') && typeof value !== 'boolean') {
    return '必须是是/否值';
  }
  if (q.type === 'choice' && Array.isArray(q.options)) {
    if (!q.options.some((option) => option.value === value)) {
      return '选项不在该表单版本中';
    }
  }
  if (q.type === 'consent' && q.mustBeTrue !== false && value !== true) {
    return '必须明确同意';
  }
  return null;
}

/**
 * Effective answers are not merely all stored raw values. A conditional field
 * is effective only when its section is visible in the current pinned version
 * and the value was written during the section's current reveal generation.
 * Thus an old answer from a previous visible/hidden cycle cannot silently be
 * counted as the new answer after the guest goes back and changes a trigger.
 */
export function computeEffectiveAnswers(draft, form) {
  const raw = draft.rawAnswers ?? {};
  const meta = draft.answerMeta ?? {};
  const epochs = draft.sectionEpochs ?? {};
  const effective = {};
  const stale = [];

  for (const q of form.questions) {
    if (Object.hasOwn(raw, q.key) && !isBlankAnswer(raw[q.key])) {
      effective[q.key] = clone(raw[q.key]);
    }
  }

  for (const section of form.sections ?? []) {
    const visible = isSectionVisible(section, raw);
    const currentEpoch = epochs[section.key] ?? 0;
    for (const q of section.questions) {
      const hasValue = Object.hasOwn(raw, q.key) && !isBlankAnswer(raw[q.key]);
      if (!hasValue) continue;

      const answerEpoch = meta[q.key]?.epoch;
      if (visible && answerEpoch === currentEpoch && currentEpoch > 0) {
        effective[q.key] = clone(raw[q.key]);
      } else {
        stale.push({
          key: q.key,
          sectionKey: section.key,
          reason: !visible ? 'section_hidden' : 'previous_reveal_requires_new_answer',
        });
      }
    }
  }

  return { effective, stale };
}

export function validateDraftAnswers(draft, form) {
  const { effective, stale } = computeEffectiveAnswers(draft, form);
  const errors = [];

  for (const { question: q, section } of listAllQuestions(form)) {
    if (section && !isSectionVisible(section, draft.rawAnswers ?? {})) continue;
    const message = validateQuestionAnswer(q, effective[q.key]);
    if (message) {
      errors.push({ key: q.key, sectionKey: section?.key ?? null, message });
    }
  }

  return {
    valid: errors.length === 0,
    errors,
    effectiveAnswers: effective,
    staleAnswers: stale,
    mode: effective.attendanceMode ?? null,
  };
}
