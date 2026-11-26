import { createInitialState } from './store.js';

export const SCENARIO_TIMES = {
  entry: '2026-11-27T01:10:00Z',
  surveyJia: '2026-11-27T01:20:00Z',
  surveyYi: '2026-11-27T01:40:00Z',
  pressureTest: '2026-11-27T02:00:00Z',
  repairA: '2026-11-27T03:10:00Z',
  jiaRevisit: '2026-11-28T02:30:00Z',
  yiRevisit: '2026-11-28T06:30:00Z',
  nextDay: '2026-11-29T01:00:00Z',
};

export function leakTopology() {
  return {
    sources: [
      {
        id: 'A',
        name: 'A 源：甲乙同源漏点',
        intermittent: false,
        branchSegmentId: 'SEG_A_SUPPLY',
      },
      {
        id: 'B',
        name: 'B 源：乙可能受影响的间歇源',
        intermittent: true,
        branchSegmentId: 'SEG_B_BRANCH',
      },
    ],
    segments: [
      { id: 'SEG_A_SUPPLY', fictional: true, scope: 'common' },
      { id: 'SEG_JIA_BRANCH', fictional: true, scope: 'private_indoor' },
      { id: 'SEG_YI_BRANCH', fictional: true, scope: 'private_indoor' },
      { id: 'SEG_B_BRANCH', fictional: true, scope: 'public_branch' },
      { id: 'SEG_BING_DRAIN', fictional: true, scope: 'private_indoor' },
    ],
    points: [
      { id: 'CP_JIA', name: '甲户滴水/积水检查点', segmentId: 'SEG_JIA_BRANCH', ticketId: 'T1' },
      { id: 'CP_YI', name: '乙户天花板返潮检查点', segmentId: 'SEG_YI_BRANCH', ticketId: 'T2' },
      { id: 'CP_B_TEST', name: 'B 支路模拟加压指定观察点', segmentId: 'SEG_B_BRANCH' },
      { id: 'CP_BING', name: '丙户临时处置观察点', segmentId: 'SEG_BING_DRAIN', ticketId: 'T3' },
    ],
    edges: [
      { from: 'A', to: 'J_A_COMMON', segmentId: 'SEG_A_SUPPLY' },
      { from: 'J_A_COMMON', to: 'CP_JIA', segmentId: 'SEG_JIA_BRANCH' },
      { from: 'J_A_COMMON', to: 'CP_YI', segmentId: 'SEG_YI_BRANCH' },
      { from: 'B', to: 'CP_YI', segmentId: 'SEG_B_BRANCH' },
      { from: 'B', to: 'CP_B_TEST', segmentId: 'SEG_B_BRANCH' },
      { from: 'J_A_COMMON', to: 'CP_BING', segmentId: 'SEG_BING_DRAIN' },
    ],
  };
}

export function seedBingTemporaryHousehold(service, options = {}) {
  const at = options.at ?? '2026-11-28T01:00:00Z';
  service.createTicket({
    id: 'T3',
    residentId: 'U_BING',
    residentName: '丙',
    unit: '202',
    location: '楼下相邻户 / 临时导水点',
    symptom: '柜体旁短时渗水，已接受临时处置',
    adjacentTo: ['T2'],
    pointIds: ['CP_BING'],
    entryWindows: [
      { id: 'EW_BING', start: '2026-11-27T05:00:00Z', end: '2026-11-27T07:00:00Z' },
    ],
    contractKind: 'temporary_dry_once',
    treatmentType: 'temporary',
  });
  service.registerRepair({
    id: 'REP_BING_TEMP',
    ticketId: 'T3',
    sourceIds: [],
    treatmentType: 'temporary',
    note: '只做临时封堵/导水，合同仅要求一次干回访',
    at,
  });
  if (options.dryRevisit !== false) {
    service.recordRevisit({
      revisitId: 'RV_BING_DRY_ONCE',
      ticketId: 'T3',
      at: options.revisitAt ?? '2026-11-28T03:30:00Z',
      result: 'dry',
      kind: 'ordinary',
    });
  }
  return service.state.tickets.find((ticket) => ticket.id === 'T3');
}

