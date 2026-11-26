/**
 * 虚构管段拓扑与源头组合相容性规则。
 * 这里不连接真实管道，所有连通、隔离、加压和施工均为服务内模拟。
 */

export const MAX_SOURCES = 4;
export const MAX_INDEPENDENT_SOURCES = 2;

/**
 * 沿输入的有向连通关系判断源头是否能到达检查点。
 * isolatedSegmentIds 中的管段边会被临时隔开。
 */
export function sourceReachesPoint(
  topology,
  sourceId,
  pointId,
  isolatedSegmentIds = [],
) {
  if (!topology || !pointId) return false;
  const blocked = new Set(isolatedSegmentIds);
  const adjacency = new Map();

  for (const edge of topology.edges ?? []) {
    if (edge.segmentId && blocked.has(edge.segmentId)) continue;
    if (!adjacency.has(edge.from)) adjacency.set(edge.from, []);
    adjacency.get(edge.from).push(edge.to);
  }

  const queue = [sourceId];
  const seen = new Set([sourceId]);
  while (queue.length) {
    const node = queue.shift();
    if (node === pointId) return true;
    for (const next of adjacency.get(node) ?? []) {
      if (seen.has(next)) continue;
      seen.add(next);
      queue.push(next);
    }
  }
  return false;
}

/** 候选源头至多四处；现场模型允许同时存在至多两处独立源头。 */
export function enumerateSourceCombinations(
  sourceIds,
  maxIndependent = MAX_INDEPENDENT_SOURCES,
) {
  const ids = [...sourceIds];
  const combinations = [[]];
  for (const id of ids) {
    const length = combinations.length;
    for (let i = 0; i < length; i += 1) {
      const combo = combinations[i];
      if (combo.length < maxIndependent) combinations.push([...combo, id]);
    }
  }
  return combinations;
}

function observationIsDeterministic(evidence, reading) {
  return (
    reading?.deterministic === true ||
    evidence?.controlled === true ||
    evidence?.kind === 'pressure_test' ||
    evidence?.kind === 'isolation_water_test'
  );
}

/**
 * 依据检查/试水证据筛选仍相容的源头组合。
 *
 * - 湿读数：在当时隔离状态下，组合中至少一个源头必须仍能到达该点。
 * - 受控干读数：在当时隔离状态下，不允许组合中的源头仍能到达该点。
 * - 普通回访的干读数不用于排除间歇源；B 带故障也可能暂干。
 * - guaranteed pressureTest 表达“故障必湿、完好必干”的模拟加压试验。
 */
export function evaluateCompatibleCombinations(topology, evidenceList = []) {
  const sourceIds = (topology?.sources ?? []).map((source) => source.id);
  const activeEvidence = evidenceList.filter((item) => item && !item.retracted);

  return enumerateSourceCombinations(sourceIds).filter((combination) => {
    for (const evidence of activeEvidence) {
      const isolatedSegmentIds =
        evidence.isolatedSegmentIds ??
        (evidence.isolateSegmentId ? [evidence.isolateSegmentId] : []);

      const pressureTest = evidence.pressureTest;
      if (pressureTest?.guaranteed && pressureTest.sourceId) {
        const includesSource = combination.includes(pressureTest.sourceId);
        if (pressureTest.result === 'dry' && includesSource) return false;
        if (pressureTest.result === 'wet' && !includesSource) return false;
      }

      for (const reading of evidence.readings ?? []) {
        if (reading.wet === undefined || reading.wet === null) continue;
        const reachable = combination.some((sourceId) =>
          sourceReachesPoint(
            topology,
            sourceId,
            reading.pointId,
            isolatedSegmentIds,
          ),
        );

        if (reading.wet && !reachable) return false;

        if (
          !reading.wet &&
          observationIsDeterministic(evidence, reading) &&
          reachable
        ) {
          return false;
        }
      }
    }
    return true;
  });
}

export function summarizeCompatibility(topology, evidenceList = []) {
  const combinations = evaluateCompatibleCombinations(topology, evidenceList);
  const compatibleSourceIds = [
    ...new Set(combinations.flatMap((combo) => combo)),
  ].sort();
  const allSourceIds = (topology?.sources ?? []).map((source) => source.id);
  const excludedSourceIds = allSourceIds.filter(
    (id) => !compatibleSourceIds.includes(id),
  );
  return {
    combinations,
    compatibleSourceIds,
    excludedSourceIds,
    maxSources: MAX_SOURCES,
    maxIndependentSources: MAX_INDEPENDENT_SOURCES,
  };
}
