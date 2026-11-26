'use strict';

const { createServer } = require('./server');
const { CampaignService } = require('./service');
const path = require('path');

const port = Number(process.env.PORT || 3000);
const stateFile = process.env.STATE_FILE || path.join(__dirname, '..', 'data', 'state.json');

const service = CampaignService.open(stateFile);
const server = createServer({ service });

// 允许通过环境变量自动灌入示例规则/库存，方便手工体验。
if (process.env.SEED === '1' && Object.keys(service.state.rulebooks).length === 0) {
  const { seedBase } = require('./fixtures');
  seedBase(service);
}

server.listen(port, () => {
  // eslint-disable-next-line no-console
  console.log(`战役借还后台已启动：http://localhost:${port}  (state: ${stateFile})`);
});

module.exports = server;
