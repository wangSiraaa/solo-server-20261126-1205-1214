"""Domain errors.

Every error carries a stable machine-readable ``code`` so the HTTP layer and
tests can assert on intent rather than on message text.
"""

from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"ok": False, "error": self.code, "message": self.message, "details": self.details}


class NotFound(DomainError):
    def __init__(self, what: str, ident: str):
        super().__init__("NOT_FOUND", f"{what} not found: {ident}", {"what": what, "id": ident})


class ValidationError(DomainError):
    def __init__(self, message: str, details=None):
        super().__init__("VALIDATION", message, details)


class Conflict(DomainError):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(code, message, details)
