'use strict';

const path = require('node:path');
const { JsonStore } = require('./json-store');
const { seedState } = require('./seed');
const { PlatformService } = require('./service');
const { createApp } = require('./app');

const dataFile = process.env.DATA_FILE
  ? path.resolve(process.env.DATA_FILE)
  : path.join(__dirname, '..', 'data', 'platform.json');

if (process.env.RESET_DATA === '1') {
  const fs = require('node:fs');
  try {
    fs.rmSync(dataFile, { force: true });
  } catch {
    // ignore reset failures; constructor will surface real permission errors
  }
}

const store = new JsonStore(dataFile, seedState);
const service = new PlatformService(store);
const server = createApp(service);
const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || '0.0.0.0';

server.listen(port, host, () => {
  const address = server.address();
  const actualPort = typeof address === 'object' && address ? address.port : port;
  console.log(JSON.stringify({ event: 'listening', host, port: actualPort, dataFile }));
});

function shutdown(signal) {
  console.log(JSON.stringify({ event: 'shutdown', signal }));
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 3000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

module.exports = { server, service, store, dataFile };
