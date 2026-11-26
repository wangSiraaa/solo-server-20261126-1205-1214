import { startDefaultServer } from './src/server.js';

startDefaultServer().catch((error) => {
  console.error(error);
  process.exit(1);
});
