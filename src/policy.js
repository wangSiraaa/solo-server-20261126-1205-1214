import { sourceReachesPoint } from './topology.js';

function toTime(value) {
  return new Date(value).getTime();
}

export function windowsOverlap(windowsA = [], windowsB = []) {
  return windowsA.some((a) =>
    windowsB.some((b) => {
      const aStart = toTime(a.start);
      const aEnd = toTime(a.end);
      const bStart = toTime(b.start);
      const bEnd = toTime(b.end);
      return aStart <= bEnd && bStart <= aEnd;
    }),
  );
}

function sourcesForTicket(state, ticket) {
  const sourceIds = state.topology.sources.map((source) => source.id);
  return sourceIds.filter((sourceId) =>
    (ticket.pointIds ?? []).some((pointId) =>
      sourceReachesPoint(state.topology, sourceId, pointId, []),
    ),
  );
}

function isOpen(ticket) {
  return ticket.status !== 'closed';
}

/**
 * 管家制度：许可时段重叠且源头/管段相关的相邻户，优先合并一趟检查。
 * 已在共案中的单子不重复推荐合并。
 */
export function recommendJointCase(state) {
  const candidates = state.tickets.filter(
    (ticket) =>
      isOpen(ticket) &&
      !ticket.activeCaseId &&
      ticket.entryPermission?.granted,
  );

  for (let i = 0; i < candidates.length; i += 1) {
    for (let j = i + 1; j < candidates.length; j += 1) {
      const a = candidates[i];
      const b = candidates[j];
      const overlap = windowsOverlap(
        a.entryPermission.windows,
        b.entryPermission.windows,
      );
      if (!overlap) continue;

      const sourcesA = sourcesForTicket(state, a);
      const sourcesB = sourcesForTicket(state, b);
      const sharedSources = sourcesA.filter((id) => sourcesB.includes(id));
      const adjacent =
        a.adjacentTo.includes(b.id) || b.adjacentTo.includes(a.id);
      if (!adjacent && sharedSources.length === 0) continue;

      return {
        mode: 'joint_case',
        ticketIds: [a.id, b.id],
        reason:
          sharedSources.length > 0
            ? `入户时段重叠，且候选源头 ${sharedSources.join('、')} 可到达两户现象点`
            : '入户时段重叠且两户位置相邻，可合并一趟检查',
        suggestedInspectionOrder: [
          ...(a.pointIds ?? []),
          ...(b.pointIds ?? []),
        ],
      };
    }
  }
  return null;
}

/**
 * 若邻单已有确认、公开、可转用且正好覆盖未排除源头的结论，
 * 借结论比再派一趟检查更值得。
 */
export function recommendPublicBorrow(state, targetTicketId = null) {
  const targets = targetTicketId
    ? state.tickets.filter((ticket) => ticket.id === targetTicketId)
    : state.tickets.filter(isOpen);

  for (const target of targets) {
    const alreadyBorrowed = new Set(
      state.borrows
        .filter((borrow) => borrow.targetTicketId === target.id)
        .map((borrow) => borrow.evidenceId),
    );

    for (const evidence of state.inspections) {
      if (evidence.retracted || alreadyBorrowed.has(evidence.id)) continue;
      if (evidence.access?.scope === 'private' || !evidence.transferable) {
        continue;
      }
      if (!evidence.confirmed) continue;
      // 同一共案内本来就共享证据，不需要再走“转借邻单”。
      if (
        evidence.caseId &&
        target.activeCaseId &&
        evidence.caseId === target.activeCaseId
      ) {
        continue;
      }

      const sourceId = evidence.pressureTest?.sourceId ?? null;
      const reachableSourceIds = sourcesForTicket(state, target);
      if (sourceId && !reachableSourceIds.includes(sourceId)) continue;

      const adjacent =
        target.adjacentTo.includes(evidence.ticketId) ||
        evidence.reusableForTicketIds?.includes(target.id);
      const sameSegment =
        evidence.segmentId &&
        (target.pointIds ?? []).some((pointId) => {
          const point = state.topology.points.find((p) => p.id === pointId);
          return point?.segmentId === evidence.segmentId;
        });
      if (!adjacent && !sameSegment) continue;

      return {
        mode: 'borrow_public_conclusion',
        targetTicketId: target.id,
        evidenceId: evidence.id,
        reason: `可借用已确认公区结论：${evidence.conclusion ?? evidence.id}`,
      };
    }
  }
  return null;
}

export function butlerRecommendation(state) {
  return (
    recommendJointCase(state) ??
    recommendPublicBorrow(state) ?? {
      mode: 'separate_visit',
      reason: '没有重叠的已许可时段，也没有可直接转借的相关公区结论；按分户委托安排。',
    }
  );
}
