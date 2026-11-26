'use strict';

const fs = require('fs');
const path = require('path');
const { DomainError, codes, validation, notFound, conflict } = require('./errors');

const STATE_VERSION = 1;

/**
 * 战役借还后台的核心服务。
 *
 * 关键不变量（贯穿全文件）：
 *  1. 队伍存档（team）是纯逻辑：章节进度、走过的门、已知揭晓、钥匙。
 *  2. 实物盒（box）只记录“永久痕迹”：贴死的门牌、破封、刻痕。二者绝不互相覆盖。
 *  3. 还盒必须同时、分别地声明“这队玩到哪”与“这盒留下哪道痕迹”。
 *  4. 所有写操作通过串行事务队列执行，保证并发借最后一盒时只有一个成功。
 *  5. 每次变更立即落盘，重启后凭持久化的借单仍可追溯。
 */
class CampaignService {
  constructor(stateFile, state) {
    this.file = stateFile ? path.resolve(stateFile) : null;
    this.state = state || CampaignService._emptyState();
    this._chain = Promise.resolve();
  }

  static _emptyState() {
    return {
      version: STATE_VERSION,
      seq: { team: 0, box: 0, loan: 0, card: 0, pouch: 0, receipt: 0 },
      rulebooks: {},
      teams: {},
      boxes: {},
      pouches: {},
      cards: {},
      loans: {},
      receipts: [],
    };
  }

  // ---------------------------------------------------------------- 持久化

  static open(stateFile) {
    let state = CampaignService._emptyState();
    if (stateFile && fs.existsSync(stateFile)) {
      const raw = fs.readFileSync(stateFile, 'utf8');
      if (raw.trim()) state = JSON.parse(raw);
      if (state.version !== STATE_VERSION) {
        throw new Error(`不支持的存档版本 ${state.version}`);
      }
    }
    return new CampaignService(stateFile, state);
  }

