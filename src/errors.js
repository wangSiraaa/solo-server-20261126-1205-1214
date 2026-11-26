'use strict';

class HttpError extends Error {
  constructor(status, code, message, details = undefined) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function badRequest(code, message, details) {
  return new HttpError(400, code, message, details);
}

function unauthorized(code, message, details) {
  return new HttpError(401, code, message, details);
}

function forbidden(code, message, details) {
  return new HttpError(403, code, message, details);
}

function notFound(code, message, details) {
  return new HttpError(404, code, message, details);
}

function conflict(code, message, details) {
  return new HttpError(409, code, message, details);
}

function unprocessable(code, message, details) {
  return new HttpError(422, code, message, details);
}

module.exports = {
  HttpError,
  badRequest,
  unauthorized,
  forbidden,
  notFound,
  conflict,
  unprocessable,
};
