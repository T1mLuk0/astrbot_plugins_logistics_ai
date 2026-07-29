"""LogisticsAI API 异常定义。"""

class LogisticsAIError(Exception):
    """LogisticsAI 插件基础异常。"""

class LogisticsAIConfigurationError(LogisticsAIError):
    """插件配置不正确。"""

class LogisticsAIRequestError(LogisticsAIError):
    """API 请求失败。"""

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