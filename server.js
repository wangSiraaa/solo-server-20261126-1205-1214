import { createServer } from './src/server.js';

const port = Number(process.env.PORT || process.env.SERVER_PORT || 3000);
const host = process.env.HOST || '0.0.0.0';
const { observatory, server } = createServer({ seed: process.env.SEED !== 'false' });

server.listen(port, host, () => {
  console.log(`掩星联合值守服务已启动: http://${host}:${port}`);
  console.log(`读片员入口示例: http://localhost:${port}/reader.html?commissionId=C-DONE`);
  console.log(JSON.stringify(observatory.publicState()));
});

export { createServer };
