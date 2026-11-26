import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApp } from './src/app.mjs';
import { JsonStore } from './src/store.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT || 4178);
const host = process.env.HOST || '127.0.0.1';
const dataFile = process.env.DATA_FILE || path.join(__dirname, 'data', 'club-state.json');

const store = new JsonStore(dataFile);
store.save();
const app = createApp({ store });
const server = http.createServer(app);

server.listen(port, host, () => {
  console.log(`棋社裁判服务已启动: http://${host}:${port}`);
  console.log(`数据文件: ${dataFile}`);
  console.log(`已受理对局 ${Object.keys(store.state.games).length} 盘，联赛结果 ${Object.keys(store.state.results).length} 份`);
});

function shutdown(signal) {
  console.log(`收到 ${signal}，保存后关闭...`);
  store.save();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 500).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
