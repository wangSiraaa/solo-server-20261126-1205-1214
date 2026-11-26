import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import {
  deriveFaces,
  emptyPattern,
  matchActualGrid,
  mirrorHorizontal,
  normalizePattern,
  planCompleteBoxes,
  remnantSegments,
  transformPattern
} from './domain.js';

export class HttpError extends Error {
  constructor(status, code, message, details = undefined) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function clone(value) {
  return structuredClone(value);
}

export class Store {
  constructor(dataFile = resolve(process.cwd(), 'data/db.json')) {
    this.dataFile = dataFile;
    this.db = this.#defaultDb();
    this.chain = Promise.resolve();
  }

  #defaultDb() {
    return {
      schemaVersion: 1,
      revision: 0,
      counters: {},
      bars: [],
      cards: [],
      pieces: [],
      claims: [],
      orders: [],
      boxes: [],
      events: []
    };
  }

  async init() {
    try {
      const raw = await readFile(this.dataFile, 'utf8');
      this.db = JSON.parse(raw);
      for (const key of ['bars', 'cards', 'pieces', 'claims', 'orders', 'boxes', 'events']) {
        if (!Array.isArray(this.db[key])) this.db[key] = [];
      }
      this.db.counters ||= {};
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      this.db = this.#defaultDb();
      await this.persist();
    }
    return this;
  }

  nextCode(kind, prefix) {
    this.db.counters[kind] = (this.db.counters[kind] || 0) + 1;
    return `${prefix}-${String(this.db.counters[kind]).padStart(4, '0')}`;
  }

  async persist() {
    this.db.revision += 1;
    this.db.updatedAt = new Date().toISOString();
    await mkdir(dirname(this.dataFile), { recursive: true });
    const tmp = `${this.dataFile}.tmp`;
    await writeFile(tmp, JSON.stringify(this.db, null, 2), 'utf8');
    await rename(tmp, this.dataFile);
  }

  /** 所有变更串行化；并发请求不会同时领到同一片或同一段格。 */
  mutate(work) {
    const run = this.chain.then(async () => {
      const snapshot = clone(this.db);
      try {
        const result = await work(this);
        await this.persist();
        return result;
      } catch (error) {
        this.db = snapshot;
        throw error;
      }
    });
    // 不让一次失败打断后续队列。
    this.chain = run.then(
      () => undefined,
      () => undefined
    );
    return run;
  }

  async replaceDb(nextDb) {
    return this.mutate(() => {
      this.db = nextDb;
    });
  }

  findBar(code) {
    const bar = this.db.bars.find((b) => b.id === code || b.code === code);
    if (!bar) throw new HttpError(404, 'BAR_NOT_FOUND', `找不到条坯 ${code}`);
    return bar;
  }

  findCard(code) {
    const card = this.db.cards.find((c) => c.id === code || c.code === code);
    if (!card) throw new HttpError(404, 'CARD_NOT_FOUND', `找不到样卡 ${code}`);
    return card;
  }

  findPiece(code) {
    const piece = this.db.pieces.find((p) => p.id === code || p.code === code);
    if (!piece) throw new HttpError(404, 'PIECE_NOT_FOUND', `找不到实物片 ${code}`);
    return piece;
  }

  findBox(code) {
    const box = this.db.boxes.find((b) => b.id === code || b.code === code);
    if (!box) throw new HttpError(404, 'BOX_NOT_FOUND', `找不到盒号 ${code}`);
    return box;
  }

  createBar(input = {}) {
    const length = Number(input.length ?? 8);
    const rows = Number(input.rows ?? 5);
    const cols = Number(input.cols ?? 5);
    if (!Number.isInteger(length) || length < 1 || length > 64) {
      throw new HttpError(400, 'BAD_LENGTH', '条长必须是 1..64 的整数格');
    }
    if (!Number.isInteger(rows) || rows < 1 || rows > 32 || !Number.isInteger(cols) || cols < 1 || cols > 32) {
      throw new HttpError(400, 'BAD_CROSS_SECTION', '截面行列必须是 1..32 的整数');
    }
    const code = input.code || this.nextCode('bar', 'BAR');
    if (this.db.bars.some((b) => b.code === code)) {
      throw new HttpError(409, 'BAR_CODE_EXISTS', `条坯号已存在 ${code}`);
    }
    const now = new Date().toISOString();
    const bar = {
      id: code,
      code,
      kind: 'soap-bar',
      length,
      rows,
      cols,
      standardWidth: Number(input.standardWidth ?? 3),
      kerfWidth: Number(input.kerfWidth ?? 1),
      closeMode: input.closeMode || 'normal',
      seamOffset: input.seamOffset ?? (input.closeMode === 'shift_one' ? 1 : 0),
      shopMode: input.shopMode || 'precut',
      status: 'open',
      cells: Array.from({ length }, (_, index) => ({
        index,
        state: 'intact',
        pieceId: null,
        kerfGroupId: null,
        poured: false,
        pattern: emptyPattern(rows, cols)
      })),
      closedPours: null,
      events: [{ at: now, type: 'mold_opened', data: { length, rows, cols, closeMode: bar_closeMode(input) } }],
      createdAt: now,
      closedAt: null
    };
    this.db.bars.push(bar);
    this.db.events.push({ at: now, type: 'mold_opened', barCode: code });
    return clone(bar);
  }

