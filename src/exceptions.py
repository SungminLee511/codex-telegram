"""Custom exceptions for Codex Telegram Bot."""


class CodexTelegramError(Exception):
    """Base exception for Codex Telegram Bot."""


class ConfigurationError(CodexTelegramError):
    """Configuration-related errors."""


class MissingConfigError(ConfigurationError):
    """Required configuration is missing."""


class InvalidConfigError(ConfigurationError):
    """Configuration is invalid."""


class SecurityError(CodexTelegramError):
    """Security-related errors."""


class AuthenticationError(SecurityError):
    """Authentication failed."""


class AuthorizationError(SecurityError):
    """Authorization failed."""


class DirectoryTraversalError(SecurityError):
    """Directory traversal attempt detected."""


class CodexError(CodexTelegramError):
    """Codex-related errors."""


class CodexTimeoutError(CodexError):
    """Codex operation timed out."""


class CodexProcessError(CodexError):
    """Codex process execution failed."""


class CodexParsingError(CodexError):
    """Failed to parse Codex output."""


class StorageError(CodexTelegramError):
    """Storage-related errors."""


class DatabaseConnectionError(StorageError):
    """Database connection failed."""


class DataIntegrityError(StorageError):
    """Data integrity check failed."""


class TelegramError(CodexTelegramError):
    """Telegram API-related errors."""


class MessageTooLongError(TelegramError):
    """Message exceeds Telegram's length limit."""


class RateLimitError(TelegramError):
    """Rate limit exceeded."""


class RateLimitExceeded(RateLimitError):
    """Rate limit exceeded (alias for compatibility)."""
