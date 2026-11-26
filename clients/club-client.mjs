export class ClubApiError extends Error {
  constructor(status, body) {
    super(body?.error?.message || `HTTP ${status}`);
    this.name = 'ClubApiError';
    this.status = status;
    this.code = body?.error?.code;
    this.details = body?.error?.details;
    this.body = body;
  }
}

export class ClubClient {
  constructor(baseUrl = 'http://127.0.0.1:4178', options = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.prefix = options.prefix || 'req';
    this.counter = 0;
  }

  #nextRequestId() {
    this.counter += 1;
    return `${this.prefix}-${this.counter}`;
  }

  async request(method, route, body, options = {}) {
    const requestId = options.requestId || body?.requestId || this.#nextRequestId();
    const payload = body === undefined ? undefined : { ...body, requestId: body?.requestId || requestId };
    const response = await fetch(`${this.baseUrl}${route}`, {
      method,
      headers: {
        'content-type': 'application/json',
        'idempotency-key': requestId,
      },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });

    const text = await response.text();
    const json = text ? JSON.parse(text) : {};
    if (!response.ok) throw new ClubApiError(response.status, json);
    return json;
  }

  health() {
    return this.request('GET', '/api/health');
  }

  league() {
    return this.request('GET', '/api/league');
  }

  createGame(spec) {
    return this.request('POST', '/api/games', spec);
  }

  getGame(gameId) {
    return this.request('GET', `/api/games/${encodeURIComponent(gameId)}`);
  }

  games() {
    return this.request('GET', '/api/games');
  }

  place(gameId, color, x, y, requestId) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/moves`, { color, x, y }, { requestId });
  }

  pass(gameId, color, requestId) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/pass`, { color }, { requestId });
  }

  markDead(gameId, as, stones, requestId) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/markings`, { as, dead: stones }, { requestId });
  }

  disputes(gameId) {
    return this.request('GET', `/api/games/${encodeURIComponent(gameId)}/disputes`);
  }

  verifyEndorsement(gameId, endorsement) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/endorsements/verify`, endorsement);
  }

  openContinuation(gameId, note) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/continuation`, { note });
  }

  openAdjudication(gameId, options = {}) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/adjudication`, options);
  }

  evaluateAdjudication(gameId) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/adjudication/evaluate`, {});
  }

  vote(caseId, refereeId, decision, extra = {}) {
    return this.request('POST', `/api/cases/${encodeURIComponent(caseId)}/votes`, { refereeId, decision, ...extra });
  }

  setDuty(refereeId, onDuty, reason) {
    return this.request('POST', `/api/referees/${encodeURIComponent(refereeId)}/duty`, { onDuty, reason });
  }

  suspend(gameId, reason, pendingWith) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/suspension`, { suspended: true, reason, pendingWith });
  }

  resume(gameId) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/suspension`, { suspended: false });
  }

  settle(gameId, requestId) {
    return this.request('POST', `/api/games/${encodeURIComponent(gameId)}/settle`, {}, { requestId });
  }

  replay(gameId, step) {
    const suffix = step === undefined ? '' : `?step=${step}`;
    return this.request('GET', `/api/games/${encodeURIComponent(gameId)}/replay${suffix}`);
  }

  results() {
    return this.request('GET', '/api/league/results');
  }

  standings() {
    return this.request('GET', '/api/league/standings');
  }
}
