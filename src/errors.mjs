export class DomainError extends Error {
  constructor(code, message, statusCode = 409, details = undefined) {
    super(message);
    this.name = 'DomainError';
    this.code = code;
    this.statusCode = statusCode;
    this.details = details;
  }
}

export function badRequest(code, message, details) {
  return new DomainError(code, message, 400, details);
}

export function notFound(code, message, details) {
  return new DomainError(code, message, 404, details);
}

export function conflict(code, message, details) {
  return new DomainError(code, message, 409, details);
}
