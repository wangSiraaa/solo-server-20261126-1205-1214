import { createStore } from './store.js';
import { createServer } from './api.js';

const port = Number(process.env.PORT ?? 3000);
const file = process.env.DATA_FILE ?? null; // e.g. ./data/state.json

const store = createStore({ file });
const server = createServer(store);

server.listen(port, () => {
  // eslint-disable-next-line no-console
  console.log(`exchange-homestay listening on :${port}${file ? ` (data: ${file})` : ''}`);
});

function shutdown() {
  try { store.flush(); } catch { /* ignore */ }
  server.close(() => process.exit(0));
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
