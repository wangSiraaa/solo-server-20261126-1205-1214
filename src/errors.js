export class ApiError extends Error {
  constructor(status, code, message, details = undefined) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const badRequest = (code, message, details) => new ApiError(400, code, message, details);
export const notFound = (code, message, details) => new ApiError(404, code, message, details);
export const conflict = (code, message, details) => new ApiError(409, code, message, details);
