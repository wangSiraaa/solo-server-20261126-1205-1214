// File-backed in-memory store. Every entity lives in a Map; mutations call
// save() to atomically persist. Accounting is always *derived* from stored
// bookings (never incremented), so reloads and re-reports cannot double count.

import fs from 'node:fs';
import path from 'node:path';
import { clone, uid, nowIso } from './util.js';

function emptyState() {
  return {
    version: 1,
    households: {},
    applications: {},        // householdId -> active application
    applicationHistory: {},  // householdId -> [application versions]
    contracts: {},
    bookings: {},
    statements: {},          // statementId -> statement
    events: [],              // audit log (append-only, informational)
    meta: { createdAt: nowIso() },
  };
}

export function createStore({ file = null } = {}) {
  let state = emptyState();

  function load() {
    if (!file) return;
    try {
      const raw = fs.readFileSync(file, 'utf8');
      state = JSON.parse(raw);
      state.events ??= [];
      state.statements ??= {};
      state.applicationHistory ??= {};
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
      state = emptyState();
    }
  }

  let saveTimer = null;
  function save({ immediate = false } = {}) {
    if (!file) return;
    const write = () => {
      const dir = path.dirname(file);
      fs.mkdirSync(dir, { recursive: true });
      const tmp = `${file}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(state, null, 2));
      fs.renameSync(tmp, file);
    };
    if (immediate) {
      if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
      write();
      return;
    }
    if (saveTimer) return;
    saveTimer = setTimeout(() => { saveTimer = null; write(); }, 10);
  }

  function flush() { if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    if (file) { fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, JSON.stringify(state, null, 2)); } }

  function logEvent(type, payload) {
    state.events.push({ id: uid('evt'), at: nowIso(), type, payload: clone(payload) });
  }

  load();
  return {
    get state() { return state; },
    load,
    save,
    flush,
    logEvent,
    emptyState,
  };
}

export { emptyState };
