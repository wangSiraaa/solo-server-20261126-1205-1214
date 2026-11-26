import { Observatory } from '../src/domain.js';

const obs = new Observatory();
const say = (title, value) => {
  console.log(`\n## ${title}`);
  console.log(typeof value === 'string' ? value : JSON.stringify(value, null, 2));
};

obs.registerStation({ id: 'S1', name: '东河站', agreedOffset: 0 });
obs.registerStation({ id: 'S2', name: '西岭站', agreedOffset: 0 });
obs.createCommission({
  id: 'C3',
  phenomenonInterval: [4, 8],
  requiredCommonSeconds: 3,
  stationIds: ['S1', 'S2'],
  background: { before: 1, after: 1 },
});
obs.submitFrame('S1', { id: 'T1', mode: 'target', localInterval: [4, 8] });
obs.submitFrame('S2', { id: 'T2', mode: 'target', localInterval: [5, 8] });
obs.submitFrame('S1', { id: 'B1', mode: 'background', localInterval: [3, 4] });
obs.submitFrame('S2', { id: 'B2', mode: 'background', localInterval: [4, 5] });

say('初始：两站交集应为 [5,8)，恰好 3 秒', obs.evaluateCommission('C3'));

obs.setClock('S2', { agreedOffset: 1 });
say('S2 对时偏差 +1：曝光 [6,9)，公共天象段内只剩 [6,8) 两秒', {
  mappedS2: obs.evaluateCommission('C3').stationEvidence[1].targetIntervals,
  commonIntervals: obs.evaluateCommission('C3').commonIntervals,
  commonSeconds: obs.evaluateCommission('C3').commonSeconds,
});

obs.setClock('S2', { agreedOffset: 0 });
obs.updateCommission('C3', { requiredCommonSeconds: 4 });
say('恢复时钟并把委托改成 4 秒：不得沿用 3 秒合格结论', {
  commonSeconds: obs.evaluateCommission('C3').commonSeconds,
  requiredCommonSeconds: obs.evaluateCommission('C3').requiredCommonSeconds,
  qualified: obs.evaluateCommission('C3').qualified,
  reasons: obs.evaluateCommission('C3').reasons,
});

obs.createCommission({
  id: 'STAG',
  phenomenonInterval: [30, 40],
  requiredCommonSeconds: 3,
  stationIds: ['S1', 'S2'],
  background: { before: 1, after: 1 },
});
obs.submitFrame('S1', { mode: 'target', localInterval: [31, 34] });
obs.submitFrame('S2', { mode: 'target', localInterval: [36, 39] });
obs.submitFrame('S1', { mode: 'background', localInterval: [30, 31] });
obs.submitFrame('S2', { mode: 'background', localInterval: [39, 40] });
say('两份各 3 秒但错开：不能相加', ((e) => ({
  individualTotalSeconds: e.individualTotalSeconds,
  commonSeconds: e.commonSeconds,
  additiveSecondsRejected: e.additiveSecondsRejected,
  closeResult: obs.closeCommission('STAG').result,
}))(obs.evaluateCommission('STAG')));

console.log('\n内置完整网络服务剧本请运行 `npm start`，然后打开 http://localhost:3000/reader.html?commissionId=C-DONE');