  pourLayer(barCode, index, patternInput, meta = {}) {
    const bar = this.findBar(barCode);
    const i = Number(index);
    if (!Number.isInteger(i) || i < 0 || i >= bar.length) {
      throw new HttpError(400, 'BAD_LAYER_INDEX', `层号应在 0..${bar.length - 1}`);
    }
    const pattern = normalizePattern(patternInput.pattern ?? patternInput, bar.rows, bar.cols);
    const cell = bar.cells[i];
    const before = clone(cell.pattern);
    cell.pattern = pattern;
    cell.poured = pattern.cells.some((row) => row.some((v) => v !== null));
    const now = new Date().toISOString();
    bar.events.push({ at: now, type: 'layer_poured', index: i, by: meta.by || 'master' });
    if (bar.status === 'closed') {
      bar.events.push({ at: now, type: 'actual_pour_amended', index: i, before, after: clone(pattern), reason: meta.reason || 'replay correction' });
    }
    return clone(bar.cells[i]);
  }

  amendPourCell(barCode, index, row, col, color, reason = 'replay correction') {
    const bar = this.findBar(barCode);
    const i = Number(index);
    const r = Number(row);
    const c = Number(col);
    if (![i, r, c].every(Number.isInteger)) throw new HttpError(400, 'BAD_COORDINATE', 'index/row/col 必须是整数');
    if (i < 0 || i >= bar.length || r < 0 || r >= bar.rows || c < 0 || c >= bar.cols) {
      throw new HttpError(400, 'COORDINATE_OUT_OF_RANGE', '浇注格坐标超出条坯');
    }
    const oldColor = bar.cells[i].pattern.cells[r][c];
    const nextColor = color === null || color === '' ? null : String(color).toUpperCase();
    bar.cells[i].pattern.cells[r][c] = nextColor;
    bar.cells[i].poured = bar.cells[i].pattern.cells.some((line) => line.some((v) => v !== null));
    const now = new Date().toISOString();
    bar.events.push({
      at: now,
      type: 'actual_cell_amended',
      index: i,
      row: r,
      col: c,
      from: oldColor,
      to: nextColor,
      reason
    });
    return { index: i, row: r, col: c, from: oldColor, to: nextColor };
  }

  closeBar(barCode, input = {}) {
    const bar = this.findBar(barCode);
    if (bar.status === 'closed') return clone(bar);
    if (input.closeMode) bar.closeMode = input.closeMode;
    const now = new Date().toISOString();
    bar.status = 'closed';
    bar.closedAt = now;
    bar.closedPours = bar.cells.map((cell) => ({ index: cell.index, poured: cell.poured, pattern: clone(cell.pattern) }));
    bar.events.push({ at: now, type: 'mold_closed', data: { closeMode: bar.closeMode, seamOffset: bar.seamOffset } });
    return clone(bar);
  }

  restoreOriginalPours(barCode) {
    const bar = this.findBar(barCode);
    if (!bar.closedPours) throw new HttpError(409, 'NOT_CLOSED', '条坯尚未合模，没有合模时原始浇注快照');
    for (const saved of bar.closedPours) {
      const cell = bar.cells[saved.index];
      // 只恢复颜色，不恢复切割状态；已切走的格不能因回放而重新变成余料。
      cell.pattern = clone(saved.pattern);
      cell.poured = saved.poured;
    }
    bar.events.push({ at: new Date().toISOString(), type: 'original_pours_restored' });
    return clone(bar);
  }

