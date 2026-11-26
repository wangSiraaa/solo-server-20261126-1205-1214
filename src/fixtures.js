import { DomainError } from './election.js';

export function scenarioDefinition(name, overrides = {}) {
  switch (name) {
    case 'transfer':
    case 'original-four':
      return originalFour({ backup: 'yi', yiResponse: 'willing', ...overrides });
    case 'ineligible-backup':
      return originalFour({ backup: 'bing', ...overrides });
    case 'refused-backup':
      return originalFour({ backup: 'yi', yiResponse: 'refused', ...overrides });
    case 'both-willing':
      return bothWilling(overrides);
    case 'overquota':
      return overQuota(overrides);
    default:
      throw new DomainError(404, 'SCENARIO_NOT_FOUND', `未知演示场景 ${name}`);
  }
}

function originalFour({ backup = 'yi', yiResponse = 'willing', jiaWilling = ['reading'], preference = 'reading' } = {}) {
  const yiCommitment =
    yiResponse === 'refused'
      ? {
          willingSeatIds: ['court'],
          preferredSeatId: 'court',
          seatResponses: { court: 'refused' },
        }
      : { willingSeatIds: ['court'], preferredSeatId: 'court' };

  return {
    name: '四票开局：甲阅览，球场票甲转乙/丙',
    candidates: [
      {
        id: 'jia',
        name: '甲',
        qualifiedSeatIds: ['reading', 'court'],
        commitment: { willingSeatIds: jiaWilling, preferredSeatId: preference },
      },
      {
        id: 'yi',
        name: '乙',
        qualifiedSeatIds: ['court'],
        commitment: yiCommitment,
      },
      {
        id: 'bing',
        name: '丙',
        // 有意不给 court 资格；保留 reading 资格以证明只是球场不符。
        qualifiedSeatIds: ['reading'],
        commitment: { willingSeatIds: ['reading', 'court'], preferredSeatId: 'reading' },
      },
    ],
    ballots: [
      { id: 'b1', voterId: 'v1', voterName: '街坊一', choices: [{ personId: 'jia', seatId: 'reading' }] },
      { id: 'b2', voterId: 'v2', voterName: '街坊二', choices: [{ personId: 'jia', seatId: 'reading' }] },
      {
        id: 'b3',
        voterId: 'v3',
        voterName: '街坊三',
        choices: [
          { personId: 'jia', seatId: 'court' },
          { personId: backup, seatId: 'court' },
        ],
      },
      {
        id: 'b4',
        voterId: 'v4',
        voterName: '街坊四',
        choices: [
          { personId: 'jia', seatId: 'court' },
          { personId: backup, seatId: 'court' },
        ],
      },
    ],
  };
}

function bothWilling({ preference = 'reading', withBackup = false, extraReadingTicket = false } = {}) {
  const ballots = [
    { id: 'b1', voterId: 'v1', choices: [{ personId: 'jia', seatId: 'reading' }] },
    { id: 'b2', voterId: 'v2', choices: [{ personId: 'jia', seatId: 'reading' }] },
    {
      id: 'b3',
      voterId: 'v3',
      choices: withBackup
        ? [
            { personId: 'jia', seatId: 'court' },
            { personId: 'yi', seatId: 'court' },
          ]
        : [{ personId: 'jia', seatId: 'court' }],
    },
    {
      id: 'b4',
      voterId: 'v4',
      choices: withBackup
        ? [
            { personId: 'jia', seatId: 'court' },
            { personId: 'yi', seatId: 'court' },
          ]
        : [{ personId: 'jia', seatId: 'court' }],
    },
  ];
  if (extraReadingTicket) {
    ballots.push({ id: 'b5', voterId: 'v5', choices: [{ personId: 'jia', seatId: 'reading' }] });
  }

  return {
    name: '甲愿任两席但只能任一',
    candidates: [
      {
        id: 'jia',
        name: '甲',
        qualifiedSeatIds: ['reading', 'court'],
        commitment: { willingSeatIds: ['reading', 'court'], preferredSeatId: preference },
      },
      {
        id: 'yi',
        name: '乙',
        qualifiedSeatIds: ['court'],
        commitment: { willingSeatIds: ['court'], preferredSeatId: 'court' },
      },
    ],
    ballots,
  };
}

function overQuota() {
  return {
    name: '同项三票超过两票定额',
    candidates: [
      {
        id: 'jia',
        name: '甲',
        qualifiedSeatIds: ['reading'],
        commitment: { willingSeatIds: ['reading'], preferredSeatId: 'reading' },
      },
      {
        id: 'yi',
        name: '乙',
        qualifiedSeatIds: ['court'],
        commitment: { willingSeatIds: ['court'], preferredSeatId: 'court' },
      },
    ],
    ballots: [
      { id: 'b1', voterId: 'v1', choices: [{ personId: 'jia', seatId: 'reading' }] },
      { id: 'b2', voterId: 'v2', choices: [{ personId: 'jia', seatId: 'reading' }] },
      { id: 'b3', voterId: 'v3', choices: [{ personId: 'jia', seatId: 'reading' }] },
      { id: 'b4', voterId: 'v4', choices: [{ personId: 'yi', seatId: 'court' }] },
      { id: 'b5', voterId: 'v5', choices: [{ personId: 'yi', seatId: 'court' }] },
    ],
  };
}

/** Seed a scenario through store commands so IDs/seq and validation stay real. */
export function seedScenario(store, scenarioName, options = {}) {
  const definition = scenarioDefinition(scenarioName, options);
  const round = store.createRound({ id: options.roundId, name: options.name ?? definition.name }).body;
  for (const candidate of definition.candidates) store.putCandidate(round.id, candidate);
  for (const ballot of definition.ballots) store.addBallot(round.id, ballot);
  return store.getRound(round.id);
}
