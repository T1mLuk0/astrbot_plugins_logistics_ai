"""Minimal dependency stubs used only when AstrBot or aiohttp is unavailable."""

from __future__ import annotations

import logging
import sys
import types
from typing import Any


def install_test_stubs() -> None:
    """Install import-only stubs without replacing real installed packages."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        _install_aiohttp_stub()

    try:
        import astrbot  # noqa: F401
    except ImportError:
        _install_astrbot_stub()


def _install_aiohttp_stub() -> None:
    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class ClientSession:
        closed = False

    class ClientTimeout:
        def __init__(self, **_: Any) -> None:
            pass

    class TCPConnector:
        def __init__(self, **_: Any) -> None:
            pass

    aiohttp.ClientError = ClientError
    aiohttp.ClientSession = ClientSession
    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.TCPConnector = TCPConnector
    sys.modules["aiohttp"] = aiohttp


def _install_astrbot_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")

    class AstrBotConfig(dict[str, Any]):
        pass

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context: Context) -> None:
            self.context = context

    class Plain:
        def __init__(self, text: str = "") -> None:
            self.text = text

    class Image:
        def __init__(self, url: str = "") -> None:
            self.url = url

    class File:
        def __init__(self, url: str = "") -> None:
            self.url = url

    class Reply:
        def __init__(
            self,
            *,
            id: str = "",
            chain: list[Any] | None = None,
            sender_id: str = "",
            sender_nickname: str = "",
            time: int | float | str | None = None,
            message_str: str = "",
        ) -> None:
            self.id = id
            self.chain = chain or []
            self.sender_id = sender_id
            self.sender_nickname = sender_nickname
            self.time = time
            self.message_str = message_str

    class EventMessageType:
        GROUP_MESSAGE = "group_message"

    class Filter:
        @staticmethod
        def event_message_type(_: Any):
            return lambda function: function

    Filter.EventMessageType = EventMessageType

    def register(*_: Any, **__: Any):
        return lambda plugin_class: plugin_class

    test_logger = logging.getLogger("astrbot-test")
    test_logger.addHandler(logging.NullHandler())
    test_logger.propagate = False
    api.AstrBotConfig = AstrBotConfig
    api.logger = test_logger
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = Filter()
    components.File = File
    components.Image = Image
    components.Plain = Plain
    components.Reply = Reply
    star.Context = Context
    star.Star = Star
    star.register = register

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.message_components"] = components
    sys.modules["astrbot.api.star"] = star
