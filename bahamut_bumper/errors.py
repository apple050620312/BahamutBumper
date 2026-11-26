"""Domain errors shared by the application."""

from __future__ import annotations

class ConfigurationError(RuntimeError):
    """The daemon cannot safely run with its current configuration."""


class AuthenticationError(RuntimeError):
    """The exported Bahamut login is absent, expired, or is the wrong user."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class MobileLoginUnavailable(AuthenticationError):
    """The mobile endpoint is unavailable; a verified forum session may work."""


class SafetyError(RuntimeError):
    """Page state is ambiguous, so a write operation must not continue."""
