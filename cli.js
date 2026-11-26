'use strict';

const DEFAULT_BASE = process.env.HOTLINE_URL || 'http://127.0.0.1:3000';
const DEFAULT_PHONE = process.env.CALLER_PHONE || '13800001111';

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  const valueFlags = new Set(['base', 'phone', 'rev', 'text', 'leg', 'call', 'agent', 'simulate']);
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token.startsWith('--')) {
      const raw = token.slice(2);
      const eq = raw.indexOf('=');
      if (eq >= 0) {
        flags[raw.slice(0, eq)] = raw.slice(eq + 1);
      } else if (valueFlags.has(raw) && i + 1 < argv.length && !argv[i + 1].startsWith('--')) {
        flags[raw] = argv[i + 1];
        i += 1;
      } else {
        flags[raw] = true;
      }
    } else {
      positional.push(token);
    }
  }
  return { positional, flags };
}

function inferPhone(positional, flags) {
  if (flags.phone) return String(flags.phone).replace(/[^\d]/g, '');
  const maybePhone = positional.find((value) => /^\d{7,}$/.test(value));
  return maybePhone || DEFAULT_PHONE;
}

async function request(base, method, route, body) {
  const res = await fetch(`${base}${route}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let json;
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    json = { raw: text };
  }
  return { status: res.status, ok: res.ok, json };
}

function printResult(result, flags) {
  if (flags.quiet) return;
  console.log(JSON.stringify(result.json, null, 2));
}

function bodyWithRev(flags, body = {}) {
  if (flags.rev !== undefined && flags.rev !== true) body.expectedRevision = Number(flags.rev);
  return body;
}

async function main() {
  const { positional, flags } = parseArgs(process.argv.slice(2));
  const [command, ...rest] = positional;
  const base = flags.base || DEFAULT_BASE;

  if (!command || command === 'help' || flags.help) {
    console.log(`社区话务后台 CLI

用法:
  node cli.js board [--base http://127.0.0.1:3000]
  node cli.js caller [phone] [--phone 138...]
  node cli.js audio [--phone 138...]

模拟流程（默认号码 ${DEFAULT_PHONE}，可用 --phone 覆盖）:
  node cli.js inbound [phone]
  node cli.js answer [a]
  node cli.js ask date|pickup
  node cli.js say-date [a]
  node cli.js say-pickup [b]
  node cli.js hold [a]
  node cli.js consult [a b]
  node cli.js answer-consult [b]
  node cli.js assign [a b]
  node cli.js bridge
  node cli.js complete [a b]
  node cli.js hangup caller|a|b
  node cli.js retrieve [a]

资格/并发/通知:
  node cli.js grant b pickup
  node cli.js revoke b pickup
  node cli.js presence b available|busy|offline
  node cli.js close pickup <utteranceId> [b]
  node cli.js event <eventId> leg.disconnected <providerLegId>
  node cli.js reset
  加 --rev=<revision> 可携带旧版本号验证 stale_screen。
`);
    return;
  }

  let result;
  const phone = inferPhone(rest, flags);

  switch (command) {
    case 'board':
      result = await request(base, 'GET', '/board');
      break;
    case 'agents':
      result = await request(base, 'GET', '/agents');
      break;
    case 'state':
      result = await request(base, 'GET', '/state');
      break;
    case 'caller':
    case 'show': {
      const target = rest.find((v) => /^\d{7,}$/.test(v)) || phone;
      result = await request(base, 'GET', `/caller/${target}`);
      break;
    }
    case 'audio': {
      const detail = await request(base, 'GET', `/caller/${phone}`);
      const callId = detail.json.activeCase?.primaryCallId || detail.json.cases?.at(-1)?.primaryCallId;
      if (!callId) throw new Error('找不到通话 ID');
      result = await request(base, 'GET', `/calls/${callId}/audio`);
      break;
    }
    case 'reset':
      result = await request(base, 'POST', '/admin/reset', {});
      break;
    case 'inbound': {
      const target = rest[0] && /^\d{7,}$/.test(rest[0]) ? rest[0] : phone;
      result = await request(base, 'POST', '/console/inbound', bodyWithRev(flags, { callerNumber: target, agentId: flags.agent || 'a' }));
      break;
    }
    case 'answer':
      result = await request(base, 'POST', '/console/answer', bodyWithRev(flags, { callerNumber: phone, agentId: rest[0] || 'a' }));
      break;
    case 'ask':
      if (!rest[0]) throw new Error('请提供 date 或 pickup');
      result = await request(base, 'POST', '/console/ask', bodyWithRev(flags, { callerNumber: phone, itemCode: rest[0] }));
      break;
    case 'speak': {
      const [agentId, itemCode, ...textParts] = rest;
      const text = textParts.join(' ');
      if (!agentId || !itemCode || !text) throw new Error('用法: speak <agent> <itemCode> "<答复文本>"');
      result = await request(base, 'POST', '/console/speak', bodyWithRev(flags, { callerNumber: phone, agentId, itemCode, text }));
      break;
    }
    case 'say-date': {
      const agentId = rest[0] || 'a';
      result = await request(base, 'POST', '/console/speak', bodyWithRev(flags, {
        callerNumber: phone,
        agentId,
        itemCode: 'date',
        text: flags.text || '社区图书馆下周一上午九点开放。',
      }));
      break;
    }
    case 'say-pickup': {
      const agentId = rest[0] || 'b';
      result = await request(base, 'POST', '/console/speak', bodyWithRev(flags, {
        callerNumber: phone,
        agentId,
        itemCode: 'pickup',
        text: flags.text || '请凭取件码到北门服务台领取。',
      }));
      break;
    }
    case 'close': {
      const [itemCode, utteranceId, agentId] = rest;
      if (!itemCode || !utteranceId) throw new Error('用法: close <itemCode> <utteranceId> [agentId]');
      result = await request(base, 'POST', '/console/close-item', bodyWithRev(flags, {
        callerNumber: phone, itemCode, utteranceId, agentId: agentId || 'b',
      }));
      break;
    }
    case 'hold':
      result = await request(base, 'POST', '/console/hold', bodyWithRev(flags, { callerNumber: phone, agentId: rest[0] || 'a' }));
      break;
    case 'retrieve':
      result = await request(base, 'POST', '/console/retrieve', bodyWithRev(flags, { callerNumber: phone, agentId: rest[0] || 'a' }));
      break;
    case 'consult': {
      const fromAgentId = rest[0] || 'a';
      const targetAgentId = rest[1] || 'b';
      result = await request(base, 'POST', '/console/consult', bodyWithRev(flags, {
        callerNumber: phone, fromAgentId, targetAgentId, simulate: flags.simulate,
      }));
      break;
    }
    case 'answer-consult':
      result = await request(base, 'POST', '/console/answer-consult', bodyWithRev(flags, { callerNumber: phone, agentId: rest[0] || 'b' }));
      break;
    case 'assign':
      result = await request(base, 'POST', '/console/assign', bodyWithRev(flags, {
        callerNumber: phone, fromAgentId: rest[0] || 'a', toAgentId: rest[1] || 'b',
      }));
      break;
    case 'bridge':
      result = await request(base, 'POST', '/console/bridge', bodyWithRev(flags, { callerNumber: phone }));
      break;
    case 'complete':
    case 'complete-handoff':
      result = await request(base, 'POST', '/console/complete-handoff', bodyWithRev(flags, {
        callerNumber: phone, fromAgentId: rest[0] || 'a', toAgentId: rest[1] || 'b',
      }));
      break;
    case 'hangup':
      if (!rest[0]) throw new Error('用法: hangup caller|a|b [--leg=...]');
      result = await request(base, 'POST', '/console/hangup', bodyWithRev(flags, {
        callerNumber: phone,
        who: flags.leg ? undefined : rest[0],
        legId: flags.leg || undefined,
        callId: flags.call || undefined,
      }));
      break;
    case 'grant':
    case 'revoke': {
      const [agentId, skill] = rest;
      if (!agentId || !skill) throw new Error(`用法: ${command} <agentId> <skill>`);
      const method = command === 'grant' ? 'PUT' : 'DELETE';
      result = await request(base, method, `/agents/${agentId}/skills/${skill}`, bodyWithRev(flags, {}));
      break;
    }
    case 'presence': {
      const [agentId, presence] = rest;
      result = await request(base, 'POST', `/agents/${agentId}/presence`, bodyWithRev(flags, { presence }));
      break;
    }
    case 'event': {
      const [eventId, type, providerLegId] = rest;
      if (!eventId || !type || !providerLegId) throw new Error('用法: event <eventId> <type> <providerLegId>');
      result = await request(base, 'POST', '/telephony/events', { eventId, type, providerLegId });
      break;
    }
    default:
      throw new Error(`未知命令 ${command}；运行 node cli.js help`);
  }

  printResult(result, flags);
  if (!result.ok) process.exitCode = 1;
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