  /** 原子落盘：写临时文件后 rename，避免重启时读到半截 JSON。 */
  persist() {
    if (!this.file) return;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 2));
    fs.renameSync(tmp, this.file);
  }

  /** 导出只读快照（深拷贝），用于测试与场景克隆。 */
  snapshot() {
    return JSON.parse(JSON.stringify(this.state));
  }

  /** 把当前状态另存为另一份文件（“相同库存另开演示”就靠它）。 */
  cloneTo(stateFile) {
    fs.mkdirSync(path.dirname(path.resolve(stateFile)), { recursive: true });
    fs.writeFileSync(path.resolve(stateFile), JSON.stringify(this.state, null, 2));
    return CampaignService.open(stateFile);
  }

  /**
   * 串行事务：所有写操作排队执行，单进程内天然互斥。
   * 回调里是纯同步代码；抛错则不落盘、不保留部分修改（回调应先在本地算好再写）。
   * 用一条末尾被“接住”的 Promise 链串起来，确保任意一笔失败都不会卡死后续事务。
   */
  _tx(fn) {
    const run = this._chain.then(() => {
      const before = this.snapshot();
      try {
        const result = fn();
        this.persist();
        return result;
      } catch (err) {
        // 回滚到事务开始前的快照，保证失败不污染内存状态。
        this.state = before;
        throw err;
      }
    });
    // 链本身永不 reject，避免一次失败拖垮所有后续事务。
    this._chain = run.then(() => undefined, () => undefined);
    return run;
  }

  _nextId(kind, prefix) {
    this.state.seq[kind] = (this.state.seq[kind] || 0) + 1;
    return `${prefix}-${this.state.seq[kind]}`;
  }

  _now() { return new Date().toISOString(); }

  _clone(x) { return JSON.parse(JSON.stringify(x)); }

  // ---------------------------------------------------------------- 规则录入

  /** 馆员录入战役规则：章节、每章的互斥门、暗袋、开门条件。 */
  createRulebook(def) {
    if (!def || !def.id) throw validation('规则需要 id');
    if (!Array.isArray(def.chapters) || !def.chapters.length) {
      throw validation('规则至少需要一个章节');
    }
    return this._tx(() => {
      if (this.state.rulebooks[def.id]) {
        throw conflict('该战役规则已存在', { rulebookId: def.id });
      }
      const normalized = this._clone(def);
      normalized.chapters.sort((a, b) => a.index - b.index);
      for (const ch of normalized.chapters) {
        ch.doors = ch.doors || [];
        ch.hiddenPouches = ch.hiddenPouches || [];
        if (!ch.doors.length) throw validation(`章节 ${ch.index} 没有门`);
      }
      this.state.rulebooks[normalized.id] = normalized;
      return this._clone(normalized);
    });
  }

  getRulebook(id) {
    const rb = this.state.rulebooks[id];
    if (!rb) throw notFound('没有这条战役规则', { rulebookId: id });
    return this._clone(rb);
  }

  listRulebooks() { return Object.values(this.state.rulebooks).map((r) => this._clone(r)); }

  _rulebookFor(campaignId) {
    const rb = this.state.rulebooks[campaignId];
    if (!rb) throw notFound('战役规则不存在', { campaignId });
    return rb;
  }

  _chapter(rb, index) {
    const ch = rb.chapters.find((c) => c.index === index);
    if (!ch) throw notFound('章节不存在', { chapter: index });
    return ch;
  }

  // ---------------------------------------------------------------- 库存登记

  createBox({ kind, campaignId, label, permanentMarks }) {
    if (!['adopted', 'base'].includes(kind)) {
      throw validation('kind 只能是 adopted（认养盒）或 base（底盒）', { kind });
    }
    this._rulebookFor(campaignId);
    return this._tx(() => {
      const id = this._nextId('box', 'box');
      const box = {
        id,
        kind,
        campaignId,
        label: label || id,
        status: 'available',
        currentLoanId: null,
        adoptedByTeamId: null,
        permanentMarks: {
          affixedPlates: [],
          brokenSeals: [],
          grooves: [],
          ...(permanentMarks ? this._clone(permanentMarks) : {}),
        },
        createdAt: this._now(),
      };
      // 预置痕迹也补全字段，避免缺项。
      box.permanentMarks.affixedPlates = box.permanentMarks.affixedPlates || [];
      box.permanentMarks.brokenSeals = box.permanentMarks.brokenSeals || [];
      box.permanentMarks.grooves = box.permanentMarks.grooves || [];
      this.state.boxes[id] = box;
      return this._clone(box);
    });
  }

  getBox(id) {
    const b = this.state.boxes[id];
    if (!b) throw notFound('实物盒不存在', { boxId: id });
    return this._clone(b);
  }

  listBoxes(filter = {}) {
    return Object.values(this.state.boxes)
      .filter((b) => !filter.kind || b.kind === filter.kind)
      .filter((b) => !filter.campaignId || b.campaignId === filter.campaignId)
      .map((b) => this._clone(b));
  }

  createCard({ campaignId, plotKey, state = 'sealed', label }) {
    this._rulebookFor(campaignId);
    if (!plotKey) throw validation('图卡需要 plotKey（对应它揭晓的情节）');
    if (!['sealed', 'opened'].includes(state)) {
      throw validation('图卡 state 只能是 sealed（完好密封）或 opened（曾被拆封）', { state });
    }
    return this._tx(() => {
      const id = this._nextId('card', 'card');
      const card = {
        id,
        campaignId,
        plotKey,
        state,
        label: label || id,
        currentLoanId: null,
        openedByLoanId: null,
        openedAt: state === 'opened' ? this._now() : null,
      };
      this.state.cards[id] = card;
      return this._clone(card);
    });
  }

  getCard(id) {
    const c = this.state.cards[id];
    if (!c) throw notFound('图卡不存在', { cardId: id });
    return this._clone(c);
  }

  listCards(filter = {}) {
    return Object.values(this.state.cards)
      .filter((c) => !filter.state || c.state === filter.state)
      .filter((c) => !filter.plotKey || c.plotKey === filter.plotKey)
      .filter((c) => !filter.campaignId || c.campaignId === filter.campaignId)
      .map((c) => this._clone(c));
  }

  createPouch({ teamId, campaignId }) {
    const team = this.state.teams[teamId];
    if (!team) throw notFound('队伍不存在', { teamId });
    const camp = campaignId || team.campaignId;
    this._rulebookFor(camp);
    return this._tx(() => {
      const id = this._nextId('pouch', 'pouch');
      const pouch = {
        id,
        teamId,
        campaignId: camp,
        status: 'available',
        currentLoanId: null,
        // 允许分离、跟着队伍走的部件。
        separable: { plates: [], revealedCards: [] },
        createdAt: this._now(),
      };
      this.state.pouches[id] = pouch;
      return this._clone(pouch);
    });
  }

  getPouch(id) {
    const p = this.state.pouches[id];
    if (!p) throw notFound('个人战役袋不存在', { pouchId: id });
    return this._clone(p);
  }

  listPouches(filter = {}) {
    return Object.values(this.state.pouches)
      .filter((p) => !filter.teamId || p.teamId === filter.teamId)
      .map((p) => this._clone(p));
  }

  // ---------------------------------------------------------------- 队伍

  createTeam({ name, campaignId }) {
    const rb = this._rulebookFor(campaignId);
    const firstChapter = rb.chapters[0].index;
    return this._tx(() => {
      const id = this._nextId('team', 'team');
      const team = {
        id,
        name: name || id,
        campaignId,
        progress: { completedChapters: [], currentChapter: firstChapter },
        plot: [],
        keys: [],
        reveals: [],
        hiddenPouchesOpened: [],
        createdAt: this._now(),
      };
      this.state.teams[id] = team;
      return this._clone(team);
    });
  }

  getTeam(id) {
    const t = this.state.teams[id];
    if (!t) throw notFound('队伍不存在', { teamId: id });
    return this._clone(t);
  }

  listTeams(filter = {}) {
    return Object.values(this.state.teams)
      .filter((t) => !filter.campaignId || t.campaignId === filter.campaignId)
      .map((t) => this._clone(t));
  }

  // ---------------------------------------------------------------- 规则推演

  _plotAt(team, chapter) {
    return team.plot.find((p) => p.chapter === chapter) || null;
  }

  /**
   * 结构上合法的门：钥匙齐全、前序门吻合、且不违背本章已定情节（互斥）。
   * 这里不检查密封图卡——那是“资源可达性”，由调用方按库存另判。
   */
  _structurallyLegalDoors(team, chapter) {
    const rb = this._rulebookFor(team.campaignId);
    const ch = this._chapter(rb, chapter);
    const decided = this._plotAt(team, chapter);
    const legal = [];
    for (const door of ch.doors) {
      const req = door.requires || {};
      if (req.keys && !req.keys.every((k) => team.keys.includes(k))) continue;
      if (req.priorDoor) {
        const pm = this._plotAt(team, req.priorDoor.chapter);
        if (!pm || pm.door !== req.priorDoor.door) continue;
      }
      // 互斥：本章若已选择过别的门，这扇门就永远走不了。
      if (decided && decided.door !== door.id) continue;
      legal.push(door);
    }
    return legal;
  }

  /** 是否存在某张“完好密封、未被占用、情节匹配”的图卡。 */
  _findSealedCard(plotKey, excludeCardId) {
    return Object.values(this.state.cards).find(
      (c) =>
        c.state === 'sealed' &&
        c.currentLoanId === null &&
        c.plotKey === plotKey &&
        c.id !== excludeCardId
    ) || null;
  }

  /** 对某一扇门，检查它需要的密封图卡是否可满足。 */
  _doorCardNeed(door) {
    const req = door.requires || {};
    return req.sealedCardPlot || null;
  }

  /**
   * 给出队伍在下一章的可续玩性报告。
   * 返回 { legal: [...doors], satisfiable: [...doors], reasons: {...} }
   */
  _continuationPlan(team, chapter) {
    const legal = this._structurallyLegalDoors(team, chapter);
    const satisfiable = [];
    const reasons = {};
    for (const door of legal) {
      const needPlot = this._doorCardNeed(door);
      if (!needPlot) { satisfiable.push(door); continue; }
      const card = this._findSealedCard(needPlot);
      if (card) satisfiable.push(door);
      else {
        reasons[door.id] = {
          code: codes.UNREVEALED_PLOT_UNAVAILABLE,
          message: `门 ${door.id} 需要情节「${needPlot}」的完好密封图卡，但馆内可用图卡均已被拆封`,
          missingPlot: needPlot,
        };
      }
    }
    return { legal, satisfiable, reasons };
  }

  // ---------------------------------------------------------------- 领盒前判定

  /**
   * 直接回答“这盒还能不能接”的问题（可只看、不占用）。
   * 用于柜员发盒前预检，也用于开篇那句：一盒里那扇门已经贴死，
   * 周六来的另一队还能不能接它自己的战役。
   */
  evaluateTakeover(teamId, boxId, { takeover, pouchId } = {}) {
    const team = this.state.teams[teamId];
    if (!team) throw notFound('队伍不存在', { teamId });
    const box = this.state.boxes[boxId];
    if (!box) throw notFound('实物盒不存在', { boxId });
    const reasons = [];
    let ok = true;

    if (box.campaignId !== team.campaignId) {
      return { ok: false, reasons: [{ code: codes.VALIDATION, message: '盒与队伍不属于同一战役' }] };
    }
    if (box.status !== 'available' || box.currentLoanId) {
      ok = false;
      reasons.push({ code: codes.BOX_UNAVAILABLE, message: '这盒已被借出或占用' });
    }
    if (box.kind === 'adopted') {
      if (box.adoptedByTeamId && box.adoptedByTeamId !== team.id && !takeover) {
        ok = false;
        reasons.push({
          code: codes.CONFLICT,
          message: '这盒已被别的队认养；接手需显式 takeover',
          adoptedByTeamId: box.adoptedByTeamId,
        });
      }
      for (const plate of box.permanentMarks.affixedPlates) {
        const t = this._plotAt(team, plate.chapter);
        if (t && t.door !== plate.door) {
          ok = false;
          reasons.push({
            code: codes.PLOT_REWRITE,
            message: `盒上第 ${plate.chapter} 章已贴死「${plate.door}」门牌，本队该章走的是「${t.door}」，接手会改写既有剧情`,
            chapter: plate.chapter, boxDoor: plate.door, teamDoor: t.door,
            sourceReceiptId: plate.receiptId || null,
          });
        }
      }
    } else if (box.kind === 'base') {
      const pouch = pouchId ? this.state.pouches[pouchId] : null;
      for (const g of box.permanentMarks.grooves) {
        const fromTeam = this._plotAt(team, g.chapter);
        const fromPouch = pouch && (pouch.separable.plates || []).find((p) => p.chapter === g.chapter);
        const door = fromTeam ? fromTeam.door : fromPouch ? fromPouch.door : null;
        if (door && door !== g.door) {
          ok = false;
          reasons.push({
            code: codes.INCOMPATIBLE_BOX,
            message: `底盒第 ${g.chapter} 章留有「${g.door}」刻痕，与本队剧情「${door}」不兼容`,
            chapter: g.chapter, boxDoor: g.door, teamDoor: door,
            sourceReceiptId: g.receiptId || null,
          });
        }
      }
    }

    // 队伍能否续玩（与盒无关的部分）。
    const chapter = team.progress.currentChapter;
    if (chapter == null) {
      ok = false;
      reasons.push({ code: codes.NO_LEGAL_CONTINUATION, message: '该队战役已走完' });
    } else {
      const plan = this._continuationPlan(team, chapter);
      if (!plan.satisfiable.length) {
        ok = false;
        for (const [doorId, r] of Object.entries(plan.reasons)) {
          reasons.push({ ...r, door: doorId });
        }
        if (!plan.legal.length) {
          reasons.push({ code: codes.NO_LEGAL_CONTINUATION, message: '当前章节没有合法分支' });
        }
      }
    }

    return { ok, reasons };
  }

  /** 查询队伍下一章可续玩的门（含图卡可达性）。 */
  getContinuationPlan(teamId) {
    const team = this.state.teams[teamId];
    if (!team) throw notFound('队伍不存在', { teamId });
    const chapter = team.progress.currentChapter;
    if (chapter == null) return { chapter: null, legalDoors: [], satisfiableDoors: [], reasons: {} };
    const plan = this._continuationPlan(team, chapter);
    return {
      chapter,
      legalDoors: plan.legal.map((d) => d.id),
      satisfiableDoors: plan.satisfiable.map((d) => d.id),
      reasons: plan.reasons,
    };
  }

  // ---------------------------------------------------------------- 领盒（checkout）

  /**
   * 发盒。必须同时满足：队伍还能续玩、实物盒可用、基地/个人袋兼容、
   * 若续玩需要密封图卡则确有一张可发。整个校验与占用在一个事务内完成。
   */
  async checkout(params = {}) {
    const { teamId, model, boxId, pouchId, door, cardId, takeover } = params;
    return this._tx(() => {
      if (!['adopted', 'base'].includes(model)) {
        throw validation('model 只能是 adopted 或 base');
      }
      const team = this.state.teams[teamId];
      if (!team) throw notFound('队伍不存在', { teamId });
      const box = this.state.boxes[boxId];
      if (!box) throw notFound('实物盒不存在', { boxId });
      if (box.campaignId !== team.campaignId) {
        throw validation('实物盒与队伍不属于同一战役', { boxCampaign: box.campaignId, teamCampaign: team.campaignId });
      }
      // 模式与盒型必须匹配：认养盒对认养借法，底盒对底盒+个人袋借法。
      if (model === 'adopted' && box.kind !== 'adopted') {
        throw validation('认养借法需要认养战役盒', { boxKind: box.kind });
      }
      if (model === 'base' && box.kind !== 'base') {
        throw validation('底盒借法需要空白底盒', { boxKind: box.kind });
      }

      // 一队同时只能有一个在借。
      const teamOpenLoan = Object.values(this.state.loans).find(
        (l) => l.teamId === teamId && l.status === 'open'
      );
      if (teamOpenLoan) {
        throw new DomainError(codes.TEAM_BUSY, '这队已有未归还的借盒', { loanId: teamOpenLoan.id });
      }
      // 一盒同时只能借给一桌（并发最后一盒只允许一个拿走）。
      if (box.status !== 'available' || box.currentLoanId) {
        throw new DomainError(codes.BOX_UNAVAILABLE, '这盒已被借出或占用', { boxId });
      }

      const chapter = team.progress.currentChapter;
      if (chapter == null) {
        throw new DomainError(codes.NO_LEGAL_CONTINUATION, '该队战役已走完，没有下一章', { teamId });
      }
      const rb = this._rulebookFor(team.campaignId);

      // 认养盒的接手判定：不得改写既有剧情。
      if (model === 'adopted') {
        this._assertAdoptedCompatible(box, team, { takeover });
      }

      // 底盒 + 个人袋：袋要属于本队、未在借，且与底盒上的非可分离痕迹兼容。
      let pouch = null;
      if (model === 'base') {
        if (!pouchId) throw validation('底盒借法必须指定个人战役袋 pouchId');
        pouch = this.state.pouches[pouchId];
        if (!pouch) throw notFound('个人战役袋不存在', { pouchId });
        if (pouch.teamId !== teamId) throw validation('个人战役袋不属于该队', { pouchId, teamId });
        if (pouch.campaignId !== team.campaignId) throw validation('个人战役袋不属于同一战役');
        if (pouch.status !== 'available' || pouch.currentLoanId) {
          throw new DomainError(codes.CONFLICT, '个人战役袋已在借', { pouchId });
        }
        this._assertBaseCompatible(box, team, pouch);
      }

      // 续玩计划：结构合法的门 + 图卡可满足的门。
      const plan = this._continuationPlan(team, chapter);
      if (!plan.legal.length) {
        throw new DomainError(codes.NO_LEGAL_CONTINUATION, '这队在当前章节没有合法的续玩分支', {
          teamId, chapter,
        });
      }

      // 若指定了门，必须是这个队真的能走的门（不能只接受馆员预演的那一种，反之亦然）。
      let chosenDoor = null;
      if (door != null) {
        chosenDoor = plan.legal.find((d) => d.id === door) || null;
        if (!chosenDoor) {
          throw new DomainError(codes.DOOR_ILLEGAL, `门 ${door} 对此队不合法（钥匙/前序/互斥剧情不符）`, {
            door, chapter,
            legalDoors: plan.legal.map((d) => d.id),
          });
        }
        if (!plan.satisfiable.some((d) => d.id === door)) {
          const reason = plan.reasons[door];
          throw new DomainError(reason.code, reason.message, {
            door, chapter, missingPlot: reason.missingPlot,
          });
        }
      } else if (!plan.satisfiable.length) {
        // 所有结构合法的门都因缺图卡无法兑现 -> 不能承诺未揭情节。
        const firstReason = Object.values(plan.reasons)[0];
        throw new DomainError(
          firstReason ? firstReason.code : codes.NO_LEGAL_CONTINUATION,
          firstReason ? firstReason.message : '没有可兑现的续玩分支',
          { chapter }
        );
      }

      // 只有明确选定了“需要密封图卡”的门时，才在发盒时占用一张图卡。
      // 未指定门时保留多种分支可选，图卡在实际选门时再占用。
      const needPlot = chosenDoor ? this._doorCardNeed(chosenDoor) : null;
      let allocatedCard = null;
      if (needPlot) {
        if (cardId) {
          const c = this.state.cards[cardId];
          if (!c) throw notFound('图卡不存在', { cardId });
          if (c.state !== 'sealed') {
            throw new DomainError(codes.UNREVEALED_PLOT_UNAVAILABLE, '指定的图卡已被拆封，不能承诺未揭情节', { cardId });
          }
          if (c.plotKey !== needPlot) throw validation('指定图卡的情节与门要求不符', { cardId, needPlot, cardPlot: c.plotKey });
          if (c.currentLoanId) throw conflict('指定图卡已被占用', { cardId });
          allocatedCard = c;
        } else {
          allocatedCard = this._findSealedCard(needPlot);
          if (!allocatedCard) {
            throw new DomainError(codes.UNREVEALED_PLOT_UNAVAILABLE, '没有可用的完好密封图卡', { missingPlot: needPlot });
          }
        }
      } else if (cardId) {
        // 不缺口卡却指定了卡：拒绝，避免无意义占用。
        throw validation('该续玩不需要密封图卡', { cardId });
      }
      if (params.openCard === true && !allocatedCard) {
        throw validation('要登记开卡，必须选定一扇需要密封图卡的门并已分配到图卡');
      }

      // 事务内占用资源。
      const loanId = this._nextId('loan', 'loan');
      const receiptId = this._nextId('receipt', 'receipt');
      const now = this._now();
      box.status = 'on_loan';
      box.currentLoanId = loanId;
      if (model === 'adopted') box.adoptedByTeamId = teamId;
      if (pouch) { pouch.status = 'on_loan'; pouch.currentLoanId = loanId; }
      if (allocatedCard) allocatedCard.currentLoanId = loanId;

      const loan = {
        id: loanId,
        receiptId,
        teamId,
        model,
        boxId,
        pouchId: pouch ? pouch.id : null,
        chapter,
        status: 'open',
        door: null,
        allocatedCardId: allocatedCard ? allocatedCard.id : null,
        cardOpened: false,
        actions: [],
        checkoutSnapshot: { teamProgress: this._clone(team.progress), plot: this._clone(team.plot) },
        createdAt: now,
      };

      // 发盒时若要登记“本次真开了图卡”，在此记录开卡。
      if (params.openCard === true && allocatedCard) {
        allocatedCard.state = 'opened';
        allocatedCard.openedByLoanId = loanId;
        allocatedCard.openedAt = now;
        loan.cardOpened = true;
        loan.actions.push({ type: 'open-card', cardId: allocatedCard.id, plotKey: allocatedCard.plotKey, atIssue: true, at: now });
        if (!team.reveals.includes(allocatedCard.plotKey)) team.reveals.push(allocatedCard.plotKey);
      }

      this.state.loans[loanId] = loan;
      const receipt = {
        id: receiptId,
        loanId,
        teamId,
        model,
        boxId,
        pouchId: pouch ? pouch.id : null,
        chapter,
        status: 'issued',
        issuedAt: now,
        returnedAt: null,
        issue: {
          teamProgressBefore: this._clone(team.progress),
          plotBefore: this._clone(team.plot),
          allocatedCardId: allocatedCard ? allocatedCard.id : null,
          cardOpenedAtIssue: loan.cardOpened,
          doorRequested: door != null ? door : null,
        },
        result: null,
      };
      this.state.receipts.push(receipt);

      return {
        loan: this._clone(loan),
        receipt: this._clone(receipt),
        allocatedCard: allocatedCard ? this._clone(allocatedCard) : null,
        legalDoors: plan.legal.map((d) => d.id),
      };
    });
  }

  _assertAdoptedCompatible(box, team, { takeover }) {
    if (box.adoptedByTeamId && box.adoptedByTeamId !== team.id && !takeover) {
      throw conflict('这盒已被别的队认养占用；如需接手请显式 takeover', {
        boxId: box.id, adoptedByTeamId: box.adoptedByTeamId,
      });
    }
    // 盒上贴死的门牌就是既有剧情；接手方不得改写。
    for (const plate of box.permanentMarks.affixedPlates) {
      const t = this._plotAt(team, plate.chapter);
      if (t && t.door !== plate.door) {
        throw new DomainError(
          codes.PLOT_REWRITE,
          `盒上第 ${plate.chapter} 章已贴死「${plate.door}」门牌，本队在该章走的是「${t.door}」，接手会改写既有剧情`,
          {
            boxId: box.id, chapter: plate.chapter,
            boxDoor: plate.door, teamDoor: t.door,
            sourceReceiptId: plate.receiptId || null,
          }
        );
      }
    }
  }

  _assertBaseCompatible(box, team, pouch) {
    if (box.kind !== 'base') return;
    // 底盒上不可拆的刻痕记录了分支 -> 必须与队伍/袋的剧情一致。
    for (const groove of box.permanentMarks.grooves) {
      const fromTeam = this._plotAt(team, groove.chapter);
      const fromPouch = (pouch.separable.plates || []).find((p) => p.chapter === groove.chapter);
      const door = fromTeam ? fromTeam.door : fromPouch ? fromPouch.door : null;
      if (door && door !== groove.door) {
        throw new DomainError(
          codes.INCOMPATIBLE_BOX,
          `底盒在第 ${groove.chapter} 章留有「${groove.door}」刻痕，与本队剧情「${door}」不兼容`,
          { boxId: box.id, chapter: groove.chapter, boxDoor: groove.door, teamDoor: door }
        );
      }
    }
  }

  // ---------------------------------------------------------------- 现场行动

  getLoan(id) {
    const l = this.state.loans[id];
    if (!l) throw notFound('借单不存在', { loanId: id });
    return this._clone(l);
  }

  listLoans(filter = {}) {
    return Object.values(this.state.loans)
      .filter((l) => !filter.teamId || l.teamId === filter.teamId)
      .filter((l) => !filter.status || l.status === filter.status)
      .map((l) => this._clone(l));
  }

  _openLoan(loanId) {
    const loan = this.state.loans[loanId];
    if (!loan) throw notFound('借单不存在', { loanId });
    if (loan.status !== 'open') throw new DomainError(codes.LOAN_CLOSED, '借单已归还，不能再做现场行动', { loanId });
    return loan;
  }

  _teamOf(loan) {
    const team = this.state.teams[loan.teamId];
    if (!team) throw notFound('队伍不存在', { teamId: loan.teamId });
    return team;
  }

  /** 在关卡里选择分支（蓝门/红门）。两种合法剧情都可接受，选中即不可复原。 */
  async chooseDoor(loanId, { door } = {}) {
    return this._tx(() => {
      const loan = this._openLoan(loanId);
      const team = this._teamOf(loan);
      if (!door) throw validation('需要 door');
      const chapter = loan.chapter;
      if (loan.door && loan.door !== door) {
        throw new DomainError(codes.IRREVERSIBLE, `本章已选择「${loan.door}」，不能改为「${door}」`);
      }
      const plan = this._continuationPlan(team, chapter);
      const target = plan.legal.find((d) => d.id === door);
      if (!target) {
        throw new DomainError(codes.DOOR_ILLEGAL, `门 ${door} 对当前队伍不合法`, {
          door, chapter, legalDoors: plan.legal.map((d) => d.id),
        });
      }
      // 缺口卡的门：入门即开卡。优先复用发盒时已占用的卡，否则现场占用一张密封卡。
      const needPlot = this._doorCardNeed(target);
      let usedCard = null;
      if (needPlot) {
        usedCard = loan.allocatedCardId ? this.state.cards[loan.allocatedCardId] : null;
        if (!usedCard || usedCard.plotKey !== needPlot) {
          const candidate = this._findSealedCard(needPlot);
          if (!candidate) {
            throw new DomainError(
              codes.UNREVEALED_PLOT_UNAVAILABLE,
              `门 ${door} 需要情节「${needPlot}」的完好密封图卡，但当前没有可用的一张`,
              { door, missingPlot: needPlot }
            );
          }
          usedCard = candidate;
          usedCard.currentLoanId = loan.id;
          loan.allocatedCardId = usedCard.id;
        }
        if (usedCard.state === 'sealed') {
          usedCard.state = 'opened';
          usedCard.openedByLoanId = loan.id;
          usedCard.openedAt = this._now();
          loan.cardOpened = true;
          loan.actions.push({ type: 'open-card', cardId: usedCard.id, plotKey: usedCard.plotKey, atIssue: false, at: this._now() });
          if (!team.reveals.includes(usedCard.plotKey)) team.reveals.push(usedCard.plotKey);
        }
      }

      // 记录互斥剧情（不可复原）。
      const existing = this._plotAt(team, chapter);
      if (existing && existing.door !== door) {
        throw new DomainError(codes.IRREVERSIBLE, '本章已定别的分支，不能改写');
      }
      if (!existing) {
        const plot = { chapter, door, plot: target.revealsPlot || null };
        team.plot.push(plot);
        if (target.revealsPlot && !team.reveals.includes(target.revealsPlot)) {
          team.reveals.push(target.revealsPlot);
        }
      }
      if (target.grantsKey && !team.keys.includes(target.grantsKey)) {
        team.keys.push(target.grantsKey);
      }
      loan.door = door;
      loan.actions.push({ type: 'choose-door', door, chapter, plot: target.revealsPlot || null, at: this._now() });
      return { loan: this._clone(loan), team: this._clone(team) };
    });
  }

  /** 打开暗袋：揭过便无法复原。可同时获得钥匙/情节。 */
  async openHiddenPouch(loanId, { pouchId } = {}) {
    return this._tx(() => {
      const loan = this._openLoan(loanId);
      const team = this._teamOf(loan);
      if (!pouchId) throw validation('需要 pouchId');
      const rb = this._rulebookFor(team.campaignId);
      const ch = this._chapter(rb, loan.chapter);
      const pouch = (ch.hiddenPouches || []).find((p) => p.id === pouchId);
      if (!pouch) throw notFound('本章没有这个暗袋', { pouchId, chapter: loan.chapter });
      if (team.hiddenPouchesOpened.includes(pouchId)) {
        throw new DomainError(codes.IRREVERSIBLE, '这个暗袋已被揭开，无法复原', { pouchId });
      }
      // 若暗袋绑定了门，必须先走该门。
      if (pouch.door && loan.door !== pouch.door) {
        throw new DomainError(codes.ACTION_NOT_ALLOWED, `暗袋 ${pouchId} 属于「${pouch.door}」分支`, { pouchId, needDoor: pouch.door });
      }
      team.hiddenPouchesOpened.push(pouchId);
      if (pouch.revealsPlot && !team.reveals.includes(pouch.revealsPlot)) {
        team.reveals.push(pouch.revealsPlot);
      }
      if (pouch.grantsKey && !team.keys.includes(pouch.grantsKey)) {
        team.keys.push(pouch.grantsKey);
      }
      loan.actions.push({
        type: 'open-hidden-pouch', pouchId,
        revealedPlot: pouch.revealsPlot || null,
        grantedKey: pouch.grantsKey || null,
        at: this._now(),
      });
      return { loan: this._clone(loan), team: this._clone(team) };
    });
  }

  /** 把门牌贴到地图上。认养盒贴到实物盒（永久）；底盒借法下门牌可分离、进个人袋。 */
  async affixPlate(loanId, { chapter, door, plateId } = {}) {
    return this._tx(() => {
      const loan = this._openLoan(loanId);
      const team = this._teamOf(loan);
      const ch = chapter != null ? chapter : loan.chapter;
      const d = door || loan.door;
      if (!d) throw validation('贴门牌需要 door（或先 chooseDoor）');
      const rb = this._rulebookFor(team.campaignId);
      const chapterDef = this._chapter(rb, ch);
      const doorDef = chapterDef.doors.find((x) => x.id === d);
      if (!doorDef) throw notFound('章节没有这扇门', { chapter: ch, door: d });
      const plate = plateId || doorDef.affixPlate || `plate-${ch}-${d}`;
      const now = this._now();

      if (loan.model === 'adopted') {
        const box = this.state.boxes[loan.boxId];
        if (box.permanentMarks.affixedPlates.some((p) => p.chapter === ch && p.door === d)) {
          return { loan: this._clone(loan), box: this._clone(box), alreadyAffixed: true };
        }
        box.permanentMarks.affixedPlates.push({
          chapter: ch, door: d, plateId: plate,
          loanId: loan.id, receiptId: loan.receiptId, at: now,
        });
        loan.actions.push({ type: 'affix-plate', chapter: ch, door: d, plateId: plate, target: 'box', at: now });
        return { loan: this._clone(loan), box: this._clone(box) };
      }

      // base 借法：门牌允许分离 -> 留在个人战役袋里。
      const pouch = this.state.pouches[loan.pouchId];
      if (!pouch) throw notFound('借单没有个人战役袋', { loanId: loan.id });
      if (!pouch.separable.plates.some((p) => p.chapter === ch && p.door === d)) {
        pouch.separable.plates.push({ chapter: ch, door: d, plateId: plate });
      }
      loan.actions.push({ type: 'affix-plate', chapter: ch, door: d, plateId: plate, target: 'pouch', at: now });
      return { loan: this._clone(loan), pouch: this._clone(pouch) };
    });
  }

  /** 显式开图卡（也可能在 checkout 或 chooseDoor 时已开）。 */
  async openCard(loanId, { cardId } = {}) {
    return this._tx(() => {
      const loan = this._openLoan(loanId);
      const team = this._teamOf(loan);
      const id = cardId || loan.allocatedCardId;
      if (!id) throw validation('没有可开的图卡');
      const card = this.state.cards[id];
      if (!card) throw notFound('图卡不存在', { cardId: id });
      if (card.currentLoanId !== loan.id) throw conflict('图卡不在本次借盒内', { cardId: id });
      if (card.state === 'sealed') {
        card.state = 'opened';
        card.openedByLoanId = loan.id;
        card.openedAt = this._now();
        loan.actions.push({ type: 'open-card', cardId: id, plotKey: card.plotKey, atIssue: false, at: this._now() });
      }
      loan.cardOpened = true;
      if (!team.reveals.includes(card.plotKey)) team.reveals.push(card.plotKey);
      return { loan: this._clone(loan), card: this._clone(card) };
    });
  }

  // ---------------------------------------------------------------- 还盒（return）

  /**
   * 还盒：必须分别声明
   *   progress  —— 这队确实玩到哪里（进入队伍存档）
   *   boxMarks  —— 还回来的这盒真有哪道永久痕迹（进入实物盒）
   * 队伍存档与实物盒永不相覆盖。
   */
  async returnLoan(loanId, body = {}) {
    return this._tx(() => {
      const loan = this._openLoan(loanId);
      const team = this._teamOf(loan);
      const box = this.state.boxes[loan.boxId];
      if (!box) throw notFound('实物盒不存在', { boxId: loan.boxId });
      const now = this._now();
      // 还盒必须同时、分别地说清两件事，缺一不可：
      //   progress  —— 这队确实玩到哪里；
      //   boxMarks  —— 还回来的这盒真有哪道永久痕迹。
      if (!body.progress || typeof body.progress !== 'object') {
        throw validation('还盒必须声明 progress（这队玩到哪里）');
      }
      if (!body.boxMarks || typeof body.boxMarks !== 'object') {
        throw validation('还盒必须声明 boxMarks（这盒真有哪道永久痕迹）');
      }
      if (!['room', 'chapter', 'completedChapter', 'completedChapters'].some(
        (k) => body.progress[k] != null
      ) && !loan.door) {
        throw validation('progress 需至少声明玩到哪一章（completedChapter 等）');
      }
      const progress = body.progress;
      const boxMarks = body.boxMarks;

      // ---- 1) 队伍存档：这队玩到哪里 ----
      const completed = new Set(team.progress.completedChapters);
      if (Array.isArray(progress.completedChapters)) {
        for (const c of progress.completedChapters) completed.add(c);
      }
      if (progress.completedChapter != null) completed.add(progress.completedChapter);
      team.progress.completedChapters = [...completed].sort((a, b) => a - b);

      if (progress.door) {
        // 还盒时可再声明本章走法（须与现场选择一致）。
        const existing = this._plotAt(team, loan.chapter);
        if (existing && existing.door !== progress.door) {
          throw new DomainError(codes.IRREVERSIBLE, '本章已定别的分支，不能改写');
        }
        if (existing && loan.door && loan.door !== progress.door) {
          throw new DomainError(codes.IRREVERSIBLE, '还盒声明的门与现场选择的门不一致');
        }
        if (!existing) {
          team.plot.push({ chapter: loan.chapter, door: progress.door, plot: progress.plot || null });
          if (progress.plot && !team.reveals.includes(progress.plot)) team.reveals.push(progress.plot);
        }
      }
      if (Array.isArray(progress.keys)) {
        for (const k of progress.keys) if (!team.keys.includes(k)) team.keys.push(k);
      }
      if (Array.isArray(progress.reveals)) {
        for (const r of progress.reveals) if (!team.reveals.includes(r)) team.reveals.push(r);
      }
      // 依据规则推进到下一章。
      const rb = this._rulebookFor(team.campaignId);
      const nextCh = rb.chapters.find((c) => !team.progress.completedChapters.includes(c.index));
      team.progress.currentChapter = nextCh ? nextCh.index : null;

      // ---- 2) 实物盒：这盒真有哪道永久痕迹（与队伍存档无关） ----
      const durableChanges = [];
      const adopted = loan.model === 'adopted';
      const pouch = loan.pouchId ? this.state.pouches[loan.pouchId] : null;

      const addAffixed = (raw) => {
        const mark = {
          chapter: raw.chapter, door: raw.door, plateId: raw.plateId || `plate-${raw.chapter}-${raw.door}`,
          loanId: loan.id, receiptId: loan.receiptId, at: now,
        };
        if (adopted) {
          if (!box.permanentMarks.affixedPlates.some((p) => p.chapter === mark.chapter && p.door === mark.door)) {
            box.permanentMarks.affixedPlates.push(mark);
            durableChanges.push({ kind: 'affixedPlate', ...mark });
          }
        } else if (pouch) {
          // 底盒借法：门牌可分离，归入个人战役袋。
          if (!pouch.separable.plates.some((p) => p.chapter === mark.chapter && p.door === mark.door)) {
            pouch.separable.plates.push({ chapter: mark.chapter, door: mark.door, plateId: mark.plateId });
          }
        }
      };
      for (const p of boxMarks.affixedPlates || []) addAffixed(p);

      const addBrokenSeal = (raw) => {
        const mark = {
          chapter: raw.chapter, sealId: raw.sealId || `seal-${raw.chapter}`,
          plotKey: raw.plotKey || null,
          loanId: loan.id, receiptId: loan.receiptId, at: now,
        };
        if (!box.permanentMarks.brokenSeals.some((s) => s.chapter === mark.chapter && s.sealId === mark.sealId)) {
          box.permanentMarks.brokenSeals.push(mark);
          durableChanges.push({ kind: 'brokenSeal', ...mark });
        }
      };
      for (const s of boxMarks.brokenSeals || []) addBrokenSeal(s);

      const addGroove = (raw) => {
        const mark = {
          chapter: raw.chapter, door: raw.door,
          loanId: loan.id, receiptId: loan.receiptId, at: now,
        };
        if (!box.permanentMarks.grooves.some((g) => g.chapter === mark.chapter && g.door === mark.door)) {
          box.permanentMarks.grooves.push(mark);
          durableChanges.push({ kind: 'groove', ...mark });
        }
      };
      for (const g of boxMarks.grooves || []) addGroove(g);

      // ---- 3) 归还资源 ----
      box.status = 'available';
      box.currentLoanId = null;
      if (pouch) { pouch.status = 'available'; pouch.currentLoanId = null; }

      // 本次用到的图卡：已被拆封的，底盒借法下可归入个人袋的“已揭卡”。
      const usedCards = Object.values(this.state.cards).filter((c) => c.currentLoanId === loan.id);
      for (const card of usedCards) {
        card.currentLoanId = null;
        if (card.state === 'opened' && !adopted && pouch) {
          if (!pouch.separable.revealedCards.some((x) => x.cardId === card.id)) {
            pouch.separable.revealedCards.push({ cardId: card.id, plotKey: card.plotKey });
          }
        }
      }

      loan.status = 'returned';
      loan.returnedAt = now;
      loan.return = {
        progressDeclared: this._clone(progress),
        boxMarksDeclared: this._clone(boxMarks),
        teamProgressAfter: this._clone(team.progress),
        plotAfter: this._clone(team.plot),
        keysAfter: this._clone(team.keys),
        boxPermanentMarksAfter: this._clone(box.permanentMarks),
        newDurableChanges: durableChanges,
      };

      const receipt = this.state.receipts.find((r) => r.id === loan.receiptId);
      if (receipt) {
        receipt.status = 'returned';
        receipt.returnedAt = now;
        receipt.result = {
          teamProgressAfter: this._clone(team.progress),
          plotAfter: this._clone(team.plot),
          boxPermanentMarksAfter: this._clone(box.permanentMarks),
          newDurableChanges: this._clone(durableChanges),
        };
      }

      return {
        loan: this._clone(loan),
        team: this._clone(team),
        box: this._clone(box),
        receipt: receipt ? this._clone(receipt) : null,
      };
    });
  }

  // ---------------------------------------------------------------- 借单与追溯

  listReceipts(filter = {}) {
    return this.state.receipts
      .filter((r) => !filter.teamId || r.teamId === filter.teamId)
      .filter((r) => !filter.boxId || r.boxId === filter.boxId)
      .filter((r) => !filter.status || r.status === filter.status)
      .map((r) => this._clone(r));
  }

  getReceipt(id) {
    const r = this.state.receipts.find((x) => x.id === id);
    if (!r) throw notFound('借单不存在', { receiptId: id });
    return this._clone(r);
  }

  /**
   * 重启后依然可用：扫描持久化的盒上永久痕迹与各队存档，
   * 找出“哪队因哪道永久变化需要另挑盒”，并指向制造该痕迹的那张借单。
   */
  reportRebindings() {
    const entries = [];
    const boxes = Object.values(this.state.boxes);
    const teams = Object.values(this.state.teams);
    for (const team of teams) {
      for (const box of boxes) {
        if (box.campaignId !== team.campaignId) continue;
        // 认养盒：贴死的门牌 vs 队伍已定剧情。
        for (const plate of box.permanentMarks.affixedPlates || []) {
          const t = this._plotAt(team, plate.chapter);
          if (t && t.door !== plate.door) {
            entries.push(this._rebindEntry(team, box, {
              kind: 'affixedPlate', chapter: plate.chapter, door: plate.door,
              plateId: plate.plateId, sourceReceiptId: plate.receiptId || null,
              sourceLoanId: plate.loanId || null,
            }, t));
          }
        }
        // 底盒：不可拆刻痕 vs 队伍已定剧情。
        for (const g of box.permanentMarks.grooves || []) {
          const t = this._plotAt(team, g.chapter);
          if (t && t.door !== g.door) {
            entries.push(this._rebindEntry(team, box, {
              kind: 'groove', chapter: g.chapter, door: g.door,
              sourceReceiptId: g.receiptId || null, sourceLoanId: g.loanId || null,
            }, t));
          }
        }
      }
    }
    return { generatedAt: this._now(), entries };
  }

  _rebindEntry(team, box, change, teamChoice) {
    return {
      teamId: team.id,
      teamName: team.name,
      boxId: box.id,
      boxKind: box.kind,
      boxStatus: box.status,
      conflict: change,
      teamChoice: { chapter: teamChoice.chapter, door: teamChoice.door },
      recommendation: 'another-box',
      message:
        `${team.name} 因第 ${change.chapter} 章留有「${change.door}」` +
        `${change.kind === 'affixedPlate' ? '贴死门牌' : '刻痕'}（盒 ${box.id}），` +
        `与本队该章「${teamChoice.door}」不符，需另挑一盒。`,
    };
  }

  // ---------------------------------------------------------------- 仅测试/运维用

  /** 强制设置队伍存档（用于构造“只把原选择换成红”这类对照实验）。 */
  _testSetTeam(teamId, patch) {
    return this._tx(() => {
      const team = this.state.teams[teamId];
      if (!team) throw notFound('队伍不存在', { teamId });
      Object.assign(team, this._clone(patch));
      return this._clone(team);
    });
  }

  /** 强制设置某张图卡状态（用于构造“全部图卡曾被拆封”的对照实验）。 */
  _testSetCard(cardId, patch) {
    return this._tx(() => {
      const card = this.state.cards[cardId];
      if (!card) throw notFound('图卡不存在', { cardId });
      Object.assign(card, this._clone(patch));
      return this._clone(card);
    });
  }
}

module.exports = { CampaignService, STATE_VERSION };
