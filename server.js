import path from 'node:path';
import { createServer } from './src/server.js';

const port = Number(process.env.PORT ?? 3000);
const host = process.env.HOST ?? '0.0.0.0';
const dataFile =
  process.env.DATA_FILE === ':memory:'
    ? ':memory:'
    : process.env.DATA_FILE
      ? path.resolve(process.cwd(), process.env.DATA_FILE)
      : path.join(process.cwd(), 'data', 'phonics-review-room.json');

const { server } = createServer({ dataFile });
server.listen(port, host, () => {
  console.log(`Phonics Review Room listening on http://${host}:${port}`);
  console.log(`Data file: ${dataFile}`);
});
