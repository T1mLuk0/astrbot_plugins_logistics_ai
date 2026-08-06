"""Exceptions raised by the LogisticsAI AstrBot plugin."""


class LogisticsAIError(Exception):
    """Base exception for the LogisticsAI plugin."""


class LogisticsAIConfigurationError(LogisticsAIError):
    """Raised when the plugin configuration is invalid."""


class LogisticsAIRequestError(LogisticsAIError):
    """Raised when a LogisticsAI backend request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class LogisticsAIAnalysisError(LogisticsAIError):
    """Raised when AstrBot's current multimodal model cannot analyze a message."""