  createCard(input = {}) {
    const pattern = normalizePattern(input.pattern ?? input.text ?? input, input.rows ?? null, input.cols ?? null);
    const code = input.code || this.nextCode('card', 'CARD');
    if (this.db.cards.some((c) => c.code === code)) throw new HttpError(409, 'CARD_CODE_EXISTS', `样卡号已存在 ${code}`);
    const now = new Date().toISOString();
    const card = {
      id: code,
      code,
      name: input.name || code,
      directional: Boolean(input.directional),
      matchMode: input.matchMode || 'mask',
      orientationPolicy: input.orientationPolicy || 'horizontal-ok',
      pattern,
      version: 1,
      versions: [],
      createdAt: now,
      updatedAt: now
    };
    this.db.cards.push(card);
    return clone(card);
  }

  updateCard(code, patch = {}) {
    const card = this.findCard(code);
    card.versions.push({
      version: card.version,
      name: card.name,
      directional: card.directional,
      matchMode: card.matchMode,
      orientationPolicy: card.orientationPolicy,
      pattern: clone(card.pattern),
      updatedAt: card.updatedAt
    });
    if (patch.name !== undefined) card.name = patch.name;
    if (patch.directional !== undefined) card.directional = Boolean(patch.directional);
    if (patch.matchMode !== undefined) card.matchMode = patch.matchMode;
    if (patch.orientationPolicy !== undefined) card.orientationPolicy = patch.orientationPolicy;
    if (patch.pattern !== undefined || patch.text !== undefined || Array.isArray(patch)) {
      card.pattern = normalizePattern(patch.pattern ?? patch.text ?? patch, card.pattern.rows, card.pattern.cols);
      card.version += 1;
    }
    card.updatedAt = new Date().toISOString();
    return clone(card);
  }

  planCut(bar, input = {}) {
    const width = Number(input.width ?? bar.standardWidth);
    const kerfWidth = Number(input.kerfWidth ?? bar.kerfWidth);
    if (!Number.isInteger(width) || width < 1) throw new HttpError(400, 'BAD_WIDTH', '成品宽度必须是正整数格');
    if (!Number.isInteger(kerfWidth) || kerfWidth < 0) throw new HttpError(400, 'BAD_KERF', '切口宽度必须是非负整数格');

    const segments = remnantSegments(bar);
    if (!segments.length) throw new HttpError(409, 'NO_REMNANT', '这条条坯已经没有连续余料');

    let segment;
    if (input.segmentStart !== undefined) {
      const s = Number(input.segmentStart);
      segment = segments.find((seg) => seg.start === s);
      if (!segment) throw new HttpError(409, 'SEGMENT_NOT_FOUND', `第 ${s} 格起不是连续余料段`, { segments });
    } else {
      segment = segments[0];
    }

    let start;
    let end;
    let kerfIndexes = [];
    const from = input.from || 'head';

    if (input.start !== undefined) {
      start = Number(input.start);
      end = input.end !== undefined ? Number(input.end) : start + width - 1;
      if (start < segment.start || end > segment.end || start > end) {
        throw new HttpError(409, 'CUT_OUTSIDE_REMNANT', '切口/成品区间不在当前连续余料内', { segment });
      }
      if (input.kerfIndexes) kerfIndexes = input.kerfIndexes.map(Number);
    } else if (from === 'tail') {
      end = segment.end;
      start = end - width + 1;
      const kerfEnd = start - 1;
      const kerfStart = kerfEnd - kerfWidth + 1;
      if (start < segment.start) throw new HttpError(409, 'PIECE_DOES_NOT_FIT', `余料不足 ${width} 格`, { segment });
      kerfIndexes = Array.from({ length: kerfWidth }, (_, i) => kerfStart + i).filter((i) => i >= segment.start);
      if (kerfWidth > 0 && kerfIndexes.length !== kerfWidth) {
        throw new HttpError(409, 'KERF_DOES_NOT_FIT', '尾部切口宽度会超出余料；请指定其他切口或保留端料', { segment });
      }
    } else {
      start = segment.start;
      end = start + width - 1;
      if (end > segment.end) throw new HttpError(409, 'PIECE_DOES_NOT_FIT', `余料不足 ${width} 格`, { segment });
      const kerfStart = end + 1;
      kerfIndexes = Array.from({ length: kerfWidth }, (_, i) => kerfStart + i);
      if (kerfWidth > 0 && kerfIndexes[kerfIndexes.length - 1] > segment.end) {
        throw new HttpError(409, 'KERF_DOES_NOT_FIT', '头部切口宽度会超出余料；请指定其他切口或保留端料', { segment });
      }
    }

    for (const i of [...Array.from({ length: end - start + 1 }, (_, k) => start + k), ...kerfIndexes]) {
      if (bar.cells[i].state !== 'intact') {
        throw new HttpError(409, 'CELL_NOT_ACTUAL', `第 ${i} 格已属于成品或切口，不能重复切取`, { index: i });
      }
    }
    return { segment, start, end, width: end - start + 1, kerfIndexes };
  }

