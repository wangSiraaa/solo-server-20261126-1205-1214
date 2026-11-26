export class HttpError extends Error {
  constructor(status, code, message, details = undefined) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const badRequest = (code, message, details) =>
  new HttpError(400, code, message, details);
export const notFound = (code, message, details) =>
  new HttpError(404, code, message, details);
export const conflict = (code, message, details) =>
  new HttpError(409, code, message, details);
export const unprocessable = (code, message, details) =>
  new HttpError(422, code, message, details);