/**
 * 构造题述基准及变体。
 *
 * variant/options:
 * - baseline：B 加压干读数 + 两户约定时段干回访，两户均可结案。
 * - withoutBExclusion：不录入/撤下排除 B 的加压证据。
 * - missingYiRevisit：保留 B 排除，但乙在约定时段尚无入户回访。
 * - includeBing：加办丙户的一次干回访临时处置。
 */
export function seedScenario(service, variant = 'baseline', options = {}) {
  const config = {
    includeBExclusion:
      options.includeBExclusion ?? variant !== 'withoutBExclusion',
    includeYiRevisit:
      options.includeYiRevisit ?? variant !== 'missingYiRevisit',
    includeBing:
      options.includeBing ?? variant === 'bingTemporary' ?? false,
    retractBExclusion: options.retractBExclusion ?? false,
  };

  service.reset(createInitialState(service.clock()));
  service.setTopology(leakTopology());

  service.createTicket({
    id: 'T1',
    residentId: 'U_JIA',
    residentName: '甲（楼上）',
    unit: '501',
    location: '楼上：阳台/立管附近滴水',
    symptom: '楼上说滴水',
    pointIds: ['CP_JIA'],
    adjacentTo: ['T2'],
    entryWindows: [
      { id: 'EW_JIA', start: '2026-11-27T01:00:00Z', end: '2026-11-27T03:00:00Z' },
    ],
    revisitWindows: [
      { id: 'RW_JIA', start: '2026-11-28T02:00:00Z', end: '2026-11-28T04:00:00Z' },
    ],
    contract: {
      kind: 'standard',
      revisitWindowId: 'RW_JIA',
      requiredRevisit: 'agreed_window_dry',
      requireRepairCoveringSymptom: true,
      excludeAllCompatibleSources: false,
    },
  });

  service.createTicket({
    id: 'T2',
    residentId: 'U_YI',
    residentName: '乙（楼下）',
    unit: '401',
    location: '楼下：客厅天花板返潮',
    symptom: '楼下说天花板返潮',
    pointIds: ['CP_YI'],
    adjacentTo: ['T1'],
    entryWindows: [
      { id: 'EW_YI', start: '2026-11-27T02:00:00Z', end: '2026-11-27T04:00:00Z' },
    ],
    revisitWindows: [
      { id: 'RW_YI', start: '2026-11-28T06:00:00Z', end: '2026-11-28T08:00:00Z' },
    ],
    contract: {
      kind: 'strict_source_exclusion',
      revisitWindowId: 'RW_YI',
      requiredRevisit: 'agreed_window_dry',
      requireRepairCoveringSymptom: true,
      excludeAllCompatibleSources: true,
    },
  });

  service.createCase({
    id: 'CASE_JIA_YI',
    ticketIds: ['T1', 'T2'],
    at: '2026-11-27T00:40:00Z',
  });
  service.planCase({
    caseId: 'CASE_JIA_YI',
    inspectionOrder: ['CP_JIA', 'CP_YI', 'CP_B_TEST'],
    note: '先甲户滴水点，再乙户天花板，最后隔 A 做 B 支路加压',
    at: '2026-11-27T00:50:00Z',
  });
  service.dispatchCase({
    caseId: 'CASE_JIA_YI',
    actorId: 'WANG_CREW',
    at: SCENARIO_TIMES.entry,
  });

  service.recordInspection({
    id: 'INS_JIA_WET',
    caseId: 'CASE_JIA_YI',
    ticketId: 'T1',
    surveyorId: 'SURVEYOR_LIN',
    at: SCENARIO_TIMES.surveyJia,
    checkpointId: 'CP_JIA',
    segmentId: 'SEG_JIA_BRANCH',
    kind: 'survey_observation',
    readings: [{ pointId: 'CP_JIA', wet: true }],
    scope: 'private',
    conclusion: '甲检查点当前湿',
  });

  service.recordInspection({
    id: 'INS_YI_WET',
    caseId: 'CASE_JIA_YI',
    ticketId: 'T2',
    surveyorId: 'SURVEYOR_LIN',
    at: SCENARIO_TIMES.surveyYi,
    checkpointId: 'CP_YI',
    segmentId: 'SEG_YI_BRANCH',
    kind: 'survey_observation',
    readings: [{ pointId: 'CP_YI', wet: true }],
    scope: 'private',
    conclusion: '乙检查点当前湿',
  });

  if (config.includeBExclusion) {
    service.recordInspection({
      id: 'INS_B_PRESSURE_DRY',
      caseId: 'CASE_JIA_YI',
      ticketId: 'T2',
      surveyorId: 'SURVEYOR_LIN',
      at: SCENARIO_TIMES.pressureTest,
      checkpointId: 'CP_B_TEST',
      segmentId: 'SEG_B_BRANCH',
      kind: 'pressure_test',
      controlled: true,
      isolatedSegmentIds: ['SEG_A_SUPPLY'],
      coveredPointIds: ['CP_B_TEST'],
      readings: [{ pointId: 'CP_B_TEST', wet: false, deterministic: true }],
      pressureTest: {
        guaranteed: true,
        isolatedSourceId: 'A',
        sourceId: 'B',
        branchSegmentId: 'SEG_B_BRANCH',
        testCondition: '仅向 B 所在支路施加模拟压力',
        result: 'dry',
        rule: 'B 有故障则指定点必湿；B 完好则必干',
      },
      scope: 'public',
      confirmed: true,
      conclusion: 'B_EXCLUDED：模拟加压干读数排除 B 故障',
      reusableForTicketIds: ['T1', 'T2'],
    });
  }

  service.registerRepair({
    id: 'REP_A_ONLY',
    caseId: 'CASE_JIA_YI',
    sourceIds: ['A'],
    actual: true,
    treatmentType: 'permanent',
    note: '同源漏水，只实际修 A；B 未修，靠加压干读数排除',
    at: SCENARIO_TIMES.repairA,
  });

  if (config.includeYiRevisit) {
    service.recordJointRevisit({
      caseId: 'CASE_JIA_YI',
      jointRevisitId: 'JR_DAY2_BOTH_DRY',
      actorId: 'WANG_CREW',
      results: [
        {
          revisitId: 'RV_JIA_DRY',
          ticketId: 'T1',
          at: SCENARIO_TIMES.jiaRevisit,
          result: 'dry',
        },
        {
          revisitId: 'RV_YI_DRY',
          ticketId: 'T2',
          at: SCENARIO_TIMES.yiRevisit,
          result: 'dry',
        },
      ],
    });
  } else {
    service.recordRevisit({
      revisitId: 'RV_JIA_DRY',
      ticketId: 'T1',
      caseId: 'CASE_JIA_YI',
      at: SCENARIO_TIMES.jiaRevisit,
      result: 'dry',
      actorId: 'WANG_CREW',
    });
    service.dispatchClosureNotices({ at: SCENARIO_TIMES.yiRevisit });
  }

  if (config.retractBExclusion) {
    service.retractInspection(
      'INS_B_PRESSURE_DRY',
      '只撤下排除 B 的观察',
      '2026-11-28T08:00:00Z',
    );
  }

  if (config.includeBing) {
    seedBingTemporaryHousehold(service);
    service.dispatchClosureNotices({ at: '2026-11-28T04:00:00Z' });
  }

  service.dispatchClosureNotices({ at: '2026-11-28T08:30:00Z' });

  return {
    variant,
    config,
    ticketIds: service.state.tickets.map((ticket) => ticket.id),
    notificationCount: service.state.notifications.length,
  };
}