  cutFromBar(input = {}) {
    const bar = this.findBar(input.barCode || input.code);
    if (bar.status !== 'closed') throw new HttpError(409, 'BAR_NOT_CLOSED', '请先合模，再按真实浇注图切割');
    const plan = this.planCut(bar, input);
    const code = input.pieceCode || this.nextCode('piece', 'PIECE');
    if (this.db.pieces.some((p) => p.code === code)) throw new HttpError(409, 'PIECE_CODE_EXISTS', `片号已存在 ${code}`);

    const now = new Date().toISOString();
    const piece = {
      id: code,
      code,
      barCode: bar.code,
      start: plan.start,
      end: plan.end,
      width: plan.width,
      kerfIndexes: plan.kerfIndexes,
      status: 'available',
      reservationId: null,
      boxId: null,
      orderId: null,
      clerk: input.clerk || null,
      signedBy: input.signedBy || (input.signedSample ? input.clerk || null : null),
      signedAt: input.signedSample ? now : null,
      signedCardSnapshot: input.cardId ? clone(this.findCard(input.cardId)) : null,
      cutAt: now,
      cutFaces: deriveFaces(bar, plan.start, plan.end),
      createdAt: now
    };

    for (let i = plan.start; i <= plan.end; i += 1) {
      bar.cells[i].state = 'piece';
      bar.cells[i].pieceId = piece.code;
    }
    for (const i of plan.kerfIndexes) {
      bar.cells[i].state = 'kerf';
      bar.cells[i].kerfGroupId = piece.code;
    }
    bar.events.push({
      at: now,
      type: 'piece_cut',
      pieceCode: piece.code,
      start: plan.start,
      end: plan.end,
      kerfIndexes: plan.kerfIndexes,
      clerk: input.clerk || null
    });
    this.db.pieces.push(piece);
    return clone(piece);
  }

  precutBar(input = {}) {
    const bar = this.findBar(input.barCode || input.code);
    const width = Number(input.width ?? bar.standardWidth);
    const kerfWidth = Number(input.kerfWidth ?? bar.kerfWidth);
    const pieces = [];
    // 每次都从当前头条连续余料切；并发安全由 mutate 队列保证。
    while (true) {
      const segments = remnantSegments(bar);
      const seg = segments[0];
      if (!seg || seg.length < width + kerfWidth) break;
      pieces.push(this.cutFromBar({ barCode: bar.code, from: 'head', width, kerfWidth, clerk: input.clerk || 'precut' }));
      if (input.maxPieces && pieces.length >= input.maxPieces) break;
    }
    return pieces.map((p) => clone(p));
  }

  liveFaces(piece) {
    const bar = this.findBar(piece.barCode);
    return deriveFaces(bar, piece.start, piece.end);
  }

  bestMatchForPiece(piece, slot) {
    const card = slot.card || this.findCard(slot.cardId);
    const policy = slot.orientationPolicy || card.orientationPolicy || 'horizontal-ok';
    const bar = this.findBar(piece.barCode);
    const faces = deriveFaces(bar, piece.start, piece.end);
    const options = [];
    for (const face of faces) {
      const result = matchActualGrid(face.barView, card, policy);
      if (result.matches) {
        options.push({
          pieceId: piece.id,
          pieceCode: piece.code,
          barCode: piece.barCode,
          status: piece.status,
          available: piece.status === 'available',
          face: face.side,
          cellIndex: face.cellIndex,
          transform: result.transform,
          orientationPolicy: policy,
          cardId: card.code,
          source: 'actual-pour',
          barView: face.barView,
          outsideView: face.outsideView,
          outwardPattern: transformPattern(face.barView, result.transform),
          bothFaces: faces
        });
      }
    }
    if (!options.length) return null;
    options.sort((a, b) => {
      const score = (m) => (m.transform === 'identity' ? 0 : 1) + (m.face === 'head' ? 0 : 0.1);
      return score(a) - score(b);
    });
    return options[0];
  }

