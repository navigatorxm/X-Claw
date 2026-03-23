"""
Structured logging with JSON output and request ID correlation.

Features:
  - JSON log format for machine parsing
  - Request ID propagation across all components
  - Log levels: DEBUG, INFO, WARN, ERROR
  - Stack trace capture for errors
  - Structured fields: timestamp, agent_id, action, request_id, status
  - Performance metrics (latency, throughput)
"""

import json
import logging
import sys
import traceback
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pathlib import Path


class LogLevel(str, Enum):
    """Log severity levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs JSON for easy parsing."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add request_id if present in record
        if hasattr(record, "request_id") and record.request_id:
            log_data["request_id"] = record.request_id

        # Add custom fields
        if hasattr(record, "custom_fields"):
            log_data.update(record.custom_fields)

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exc(),
            }

        return json.dumps(log_data)


class StructuredLogger:
    """
    Structured logger with request ID correlation and JSON output.

    Usage:
        logger = StructuredLogger(__name__)
        logger.info("User action", agent_id="a1", action="trade", request_id="req_123")
        logger.error("Trade failed", reason="insufficient_balance", request_id="req_123")
    """

    def __init__(self, name: str):
        """Initialize logger."""
        self.logger = logging.getLogger(name)

    def _log(
        self,
        level: LogLevel,
        message: str,
        request_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        latency_ms: Optional[float] = None,
        error: Optional[str] = None,
        exception: Optional[Exception] = None,
        **kwargs,
    ) -> None:
        """
        Internal logging method with structured fields.

        Args:
            level: Log level
            message: Log message
            request_id: Unique request identifier
            agent_id: Agent performing action
            action: Action name (e.g., "execute_trade", "approve_request")
            status: Status (e.g., "success", "denied", "error")
            latency_ms: Request latency in milliseconds
            error: Error message
            exception: Exception object for traceback
            **kwargs: Additional fields to include
        """
        # Build custom fields
        fields = {}
        if agent_id:
            fields["agent_id"] = agent_id
        if action:
            fields["action"] = action
        if status:
            fields["status"] = status
        if latency_ms is not None:
            fields["latency_ms"] = latency_ms
        if error:
            fields["error"] = error
        fields.update(kwargs)

        # Create log record
        record = self.logger.makeRecord(
            name=self.logger.name,
            level=getattr(logging, level.value),
            fn="",
            lnum=0,
            msg=message,
            args=(),
            exc_info=exception if exception else None,
        )

        # Attach request_id and custom fields
        record.request_id = request_id
        record.custom_fields = fields

        # Log
        self.logger.handle(record)

    def debug(
        self,
        message: str,
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Log debug message."""
        self._log(LogLevel.DEBUG, message, request_id=request_id, **kwargs)

    def info(
        self,
        message: str,
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Log info message."""
        self._log(LogLevel.INFO, message, request_id=request_id, **kwargs)

    def warn(
        self,
        message: str,
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Log warning message."""
        self._log(LogLevel.WARN, message, request_id=request_id, **kwargs)

    def error(
        self,
        message: str,
        request_id: Optional[str] = None,
        exception: Optional[Exception] = None,
        **kwargs,
    ) -> None:
        """Log error message with optional exception."""
        self._log(
            LogLevel.ERROR,
            message,
            request_id=request_id,
            exception=exception,
            **kwargs,
        )


# Global logger registry
_loggers = {}


def get_logger(name: str) -> StructuredLogger:
    """
    Get or create a structured logger by name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        StructuredLogger instance
    """
    if name not in _loggers:
        _loggers[name] = StructuredLogger(name)
    return _loggers[name]


def setup_logging(
    level: LogLevel = LogLevel.INFO,
    log_file: Optional[str] = None,
) -> None:
    """
    Configure structured logging for the application.

    Args:
        level: Minimum log level
        log_file: Optional file path for log output (in addition to stdout)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.value))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Console output (JSON)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(console_handler)

    # File output (if specified)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(JSONFormatter())
        root_logger.addHandler(file_handler)


# Error categorization
class ErrorCategory(str, Enum):
    """Error categories for structured error logging."""
    VALIDATION = "validation_error"
    AUTHENTICATION = "authentication_error"
    AUTHORIZATION = "authorization_error"
    NOT_FOUND = "not_found_error"
    CONFLICT = "conflict_error"
    TIMEOUT = "timeout_error"
    RATE_LIMIT = "rate_limit_error"
    EXECUTION = "execution_error"
    EXTERNAL_SERVICE = "external_service_error"
    INTERNAL = "internal_error"


def categorize_error(exception: Exception) -> ErrorCategory:
    """
    Categorize an exception for structured error logging.

    Args:
        exception: The exception to categorize

    Returns:
        ErrorCategory enum value
    """
    exc_name = type(exception).__name__

    if "Timeout" in exc_name:
        return ErrorCategory.TIMEOUT
    elif "RateLimit" in exc_name:
        return ErrorCategory.RATE_LIMIT
    elif "Validation" in exc_name or "ValidationError" in exc_name:
        return ErrorCategory.VALIDATION
    elif "Auth" in exc_name:
        return ErrorCategory.AUTHENTICATION
    elif "Permission" in exc_name or "Forbidden" in exc_name:
        return ErrorCategory.AUTHORIZATION
    elif "NotFound" in exc_name:
        return ErrorCategory.NOT_FOUND
    elif "Conflict" in exc_name:
        return ErrorCategory.CONFLICT
    elif "Execution" in exc_name:
        return ErrorCategory.EXECUTION
    else:
        return ErrorCategory.INTERNAL
