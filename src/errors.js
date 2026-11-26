'use strict';

/**
 * 领域错误：每个错误带一个稳定的 code，方便 HTTP 层映射成状态码，
 * 也方便测试对“为什么被拒”做精确断言。
 */
class DomainError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'DomainError';
    this.code = code;
    this.details = details;
  }
}

const codes = {
  VALIDATION: 'VALIDATION',
  NOT_FOUND: 'NOT_FOUND',
  CONFLICT: 'CONFLICT',
  BOX_UNAVAILABLE: 'BOX_UNAVAILABLE',
  TEAM_BUSY: 'TEAM_BUSY',
  LOAN_CLOSED: 'LOAN_CLOSED',
  NO_LEGAL_CONTINUATION: 'NO_LEGAL_CONTINUATION',
  DOOR_ILLEGAL: 'DOOR_ILLEGAL',
  UNREVEALED_PLOT_UNAVAILABLE: 'UNREVEALED_PLOT_UNAVAILABLE',
  INCOMPATIBLE_BOX: 'INCOMPATIBLE_BOX',
  INCOMPATIBLE_POUCH: 'INCOMPATIBLE_POUCH',
  PLOT_REWRITE: 'PLOT_REWRITE',
  ACTION_NOT_ALLOWED: 'ACTION_NOT_ALLOWED',
  IRREVERSIBLE: 'IRREVERSIBLE',
};

function validation(message, details) { return new DomainError(codes.VALIDATION, message, details); }
function notFound(message, details) { return new DomainError(codes.NOT_FOUND, message, details); }
function conflict(message, details) { return new DomainError(codes.CONFLICT, message, details); }

module.exports = { DomainError, codes, validation, notFound, conflict };