  findMatches(query = {}) {
    const card = query.card ? query.card : this.findCard(query.cardId || query.code);
    const policy = query.orientationPolicy || card.orientationPolicy || 'horizontal-ok';
    let pieces = this.db.pieces.slice();
    if (query.barCode) pieces = pieces.filter((p) => p.barCode === query.barCode);
    if (query.pieceIds) {
      const ids = new Set(query.pieceIds);
      pieces = pieces.filter((p) => ids.has(p.id) || ids.has(p.code));
    }
    if (query.availableOnly) pieces = pieces.filter((p) => p.status === 'available');
    else if (query.status) pieces = pieces.filter((p) => p.status === query.status);

    const matches = [];
    for (const piece of pieces) {
      const match = this.bestMatchForPiece(piece, { card, orientationPolicy: policy });
      if (match) matches.push(match);
    }
    return matches;
  }

  quoteOrder(input = {}) {
    const policy = input.orientationPolicy;
    let template;
    if (Array.isArray(input.template) && input.template.length) {
      template = input.template.map((slot) => ({
        ...slot,
        card: this.findCard(slot.cardId || slot.code),
        orientationPolicy: slot.orientationPolicy || policy
      }));
    } else {
      const card = this.findCard(input.cardId || input.code);
      const count = Number(input.piecesPerBox || input.slotCount || 2);
      template = Array.from({ length: count }, () => ({ card, cardId: card.code, orientationPolicy: policy || card.orientationPolicy }));
    }

    let pieces = this.db.pieces.slice();
    if (input.barCode) pieces = pieces.filter((p) => p.barCode === input.barCode);
    if (input.pieceIds?.length) {
      const ids = new Set(input.pieceIds);
      pieces = pieces.filter((p) => ids.has(p.id) || ids.has(p.code));
    }
    if (input.claimId) {
      const claim = this.db.claims.find((c) => c.code === input.claimId);
      if (!claim) throw new HttpError(404, 'CLAIM_NOT_FOUND', `找不到领片单 ${input.claimId}`);
      const ids = new Set(claim.pieceIds);
      pieces = pieces.filter((p) => ids.has(p.id));
    }
    if (input.availableOnly !== false) {
      // 若指定本人的有效领片单，则 reserved 且属于该单也可装箱。
      pieces = pieces.filter((p) => {
        if (p.status === 'available') return true;
        if (input.claimId && p.status === 'reserved' && p.reservationId === input.claimId) return true;
        return false;
      });
    }

    const findMatch = (piece, slot) => this.bestMatchForPiece(piece, slot);
    const maxPlan = planCompleteBoxes(pieces, template, null, findMatch);
    const boxCount = input.boxCount ?? input.quantity ?? null;
    let requestedPlan = { feasible: true, boxCount: null, assignments: [] };
    if (boxCount !== null && boxCount !== undefined) {
      requestedPlan = planCompleteBoxes(pieces, template, Number(boxCount), findMatch);
    }

    // 回放/验花时要看所有真实片，包括已经装盒的；但它们不会进入上面的可领/可装盒计划。
    const physicalMatches = input.cardId || input.code
      ? this.findMatches({
        cardId: input.cardId || input.code,
        orientationPolicy: policy,
        barCode: input.barCode,
        availableOnly: false
      })
      : [];

    const decorate = (plan) => plan.assignments.map((a) => {
      const piece = pieces[a.pieceIndex];
      const match = this.bestMatchForPiece(piece, template[a.slotIndex]);
      // plan 中已带 match，但重新取最稳；若 flow 使用了某个 option，best 也一定可行。
      return { ...a, pieceIndex: undefined, pieceCode: piece.code, match: a.match || match };
    });

    return {
      feasible: requestedPlan.feasible,
      requestedBoxes: boxCount === null ? null : Number(boxCount),
      maxBoxes: maxPlan.boxCount,
      physicalMatchCount: physicalMatches.length,
      physicalMatches,
      piecesConsidered: pieces.length,
      orientationPolicies: [...new Set(template.map((s) => s.orientationPolicy || 'horizontal-ok'))],
      assignments: decorate(requestedPlan),
      maxAssignments: decorate(maxPlan)
    };
  }

