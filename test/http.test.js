'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { createServer } = require('../src/server');
const { CampaignService } = require('../src/service');
const { seedBase } = require('../src/fixtures');

async function startServer() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'camp-http-'));
  const stateFile = path.join(dir, 'state.json');
  const service = CampaignService.open(stateFile);
  const ids = await seedBase(service);
  const server = createServer({ service });
  await new Promise((resolve) => server.listen(0, resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { server, base, dir, ids };
}

async function req(base, method, urlPath, body) {
  const res = await fetch(base + urlPath, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  return { status: res.status, json };
}

test('HTTP：健康检查与规则录入', async () => {
  const { server, base } = await startServer();
  try {
    const health = await req(base, 'GET', '/health');
    assert.equal(health.status, 200);
    assert.equal(health.json.ok, true);

    const rb = await req(base, 'GET', '/rulebooks/gate-of-embers');
    assert.equal(rb.status, 200);
    assert.equal(rb.json.rulebook.chapters.length, 2);
  } finally {
    server.close();
  }
});

test('HTTP：主线发盒 -> 开卡 -> 回溯，错误带状态码与 code', async () => {
  const { server, base, ids } = await startServer();
  try {
    // 柜台对甲发蓝门续局并登记开卡。
    const out = await req(base, 'POST', '/loans', {
      teamId: ids.teamAId, model: 'adopted', boxId: ids.adoptedBoxId,
      door: 'blue', cardId: ids.mapCardId, openCard: true,
    });
    assert.equal(out.status, 200);
    assert.equal(out.json.allocatedCard.id, ids.mapCardId);
    const receiptId = out.json.receipt.id;

    // 单查回单。
    const r = await req(base, 'GET', `/receipts/${receiptId}`);
    assert.equal(r.status, 200);
    assert.equal(r.json.receipt.issue.cardOpenedAtIssue, true);

    // 重复借同一盒 -> 409 BOX_UNAVAILABLE。
    const dup = await req(base, 'POST', '/loans', {
      teamId: ids.teamBId, model: 'adopted', boxId: ids.adoptedBoxId, door: 'red',
    });
    assert.equal(dup.status, 409);
    assert.equal(dup.json.error.code, 'BOX_UNAVAILABLE');
  } finally {
    server.close();
  }
});

test('HTTP：两种合法剧情均可通过 API 选择，未预演的蓝也可接受', async () => {
  const { server, base, ids } = await startServer();
  try {
    const t = (await req(base, 'POST', '/teams', { name: 'HTTP桌', campaignId: ids.rulebookId })).json.team;
    const p = (await req(base, 'POST', '/pouches', { teamId: t.id, campaignId: ids.rulebookId })).json.pouch;
    const out = await req(base, 'POST', '/loans', { teamId: t.id, model: 'base', boxId: ids.baseBoxId, pouchId: p.id });
    assert.equal(out.status, 200);
    const chose = await req(base, 'POST', `/loans/${out.json.loan.id}/choose-door`, { door: 'blue' });
    assert.equal(chose.status, 200);
    assert.equal(chose.json.loan.door, 'blue');
  } finally {
    server.close();
  }
});

test('HTTP：重启后 /reports/rebindings 返回需另挑盒的队伍与来源借单', async () => {
  const { server, base, dir, ids } = await startServer();
  try {
    // 红队认养盒走红并贴死红门牌。
    const out = await req(base, 'POST', '/loans', {
      teamId: ids.teamBId, model: 'adopted', boxId: ids.adoptedBoxId, door: 'red',
    });
    await req(base, 'POST', `/loans/${out.json.loan.id}/choose-door`, { door: 'red' });
    await req(base, 'POST', `/loans/${out.json.loan.id}/return`, {
      progress: { completedChapter: 1 },
      boxMarks: { affixedPlates: [{ chapter: 1, door: 'red' }] },
    });
  } finally {
    server.close();
  }

  // 模拟馆长重启服务：同一份磁盘状态起一个新进程内的实例。
  const reopened = CampaignService.open(path.join(dir, 'state.json'));
  const server2 = createServer({ service: reopened });
  await new Promise((resolve) => server2.listen(0, resolve));
  const base2 = `http://127.0.0.1:${server2.address().port}`;
  try {
    const rep = await req(base2, 'GET', '/reports/rebindings');
    assert.equal(rep.status, 200);
    const hit = rep.json.entries.find((e) => e.teamId === ids.teamAId);
    assert.ok(hit);
    assert.equal(hit.conflict.kind, 'affixedPlate');
    assert.ok(hit.conflict.sourceReceiptId);
  } finally {
    server2.close();
  }
});
