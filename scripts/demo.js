import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { SalonService } from '../src/service.js';

function freshService(label) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `salon-demo-${label}-`));
  const dataFile = path.join(dir, 'salon.json');
  return { service: new SalonService({ dataFile }), dataFile };
}

function seedBase(service) {
  service.addStaff({ id: 'lin', name: '林师傅', canUnroll: true });
  service.addChair({ id: 'chair1', name: '烫卷椅' });
  service.addChair({ id: 'chair2', name: '刘海椅' });
  service.bookAppointment({
    id: 'perm',
    customerName: '第一位客人',
    signature: '第一位客人',
    mode: 'principal',
    principalStylistId: 'lin',
    sections: [{ id: 'last', name: '最后一区' }],
    plannedRollCompleteAt: 0,
    unroll: { durationMinutes: 2, minWaitMinutes: 3, maxWaitMinutes: 4 },
  });
  service.checkIn('perm', { at: 0, chairId: 'chair1' });
  service.completeSection('perm', 'last', { at: 0, stylistId: 'lin' });
}

function bangQuote(service, overrides = {}) {
  return service.quoteInsertion({
    targetAppointmentId: 'perm',
    stylistId: 'lin',
    durationMinutes: 3,
    availableFrom: 0,
    availableUntil: 3,
    chairId: 'chair2',
    ...overrides,
  });
}

function show(title, value) {
  console.log(`\n## ${title}`);
  console.log(JSON.stringify(value, null, 2));
}

// 1. Original example.
let { service, dataFile } = freshService('base');
seedBase(service);
let quote = bangQuote(service);
show('原承诺：第 0 分插三分钟刘海', {
  feasible: quote.feasible,
  stylist: 'lin',
  start: quote.start,
  end: quote.end,
  chair: quote.chairId,
  protectedUnroll: quote.assignments.find((a) => a.appointmentId === 'perm'),
  promise: quote.promise,
});
if (!quote.feasible || quote.start !== 0 || quote.end !== 3) process.exitCode = 1;

service.confirmInsertion({
  customerName: '刘海客',
  signature: '刘海客',
  stylistId: 'lin',
  durationMinutes: 3,
  availableFrom: 0,
  availableUntil: 3,
  chairId: 'chair2',
});

// Restart from the JSON ticket ledger.
service = new SalonService({ dataFile });
const receipt = service.getReceipt('perm');
show('重启后客单回条', {
  dataFile,
  status: receipt.status,
  hash: receipt.processStateHash,
  realized: receipt.segments.filter((s) => s.state === 'realized').map((s) => s.segment),
  rollComplete: receipt.segments.find((s) => s.segment === 'roll_complete'),
});
const afterRestart = bangQuote(service, { durationMinutes: 2, startAt: 3, availableFrom: undefined, availableUntil: undefined });
show('重启后再问：第 3 分插两分钟短活', { feasible: afterRestart.feasible, code: afterRestart.code });

// 2. Actual roll completion two minutes early.
({ service } = freshService('early'));
seedBase(service);
service.completeSection('perm', 'last', { at: -2, stylistId: 'lin' });
quote = bangQuote(service);
show('变异一：卷齐提前两分，最晚第 2 分必须回来', {
  feasible: quote.feasible,
  code: quote.code,
  physicalLatestBangStart: quote.physicalLatestStart,
  promise: quote.promise,
});
if (quote.feasible) process.exitCode = 1;

// 3. Restore original receipt; bang chair is not free until minute 2.
({ service } = freshService('chair'));
seedBase(service);
service.completeSection('perm', 'last', { at: 0, stylistId: 'lin' });
service.updateChair('chair2', { unavailable: [{ start: 0, end: 2, reason: '前一位客人到第 2 分才离开' }] });
quote = bangQuote(service);
show('变异二：工序回条恢复，刘海椅第 2 分才空', {
  feasible: quote.feasible,
  code: quote.code,
  chairConflict: quote.fixedConflicts?.[0] ?? null,
});
if (quote.feasible) process.exitCode = 1;

// 4. Minimal one unroller, then temporary substitute.
({ service } = freshService('substitute'));
seedBase(service);
service.updateStaffSchedule('lin', { timeOff: [{ start: 3, end: 5, reason: '临时替班/离开' }] });
let recovery = service.recover({ at: 3 });
show('最小案例：只有林师傅能拆卷，第 3 分扰动', recovery.rescueOrder);

service.addStaff({ id: 'zhou', name: '周师傅', canUnroll: true });
recovery = service.recover({ at: 3 });
show('加一位可拆卷师傅后的替班方案', recovery.rescueOrder);

const handoff = service.acceptHandoff('perm', {
  at: 3,
  stylistId: 'zhou',
  confirmState: true,
  acceptCompletedSections: true,
  chairId: 'chair1',
  customerConsent: true,
  consentSignature: '第一位客人',
});
service.startUnroll('perm', { at: 3, stylistId: 'zhou' });
service.completeUnroll('perm', { at: 5, stylistId: 'zhou' });
const checkout = service.checkout('perm', { at: 6, amount: 680 });
show('周师傅确认分区状态并替班后结账', { status: checkout.status, payments: checkout.appointment.payments });

console.log('\nDemo complete. Exit code:', process.exitCode ?? 0);