  recommendCards(query = {}) {
    const perBox = Number(query.piecesPerBox || 2);
    return this.db.cards.map((card) => {
      const policy = query.orientationPolicy || card.orientationPolicy || 'horizontal-ok';
      const quote = this.quoteOrder({
        cardId: card.code,
        piecesPerBox: perBox,
        orientationPolicy: policy,
        barCode: query.barCode,
        availableOnly: query.availableOnly !== false
      });
      return {
        cardId: card.code,
        name: card.name,
        directional: card.directional,
        orientationPolicy: policy,
        physicalMatchCount: quote.physicalMatchCount,
        availablePieceCount: quote.piecesConsidered,
        maxCompleteBoxes: quote.maxBoxes,
        canDeliverBox: quote.maxBoxes >= 1
      };
    });
  }

  claimPieces(input = {}) {
    const ids = input.pieceIds || (input.pieceId ? [input.pieceId] : []);
    if (!ids.length) throw new HttpError(400, 'NO_PIECES', '请提供 pieceId 或 pieceIds');
    const pieces = ids.map((id) => this.findPiece(id));
    const unavailable = pieces.filter((p) => p.status !== 'available');
    if (unavailable.length) {
      throw new HttpError(409, 'PIECE_NOT_AVAILABLE', '有实物片已被其他柜员领走或装盒', unavailable.map((p) => ({
        pieceId: p.code,
        status: p.status,
        boxId: p.boxId,
        reservationId: p.reservationId
      })));
    }
    const code = input.claimId || this.nextCode('claim', 'CLM');
    const now = new Date().toISOString();
    const claim = {
      id: code,
      code,
      clerk: input.clerk || 'anonymous',
      pieceIds: pieces.map((p) => p.code),
      status: 'active',
      createdAt: now,
      releasedAt: null
    };
    for (const p of pieces) {
      p.status = 'reserved';
      p.reservationId = code;
      p.claimedBy = claim.clerk;
      p.claimedAt = now;
    }
    this.db.claims.push(claim);
    return clone(claim);
  }

  releaseClaim(code) {
    const claim = this.db.claims.find((c) => c.code === code || c.id === code);
    if (!claim) throw new HttpError(404, 'CLAIM_NOT_FOUND', `找不到领片单 ${code}`);
    if (claim.status !== 'active') return clone(claim);
    claim.status = 'released';
    claim.releasedAt = new Date().toISOString();
    for (const id of claim.pieceIds) {
      const p = this.findPiece(id);
      if (p.reservationId === claim.code && p.status === 'reserved') {
        p.status = 'available';
        p.reservationId = null;
        p.claimedBy = null;
      }
    }
    return clone(claim);
  }

