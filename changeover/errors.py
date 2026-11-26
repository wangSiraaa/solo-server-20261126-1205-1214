"""Uniform error type carried by the HTTP layer."""


class ApiError(Exception):
    """An error that maps cleanly onto an HTTP status + JSON body."""

    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details or {}

    def to_dict(self):
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body
