import { createServer } from './src/app.js';

const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || '0.0.0.0';
const scenario = process.env.SCENARIO || 'pre-split';

const server = createServer({ defaultScenario: scenario });

server.listen(port, host, () => {
  console.log(`方言社词典/制册服务已启动：http://127.0.0.1:${port}`);
  console.log(`API 目录：http://127.0.0.1:${port}/api`);
  console.log(`初始剧本：${scenario}（可 POST /api/admin/reset 重置）`);
});

function shutdown(signal) {
  console.log(`\n收到 ${signal}，正在关闭服务...`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 3000).unref();
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