  createOrder(input = {}) {
    const requestedBoxes = Number(input.boxCount ?? input.quantity ?? 1);
    const quote = this.quoteOrder({ ...input, boxCount: requestedBoxes, availableOnly: input.availableOnly });
    if (!quote.feasible) {
      throw new HttpError(409, 'NO_COMPLETE_BOX', `只能凑出 ${quote.maxBoxes} 整盒，无法办理 ${requestedBoxes} 盒`, quote);
    }

    const orderCode = input.orderCode || this.nextCode('order', 'ORD');
    const now = new Date().toISOString();
    const order = {
      id: orderCode,
      code: orderCode,
      clerk: input.clerk || null,
      customer: input.customer || null,
      destination: input.destination || null,
      note: input.note || null,
      status: 'packed',
      boxCount: requestedBoxes,
      cardSummary: input.cardId || input.template?.map((t) => t.cardId) || null,
      boxIds: [],
      claimId: input.claimId || null,
      createdAt: now
    };

    for (let b = 0; b < requestedBoxes; b += 1) {
      const boxCode = this.nextCode('box', 'BOX');
      const assignments = quote.assignments.filter((a) => a.boxIndex === b).sort((a, z) => a.slotIndex - z.slotIndex);
      const slotRecords = assignments.map((a) => {
        const piece = this.findPiece(a.pieceId);
        const match = a.match;
        const card = this.findCard(match.cardId);
        return {
          slotIndex: a.slotIndex,
          pieceId: piece.code,
          pieceCode: piece.code,
          barCode: piece.barCode,
          actualCellIndex: match.cellIndex,
          outwardFace: match.face,
          orientationTransform: match.transform,
          orientationPolicy: match.orientationPolicy,
          cardSnapshot: clone(card),
          outwardPattern: clone(match.outwardPattern),
          twoFaces: clone(match.bothFaces)
        };
      });
      const pieceIds = slotRecords.map((s) => s.pieceId);
      const box = {
        id: boxCode,
        code: boxCode,
        orderId: orderCode,
        status: 'packed',
        slotCount: slotRecords.length,
        pieceIds,
        slots: slotRecords,
        // 方便按盒号直接查“每片两面”。
        pieceFaces: slotRecords.map((s) => ({
          pieceId: s.pieceId,
          barCode: s.barCode,
          outwardFace: s.outwardFace,
          orientationTransform: s.orientationTransform,
          headFace: s.twoFaces.find((f) => f.side === 'head'),
          tailFace: s.twoFaces.find((f) => f.side === 'tail')
        })),
        destination: order.destination,
        customer: order.customer,
        packedAt: now,
        pickup: null,
        events: [{ at: now, type: 'packed', by: order.clerk }]
      };
      this.db.boxes.push(box);
      order.boxIds.push(boxCode);
      for (const id of pieceIds) {
        const piece = this.findPiece(id);
        piece.status = 'boxed';
        piece.boxId = boxCode;
        piece.orderId = orderCode;
        piece.boxedAt = now;
      }
    }

    if (input.claimId) {
      const claim = this.db.claims.find((c) => c.code === input.claimId);
      if (claim) claim.status = 'consumed';
    }
    this.db.orders.push(order);

    let boxes = order.boxIds.map((id) => clone(this.findBox(id)));
    if (input.pickup) {
      boxes = boxes.map((box) => this.pickupBox(box.code, {
        receivedBy: input.receivedBy || input.clerk || 'counter',
        destination: input.destination,
        note: input.pickupNote
      }));
      order.status = 'picked_up';
      order.pickedUpAt = boxes[0]?.pickup?.at || now;
    }
    return { order: clone(order), boxes };
  }

  pickupBox(code, input = {}) {
    const box = this.findBox(code);
    if (box.status === 'picked_up') {
      throw new HttpError(409, 'BOX_ALREADY_PICKED_UP', `盒 ${code} 已在 ${box.pickup?.at} 取走`, box.pickup);
    }
    const now = new Date().toISOString();
    box.status = 'picked_up';
    box.pickup = {
      at: now,
      receivedBy: input.receivedBy || input.customer || 'customer',
      destination: input.destination || box.destination || '到店自取',
      note: input.note || null
    };
    if (input.destination) box.destination = input.destination;
    box.events.push({ at: now, type: 'picked_up', by: input.receivedBy || null, destination: box.pickup.destination });
    for (const pieceId of box.pieceIds) {
      const piece = this.findPiece(pieceId);
      piece.status = 'picked_up';
      piece.pickedUpAt = now;
    }
    const order = this.db.orders.find((o) => o.code === box.orderId);
    if (order && order.boxIds.every((id) => this.findBox(id).status === 'picked_up')) {
      order.status = 'picked_up';
      order.pickedUpAt = now;
    }
    return clone(box);
  }

  remnants(barCode) {
    const bar = this.findBar(barCode);
    const segments = remnantSegments(bar).map((seg) => ({
      ...seg,
      cells: Array.from({ length: seg.length }, (_, i) => seg.start + i),
      fitsStandardPiece: seg.length >= bar.standardWidth + bar.kerfWidth
    }));
    return {
      barCode: bar.code,
      length: bar.length,
      standardWidth: bar.standardWidth,
      kerfWidth: bar.kerfWidth,
      segments,
      cellStates: bar.cells.map((c) => ({ index: c.index, state: c.state, pieceId: c.pieceId }))
    };
  }

  replayBar(barCode, options = {}) {
    const bar = this.findBar(barCode);
    const source = options.source === 'closed' && bar.closedPours ? bar.closedPours : bar.cells;
    return {
      barCode: bar.code,
      status: bar.status,
      closeMode: bar.closeMode,
      rows: bar.rows,
      cols: bar.cols,
      source: options.source || 'current-actual',
      layers: source.map((cell) => {
        const pattern = cell.pattern;
        return {
          index: cell.index,
          state: bar.cells[cell.index].state,
          pieceId: bar.cells[cell.index].pieceId,
          poured: cell.poured,
          barHeadView: pattern,
          tailEndView: mirrorHorizontal(pattern),
          text: undefined
        };
      })
    };
  }
}

function bar_closeMode(input) {
  return input.closeMode || 'normal';
}
