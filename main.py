"""Transport AstrBot messages to the LogisticsAI backend.

The plugin deliberately keeps platform-specific details at the edge.  It can
receive AstrBot events from both NapCat/OneBot and the QQ official adapter,
while the backend only sees one normalized message contract.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Reply
from astrbot.api.star import Context, Star, register

from .api import ApiClient
from .models import UploadReceipt


@register(
    "astrbot_plugin_logistics_ai",
    "LogisticsAI",
    "Upload original messages and route native media replies to LogisticsAI",
    "0.9.0",
)
class LogisticsAIPlugin(Star):
    """Transport messages and silently intercept native AstrBot replies."""

    # Keep an event-to-upload association only long enough for AstrBot's native
    # provider to produce its result.  A provider may decide not to answer a
    # text message, so the decorating hook is not guaranteed to run for every
    # captured event.
    _MEDIA_CONTEXT_TTL_SECONDS = 600.0

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        """Initialize clients while remaining compatible with optional config."""
        super().__init__(context)
        self.config: AstrBotConfig | dict[str, Any] = (
            config if config is not None else {}
        )
        self.api_client = ApiClient(self.config)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._processing_tasks: dict[str, asyncio.Task[Any]] = {}
        self._seen_message_keys: set[str] = set()
        # A result hook is not guaranteed to receive the exact same Python
        # event object on every adapter.  Keep aliases for the object id and
        # stable message/session identifiers instead of relying on id(event).
        self._media_event_context: dict[
            object,
            tuple[
                dict[str, Any],
                asyncio.Future[UploadReceipt | None] | None,
            ],
        ] = {}
        self._media_context_aliases: dict[int, set[object]] = {}
        self._media_context_cleanup_handles: dict[object, asyncio.TimerHandle] = {}
        self._terminating = False

        logger.info(
            "LogisticsAI plugin initialized. Every raw message uploads "
            "immediately; native media replies are intercepted silently.",
        )

    @filter.event_message_type(
        getattr(
            filter.EventMessageType,
            "ALL",
            filter.EventMessageType.GROUP_MESSAGE,
        )
    )
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """Capture any supported AstrBot message and schedule the workflow.

        The historical method name is retained for compatibility with older
        plugin installations, but the decorator intentionally accepts all
        event message types.  QQ official bots may expose group-at, C2C, or
        guild events instead of AstrBot's ``GROUP_MESSAGE`` value.
        """
        if self._terminating:
            return

        try:
            if self._is_self_message(event):
                return

            data = self._build_message_data(event)
            if not data["messageId"]:
                logger.warning(
                    "LogisticsAI ignored a message without a message ID. "
                    "platform=%s message_type=%s group_id=%s",
                    data["platform"],
                    data["messageType"],
                    data["groupId"],
                )
                return

            message_key = self._message_key(data)
            if message_key in self._seen_message_keys:
                logger.debug(
                    "LogisticsAI ignored a duplicate event. platform=%s "
                    "group_id=%s message_id=%s",
                    data["platform"],
                    data["groupId"],
                    data["messageId"],
                )
                return
            self._seen_message_keys.add(message_key)

            unified_msg_origin = self._unified_msg_origin(event)
            logger.info(
                "LogisticsAI event received. platform=%s message_type=%s "
                "origin=%s message_id=%s text_length=%s images=%s files=%s",
                data["platform"],
                data["messageType"],
                unified_msg_origin,
                data["messageId"],
                len(data["content"]),
                len(data["images"]),
                len(data["files"]),
            )
            upload_task = self._schedule_processing(
                data,
                unified_msg_origin,
                event=event,
            )
            if self._requires_native_multimodal(data):
                logger.info(
                    "LogisticsAI media raw upload scheduled before native MiMo. "
                    "message_id=%s",
                    data["messageId"],
                )
            if upload_task is None:
                logger.warning(
                    "LogisticsAI could not schedule the raw upload because the "
                    "plugin is stopping. message_id=%s",
                    data["messageId"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LogisticsAI failed to capture a group message.")

    @filter.on_decorating_result()
    async def on_decorating_result(
        self,
        event: AstrMessageEvent,
        result: Any | None = None,
    ) -> None:
        """Forward native multimodal replies before AstrBot sends them.

        This hook runs after AstrBot's own provider has produced the final
        message chain and immediately before the platform adapter sends it.
        Every captured native result is suppressed: the backend owns text
        analysis, while media replies update the already-stored raw row.
        """
        context_key, context = self._pop_media_event_context(event)
        if context is None:
            logger.debug(
                "LogisticsAI decorating hook had no matching captured event. "
                "platform=%s message_id=%s",
                self._extract_platform(event, getattr(event, "message_obj", None)),
                self._event_message_id(event),
            )
            return

        data, upload_task = context
        is_multimodal = self._requires_native_multimodal(data)
        assistant_reply = self._extract_assistant_reply(event, result)
        if not is_multimodal:
            self._suppress_native_result(event, result)
            logger.info(
                "Native text reply suppressed; backend owns text analysis. "
                "message_id=%s",
                data.get("messageId", ""),
            )
            return

        # Suppress the platform send before waiting for any backend operation.
        # The native result must never leak into QQ in silent mode.
        self._suppress_native_result(event, result)
        if not assistant_reply:
            logger.warning(
                "Native multimodal result contained no text; the original raw "
                "message remains stored or uploading. "
                "message_id=%s",
                data.get("messageId", ""),
            )
            return

        if upload_task is None:
            logger.error(
                "Native multimodal reply was suppressed, but its raw upload "
                "task was not available. message_id=%s",
                data.get("messageId", ""),
            )
            return

        self._schedule_assistant_reply_after_upload(
            upload_task,
            assistant_reply,
            str(data.get("messageId") or ""),
        )
        logger.info(
            "Native multimodal reply intercepted; backend update scheduled "
            "after the raw upload receipt. message_id=%s",
            data.get("messageId", ""),
        )

    async def _resolve_platform_images(
        self,
        event: AstrMessageEvent,
        data: dict[str, Any],
    ) -> None:
        """Resolve adapter-only image identifiers through the platform API.

        Some OneBot/aiocqhttp events expose only a short file token in the
        Image component. The token is not useful to the website or the model,
        so ask the active bot for OneBot's ``get_image`` result when no local
        path or HTTP URL is available.
        """
        image_refs = self._deduplicate(data.get("images") or [])
        if not image_refs:
            return

        resolved: list[str] = []
        for image_ref in image_refs:
            # A local AstrBot path is only meaningful inside the AstrBot
            # server. Ask NapCat for the platform URL whenever the reference
            # is not already a URL/data URI, then keep the local path only as
            # a fallback for deployments that share the media directory.
            if image_ref.startswith(("http://", "https://", "data:")):
                resolved.append(image_ref)
                continue

            platform_reference = await self._get_platform_image(
                event,
                image_ref,
            )
            resolved.append(platform_reference or image_ref)

        data["images"] = self._deduplicate(resolved)

    @staticmethod
    def _is_usable_image_reference(value: str) -> bool:
        if value.startswith(("http://", "https://", "data:")):
            return True
        try:
            return Path(value).expanduser().is_file()
        except (OSError, ValueError):
            return False

    async def _get_platform_image(
        self,
        event: AstrMessageEvent,
        image_ref: str,
    ) -> str:
        """Resolve a NapCat token or an official-adapter media reference.

        NapCat exposes OneBot ``get_image`` through ``call_action``/``call_api``.
        QQ official adapters vary by AstrBot version, so also try their common
        direct resource methods.  HTTP/data references are handled before this
        method and are never sent through an adapter action.
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            getter = getattr(event, "get_bot", None)
            if callable(getter):
                try:
                    bot = getter()
                    if hasattr(bot, "__await__"):
                        bot = await bot
                except Exception:
                    bot = None

        event_candidates = [event, bot, getattr(bot, "api", None)]
        seen_candidates: set[int] = set()
        for candidate in event_candidates:
            if candidate is None or id(candidate) in seen_candidates:
                continue
            seen_candidates.add(id(candidate))

            # OneBot/NapCat action interface.
            for method_name in ("call_action", "call_api"):
                method = getattr(candidate, method_name, None)
                if not callable(method):
                    continue
                try:
                    try:
                        result = method("get_image", file=image_ref)
                    except TypeError:
                        result = method({"action": "get_image", "file": image_ref})
                    if hasattr(result, "__await__"):
                        result = await result
                    resolved = self._extract_platform_image_result(result)
                    if resolved:
                        logger.info(
                            "LogisticsAI resolved image through %s. reference=%s",
                            method_name,
                            image_ref,
                        )
                        return resolved
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(
                        "LogisticsAI image action failed. method=%s reference=%s",
                        method_name,
                        image_ref,
                        exc_info=True,
                    )

            # Official QQ adapters and newer AstrBot adapters may expose a
            # direct resource helper instead of the OneBot action API.
            for method_name in (
                "get_image",
                "get_file",
                "download_file",
                "get_resource",
            ):
                method = getattr(candidate, method_name, None)
                if not callable(method):
                    continue
                for call in (
                    lambda: method(image_ref),
                    lambda: method(file=image_ref),
                    lambda: method(url=image_ref),
                ):
                    try:
                        result = call()
                        if hasattr(result, "__await__"):
                            result = await result
                        resolved = self._extract_platform_image_result(result)
                        if resolved:
                            logger.info(
                                "LogisticsAI resolved image through %s. reference=%s",
                                method_name,
                                image_ref,
                            )
                            return resolved
                    except asyncio.CancelledError:
                        raise
                    except (TypeError, ValueError, OSError):
                        continue
                    except Exception:
                        logger.debug(
                            "LogisticsAI direct image resolver failed. "
                            "method=%s reference=%s",
                            method_name,
                            image_ref,
                            exc_info=True,
                        )

        logger.warning(
            "LogisticsAI could not resolve platform image reference. "
            "reference=%s",
            image_ref,
        )
        return ""

    @classmethod
    def _extract_platform_image_result(cls, result: Any) -> str:
        """Extract a URL or local path from common adapter responses."""
        current = result
        visited: set[int] = set()
        for _ in range(4):
            if isinstance(current, Mapping):
                marker = id(current)
                if marker in visited:
                    return ""
                visited.add(marker)
                for key in (
                    "url",
                    "download_url",
                    "file",
                    "path",
                    "local_path",
                ):
                    value = cls._to_string(current.get(key))
                    if value:
                        return value
                nested = next(
                    (
                        current.get(key)
                        for key in ("data", "result", "resource", "attachment")
                        if isinstance(current.get(key), Mapping)
                    ),
                    None,
                )
                if nested is None:
                    return ""
                current = nested
                continue
            if isinstance(current, (str, Path)):
                return cls._to_string(current)
            for attribute in ("url", "download_url", "file", "path", "local_path"):
                value = cls._to_string(getattr(current, attribute, None))
                if value:
                    return value
            return ""
        return ""

    def _schedule_processing(
        self,
        data: dict[str, Any],
        unified_msg_origin: str,
        *,
        event: AstrMessageEvent | None = None,
    ) -> asyncio.Task[UploadReceipt | None] | None:
        """Schedule one non-blocking raw message upload."""
        if self._terminating:
            return None

        message_id = data.get("messageId") or "unknown"
        task = asyncio.create_task(
            self._process_message(
                data,
                unified_msg_origin,
                event=event,
            ),
            name=f"logistics-ai-process-{message_id}",
        )
        self._processing_tasks[self._message_key(data)] = task
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        if event is not None:
            self._register_media_event_context(
                event,
                data,
                task,
                unified_msg_origin,
            )
        return task

    def _register_media_event_context(
        self,
        event: AstrMessageEvent,
        data: dict[str, Any],
        upload_task: asyncio.Future[UploadReceipt | None] | None,
        unified_msg_origin: str,
    ) -> None:
        """Store context aliases and schedule their expiry."""
        self._remember_media_event_context(
            event,
            data,
            upload_task,
            unified_msg_origin,
        )
        try:
            loop = asyncio.get_running_loop()
            aliases = getattr(self, "_media_context_aliases", {}).get(
                id(data),
                set(),
            )
            cleanup_handles = getattr(
                self,
                "_media_context_cleanup_handles",
                {},
            )
            for context_key in aliases:
                previous_handle = cleanup_handles.pop(context_key, None)
                if previous_handle is not None:
                    previous_handle.cancel()
                cleanup_handles[context_key] = loop.call_later(
                    self._MEDIA_CONTEXT_TTL_SECONDS,
                    self._expire_media_event_context,
                    context_key,
                )
        except RuntimeError:
            # Scheduling always runs inside AstrBot's event loop.  Keep this
            # fallback for unit tests or custom hosts without one.
            pass

    def _expire_media_event_context(self, context_key: object) -> None:
        """Drop an event association when no native result arrives in time."""
        cleanup_handles = getattr(
            self,
            "_media_context_cleanup_handles",
            {},
        )
        cleanup_handle = cleanup_handles.pop(context_key, None)
        if cleanup_handle is not None:
            cleanup_handle.cancel()
        context = self._media_event_context.pop(context_key, None)
        if context is not None:
            self._clear_media_context_aliases(context[0])
            logger.debug(
                "LogisticsAI expired an unanswered native event context. "
                "message_id=%s",
                context[0].get("messageId", ""),
            )

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """Release a completed task and observe any unexpected exception."""
        self._tasks.discard(task)
        for key, processing_task in tuple(self._processing_tasks.items()):
            if processing_task is task:
                self._processing_tasks.pop(key, None)
                break
        if task.cancelled():
            return

        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return

        if exception is not None:
            logger.error(
                "LogisticsAI background task ended unexpectedly: %s",
                exception,
            )

    async def _process_message(
        self,
        data: dict[str, Any],
        unified_msg_origin: str,
        *,
        event: AstrMessageEvent | None = None,
    ) -> UploadReceipt | None:
        """Persist only the original message and return its receipt."""
        try:
            if event is not None and (data.get("images") or data.get("files")):
                await self._resolve_platform_images(event, data)
            receipt = await self.api_client.upload(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LogisticsAI raw message upload failed. "
                "platform=%s group_id=%s message_id=%s",
                data.get("platform", "qq"),
                data.get("groupId", ""),
                data.get("messageId", ""),
            )
            return None

        if receipt is None:
            return None

        logger.info(
            "LogisticsAI raw message uploaded. platform=%s group_id=%s "
            "message_id=%s database_id=%s reply_to=%s",
            data.get("platform", "qq"),
            data.get("groupId", ""),
            data.get("messageId", ""),
            receipt.database_id,
            self._reply_message_id(data),
        )

        logger.info(
            "LogisticsAI raw transport completed. Backend queued analysis. "
            "message_id=%s database_id=%s",
            data.get("messageId", ""),
            receipt.database_id,
        )
        return receipt

    def _remember_media_event_context(
        self,
        event: AstrMessageEvent,
        data: dict[str, Any],
        task: asyncio.Future[UploadReceipt | None] | None,
        unified_msg_origin: str,
    ) -> None:
        """Store object and stable aliases for the native result hook."""
        aliases = self._context_keys(event, data, unified_msg_origin)
        aliases.add(id(event))
        data_key = id(data)
        context_aliases = getattr(self, "_media_context_aliases", {})
        context_aliases[data_key] = set(aliases)

        for alias in aliases:
            previous = self._media_event_context.pop(alias, None)
            if previous is not None:
                self._clear_media_context_aliases(previous[0])
            self._media_event_context[alias] = (data, task)

    def _pop_media_event_context(
        self,
        event: AstrMessageEvent,
    ) -> tuple[
        object | None,
        tuple[
            dict[str, Any],
            asyncio.Future[UploadReceipt | None] | None,
        ] | None,
    ]:
        """Find a captured event using object id, message id, or session origin."""
        data: dict[str, Any] | None = None
        try:
            data = self._build_message_data(event)
        except Exception:
            logger.debug("Could not normalize decorating event for correlation.", exc_info=True)

        candidates: list[object] = [id(event)]
        candidates.extend(self._context_keys(event, data or {}, self._unified_msg_origin(event)))
        for candidate in candidates:
            context = self._media_event_context.pop(candidate, None)
            if context is not None:
                self._clear_media_context_aliases(context[0])
                cleanup_handles = getattr(self, "_media_context_cleanup_handles", {})
                cleanup_handle = cleanup_handles.pop(candidate, None)
                if cleanup_handle is not None:
                    cleanup_handle.cancel()
                return candidate, context
        return None, None

    def _clear_media_context_aliases(self, data: dict[str, Any]) -> None:
        """Remove all aliases belonging to one captured message."""
        aliases_by_data = getattr(self, "_media_context_aliases", {})
        aliases = aliases_by_data.pop(id(data), set())
        cleanup_handles = getattr(self, "_media_context_cleanup_handles", {})
        for alias in aliases:
            self._media_event_context.pop(alias, None)
            cleanup_handle = cleanup_handles.pop(alias, None)
            if cleanup_handle is not None:
                cleanup_handle.cancel()

    def _context_keys(
        self,
        event: AstrMessageEvent,
        data: Mapping[str, Any],
        unified_msg_origin: str,
    ) -> set[object]:
        """Build adapter-neutral correlation aliases."""
        platform = str(data.get("platform") or self._extract_platform(event, getattr(event, "message_obj", None)))
        message_id = str(data.get("messageId") or self._event_message_id(event))
        keys: set[object] = set()
        if message_id:
            keys.add(f"message:{platform}:{message_id}")
        if unified_msg_origin:
            keys.add(f"origin:{unified_msg_origin}:{message_id or '-'}")
            if not message_id:
                keys.add(f"origin:{unified_msg_origin}")
        return keys

    @classmethod
    def _unified_msg_origin(cls, event: AstrMessageEvent) -> str:
        """Read the stable AstrBot conversation origin from an event."""
        value = cls._first_non_empty_string(
            getattr(event, "unified_msg_origin", None),
            cls._call_event_method(event, "get_unified_msg_origin"),
        )
        return value

    @classmethod
    def _event_message_id(cls, event: AstrMessageEvent) -> str:
        """Read a message/event id without requiring a concrete adapter type."""
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        raw_id = cls._first_non_empty_string(
            getattr(message_obj, "message_id", None),
            getattr(message_obj, "id", None),
            cls._mapping_value(raw_message, "message_id"),
            cls._mapping_value(raw_message, "id"),
            cls._mapping_value(raw_message, "event_id"),
            getattr(event, "message_id", None),
            getattr(event, "id", None),
        )
        platform = cls._extract_platform(event, message_obj)
        return cls._normalize_backend_identifier(raw_id, f"{platform}:message")

    @classmethod
    def _normalize_backend_identifier(
        cls,
        value: Any,
        prefix: str,
        *,
        maximum: int = 128,
    ) -> str:
        """Keep platform identifiers within the backend DTO limits.

        QQ official message IDs can be several hundred characters while the
        existing API contract allows 128.  A deterministic SHA-256 token keeps
        the ID stable across the incoming event and the decorating-result hook
        without truncating away the distinguishing suffix.
        """
        text = cls._to_string(value)
        if len(text) <= maximum:
            return text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        compact = f"{prefix}:sha256:{digest}"
        return compact[:maximum]

    @staticmethod
    def _requires_native_multimodal(data: Mapping[str, Any]) -> bool:
        """Return whether AstrBot's native provider must handle media input."""
        if data.get("images") or data.get("files"):
            return True
        reply = data.get("reply")
        return isinstance(reply, Mapping) and bool(
            reply.get("images") or reply.get("files")
        )

    @classmethod
    def _extract_assistant_reply(
        cls,
        event: AstrMessageEvent,
        result: Any | None = None,
    ) -> str:
        """Extract readable text from AstrBot's final message chain."""
        event_result = cls._call_event_method(event, "get_result")
        result = event_result or result
        if isinstance(result, str):
            return result.strip()

        chain = getattr(result, "chain", None)
        if not isinstance(chain, Sequence) or isinstance(chain, (str, bytes)):
            return ""

        text_parts: list[str] = []
        for component in chain:
            if isinstance(component, Plain):
                text = cls._to_string(getattr(component, "text", None))
                if text:
                    text_parts.append(text)
        return "".join(text_parts).strip()

    @staticmethod
    def _suppress_native_result(
        event: AstrMessageEvent,
        result: Any | None = None,
    ) -> None:
        """Prevent the intercepted native chain from reaching QQ."""
        event_result = LogisticsAIPlugin._call_event_method(
            event,
            "get_result",
        ) or result
        chain = getattr(event_result, "chain", None)
        if isinstance(chain, list):
            chain.clear()

        stop_result = getattr(event_result, "stop_event", None)
        if callable(stop_result):
            stop_result()

        stop_event = getattr(event, "stop_event", None)
        if callable(stop_event):
            stop_event()

    def _schedule_assistant_reply_upload(
        self,
        receipt: UploadReceipt,
        assistant_reply: str,
        message_id: str,
    ) -> None:
        """Forward native output without blocking AstrBot's send pipeline."""
        if self._terminating:
            return

        task = asyncio.create_task(
            self._forward_assistant_reply(receipt, assistant_reply, message_id),
            name=f"logistics-ai-assistant-reply-{message_id or 'unknown'}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _schedule_assistant_reply_after_upload(
        self,
        upload_task: asyncio.Future[UploadReceipt | None],
        assistant_reply: str,
        message_id: str,
    ) -> None:
        """Wait for the raw receipt without blocking AstrBot's send pipeline."""
        if self._terminating:
            return

        task = asyncio.create_task(
            self._forward_assistant_reply_after_upload(
                upload_task,
                assistant_reply,
                message_id,
            ),
            name=f"logistics-ai-assistant-after-raw-{message_id or 'unknown'}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def _forward_assistant_reply_after_upload(
        self,
        upload_task: asyncio.Future[UploadReceipt | None],
        assistant_reply: str,
        message_id: str,
    ) -> None:
        """Attach native media text to the already-created raw message."""
        try:
            receipt = await asyncio.shield(upload_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Could not obtain the raw upload receipt for native media text. "
                "message_id=%s",
                message_id,
            )
            return

        if receipt is None or receipt.database_id is None:
            logger.error(
                "Native media text could not be attached because the raw upload "
                "returned no database ID. message_id=%s",
                message_id,
            )
            return

        await self._forward_assistant_reply(
            receipt,
            assistant_reply,
            message_id,
        )

    async def _forward_assistant_reply(
        self,
        receipt: UploadReceipt,
        assistant_reply: str,
        message_id: str,
    ) -> None:
        try:
            await self.api_client.upload_text_analysis(
                receipt,
                assistant_reply=assistant_reply,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Native assistant reply upload failed. message_id=%s database_id=%s",
                message_id,
                receipt.database_id,
            )

    def _build_message_data(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, Any]:
        """Convert any AstrBot adapter event to the LogisticsAI payload."""
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        raw_message = getattr(message_obj, "raw_message", None)
        get_messages = getattr(event, "get_messages", None)
        components = tuple(get_messages() or ()) if callable(get_messages) else ()

        text_parts, image_urls, file_urls = self._extract_chain_content(
            components,
            include_replies=False,
        )
        reply = self._extract_reply_context(components)
        if reply is not None:
            # A quoted image is represented inside Reply.chain rather than in
            # the current top-level message chain.  Promote those references
            # into the media context so the native multimodal path is used and
            # the backend can cache the same image with the enriched raw text.
            image_urls.extend(reply.get("images") or [])
            file_urls.extend(reply.get("files") or [])

        group_id = self._first_non_empty_string(
            self._call_event_method(event, "get_group_id"),
            getattr(message_obj, "group_id", None),
            getattr(message_obj, "group_openid", None),
            getattr(message_obj, "guild_id", None),
            getattr(message_obj, "channel_id", None),
            self._mapping_value(raw_message, "group_id"),
            self._mapping_value(raw_message, "group_openid"),
            self._mapping_value(raw_message, "guild_id"),
            self._mapping_value(raw_message, "channel_id"),
            self._nested_mapping_value(raw_message, "group", "id"),
            self._nested_mapping_value(raw_message, "guild", "id"),
            self._nested_mapping_value(raw_message, "channel", "id"),
        )
        user_id = self._first_non_empty_string(
            self._call_event_method(event, "get_sender_id"),
            getattr(sender, "user_id", None),
            getattr(sender, "user_openid", None),
            getattr(sender, "openid", None),
            getattr(sender, "id", None),
            self._mapping_value(raw_message, "user_id"),
            self._mapping_value(raw_message, "user_openid"),
            self._mapping_value(raw_message, "openid"),
            self._nested_mapping_value(raw_message, "sender", "user_id"),
            self._nested_mapping_value(raw_message, "sender", "user_openid"),
            self._nested_mapping_value(raw_message, "sender", "openid"),
            self._nested_mapping_value(raw_message, "sender", "id"),
            self._nested_mapping_value(raw_message, "author", "id"),
        )
        nickname = self._first_non_empty_string(
            getattr(sender, "card", None),
            self._call_event_method(event, "get_sender_name"),
            getattr(sender, "nickname", None),
            getattr(sender, "username", None),
            getattr(sender, "display_name", None),
            self._nested_mapping_value(raw_message, "sender", "card"),
            self._nested_mapping_value(raw_message, "sender", "nickname"),
            self._nested_mapping_value(raw_message, "sender", "username"),
            self._nested_mapping_value(raw_message, "sender", "display_name"),
            self._nested_mapping_value(raw_message, "author", "username"),
            self._nested_mapping_value(raw_message, "author", "nickname"),
        )

        message_id = self._first_non_empty_string(
            self._event_message_id(event),
            self._mapping_value(raw_message, "event_id"),
        )
        message_type = self._extract_message_type(event, message_obj)
        if not group_id:
            group_id = self._fallback_conversation_id(
                event,
                message_type=message_type,
                user_id=user_id,
            )

        data: dict[str, Any] = {
            "platform": self._extract_platform(event, message_obj),
            "groupId": group_id,
            "groupName": self._extract_group_name(message_obj, raw_message),
            "userId": user_id,
            "nickname": nickname,
            "senderRole": self._extract_sender_role(event),
            "messageId": message_id,
            "messageType": message_type,
            "content": "".join(text_parts).strip(),
            "images": self._deduplicate(image_urls),
            "files": self._deduplicate(file_urls),
            "receiveTime": self._extract_receive_time(message_obj, raw_message),
        }
        if reply is not None:
            data["reply"] = reply

        return data

    @classmethod
    def _fallback_conversation_id(
        cls,
        event: AstrMessageEvent,
        *,
        message_type: str,
        user_id: str,
    ) -> str:
        """Provide a bounded conversation key for private official events.

        The backend historically requires ``GroupId`` even for direct chats.
        QQ official C2C events have no group id, so use the stable user/openid
        first and the AstrBot conversation origin as a final fallback.
        """
        prefix = "private" if message_type == "private_message" else "conversation"
        origin = cls._unified_msg_origin(event)
        value = (user_id if prefix == "private" else origin) or user_id or "unknown"
        # UploadMessageDto.GroupId has a 128 character limit.
        return f"{prefix}:{value}"[:128]

    @classmethod
    def _extract_reply_context(
        cls,
        components: Sequence[Any],
    ) -> dict[str, Any] | None:
        """Capture the first explicit Reply component and its quoted chain."""
        for component in components:
            if not isinstance(component, Reply):
                continue

            reply_chain = tuple(getattr(component, "chain", None) or ())
            text_parts, image_urls, file_urls = cls._extract_chain_content(
                reply_chain,
                include_replies=False,
            )
            content = cls._first_non_empty_string(
                getattr(component, "message_str", None),
                "".join(text_parts),
                getattr(component, "text", None),
            )
            reply_id = cls._normalize_identifier(
                getattr(component, "id", None)
            )

            reply = {
                "messageId": reply_id,
                "userId": cls._normalize_identifier(
                    getattr(component, "sender_id", None)
                ),
                "nickname": cls._first_non_empty_string(
                    getattr(component, "sender_nickname", None)
                ),
                "content": content,
                "images": cls._deduplicate(image_urls),
                "files": cls._deduplicate(file_urls),
                "receiveTime": cls._format_utc_time(
                    getattr(component, "time", None)
                ),
            }

            if any(
                (
                    reply["messageId"],
                    reply["content"],
                    reply["images"],
                    reply["files"],
                )
            ):
                return reply

        return None

    @classmethod
    def _extract_chain_content(
        cls,
        components: Sequence[Any],
        *,
        include_replies: bool,
    ) -> tuple[list[str], list[str], list[str]]:
        """Extract text, images, and files from one message component chain."""
        text_parts: list[str] = []
        image_urls: list[str] = []
        file_urls: list[str] = []

        for component in components:
            if isinstance(component, Plain):
                text = cls._to_string(getattr(component, "text", None))
                if text:
                    text_parts.append(text)
                continue

            if isinstance(component, Image):
                image_url = cls._extract_resource_url(component)
                if image_url:
                    image_urls.append(image_url)
                continue

            if isinstance(component, File):
                file_url = cls._extract_resource_url(component)
                if file_url:
                    file_urls.append(file_url)
                continue

            if include_replies and isinstance(component, Reply):
                reply_chain = tuple(getattr(component, "chain", None) or ())
                nested_text, nested_images, nested_files = (
                    cls._extract_chain_content(
                        reply_chain,
                        include_replies=False,
                    )
                )
                text_parts.extend(nested_text)
                image_urls.extend(nested_images)
                file_urls.extend(nested_files)

        return text_parts, image_urls, file_urls

    @classmethod
    def _extract_platform(
        cls,
        event: AstrMessageEvent,
        message_obj: Any,
    ) -> str:
        """Normalize adapter names without hiding QQ official vs NapCat."""
        raw_message = getattr(message_obj, "raw_message", None)
        raw_platform = cls._first_non_empty_string(
            cls._call_event_method(event, "get_platform_name"),
            getattr(event, "platform", None),
            getattr(message_obj, "platform", None),
            cls._mapping_value(raw_message, "platform"),
            cls._mapping_value(raw_message, "adapter"),
        ).lower().replace("-", "_").replace(" ", "_")

        if any(
            keyword in raw_platform
            for keyword in ("napcat", "aiocqhttp", "onebot", "llonebot")
        ):
            return "napcat"
        if any(
            keyword in raw_platform
            for keyword in ("qq_official", "qqofficial", "qqguild", "official")
        ):
            return "qq_official"
        if not raw_platform:
            return "qq"
        if raw_platform == "qq":
            return "qq"
        return raw_platform

    @classmethod
    def _is_self_message(cls, event: AstrMessageEvent) -> bool:
        """Return whether the event was sent by the active bot account."""
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        raw_message = getattr(message_obj, "raw_message", None)
        sender_id = cls._first_non_empty_string(
            cls._call_event_method(event, "get_sender_id"),
            getattr(sender, "user_id", None),
            getattr(sender, "user_openid", None),
            getattr(sender, "openid", None),
            getattr(sender, "id", None),
            cls._mapping_value(raw_message, "user_id"),
            cls._mapping_value(raw_message, "user_openid"),
            cls._mapping_value(raw_message, "openid"),
            cls._nested_mapping_value(raw_message, "sender", "user_id"),
            cls._nested_mapping_value(raw_message, "sender", "user_openid"),
            cls._nested_mapping_value(raw_message, "sender", "openid"),
            cls._nested_mapping_value(raw_message, "sender", "id"),
        )
        self_id = cls._first_non_empty_string(
            cls._call_event_method(event, "get_self_id"),
            getattr(message_obj, "self_id", None),
            cls._mapping_value(raw_message, "self_id"),
            cls._mapping_value(raw_message, "bot_id"),
            getattr(getattr(event, "bot", None), "self_id", None),
        )
        return bool(sender_id and self_id and sender_id == self_id)

    @classmethod
    def _extract_group_name(
        cls,
        message_obj: Any,
        raw_message: Any,
    ) -> str:
        return cls._first_non_empty_string(
            getattr(message_obj, "group_name", None),
            getattr(message_obj, "group_name", None),
            getattr(message_obj, "guild_name", None),
            getattr(message_obj, "channel_name", None),
            cls._mapping_value(raw_message, "group_name"),
            cls._mapping_value(raw_message, "guild_name"),
            cls._mapping_value(raw_message, "channel_name"),
            cls._nested_mapping_value(raw_message, "group", "group_name"),
            cls._nested_mapping_value(raw_message, "group", "name"),
            cls._nested_mapping_value(raw_message, "guild", "name"),
            cls._nested_mapping_value(raw_message, "channel", "name"),
        )

    @classmethod
    def _extract_message_type(
        cls,
        event: AstrMessageEvent,
        message_obj: Any,
    ) -> str:
        """Normalize OneBot, C2C, group-at, and official guild event names."""
        raw_message = getattr(message_obj, "raw_message", None)
        raw_type = cls._first_non_empty_string(
            cls._call_event_method(event, "get_message_type"),
            getattr(event, "message_type", None),
            getattr(message_obj, "message_type", None),
            getattr(message_obj, "type", None),
            cls._mapping_value(raw_message, "message_type"),
            cls._mapping_value(raw_message, "post_type"),
            cls._mapping_value(raw_message, "event_type"),
        ).lower().replace("-", "_").replace(" ", "_")
        if not raw_type:
            return "group_message"
        if any(
            token in raw_type
            for token in ("group", "guild", "channel", "group_at")
        ):
            return "group_message"
        if any(
            token in raw_type
            for token in ("friend", "private", "c2c", "direct")
        ):
            return "private_message"
        if raw_type in {"message", "message_event", "event"}:
            return "group_message"
        return raw_type

    @classmethod
    def _extract_sender_role(cls, event: AstrMessageEvent) -> str:
        role = cls._first_non_empty_string(getattr(event, "role", None))
        if role:
            return role.lower()
        return "admin" if bool(cls._call_event_method(event, "is_admin")) else "member"

    @classmethod
    def _extract_receive_time(
        cls,
        message_obj: Any,
        raw_message: Any,
    ) -> str:
        raw_time = cls._first_non_empty_value(
            getattr(message_obj, "timestamp", None),
            getattr(message_obj, "time", None),
            cls._mapping_value(raw_message, "timestamp"),
            cls._mapping_value(raw_message, "time"),
        )
        return cls._format_utc_time(raw_time) or datetime.now(
            timezone.utc
        ).isoformat()

    @classmethod
    def _format_utc_time(cls, raw_time: Any) -> str:
        """Normalize datetime, Unix timestamp, or ISO input to UTC ISO 8601."""
        if isinstance(raw_time, datetime):
            value = raw_time
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()

        if raw_time is not None:
            try:
                timestamp = float(raw_time)
                if timestamp <= 0:
                    return ""
                if timestamp > 10_000_000_000:
                    timestamp /= 1000
                return datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                raw_text = cls._to_string(raw_time)
                if raw_text:
                    try:
                        parsed = datetime.fromisoformat(
                            raw_text.replace("Z", "+00:00")
                        )
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        return parsed.astimezone(timezone.utc).isoformat()
                    except ValueError:
                        return ""

        return ""

    @classmethod
    def _extract_resource_url(cls, component: Any) -> str:
        """Read a usable URL or local file reference from a media component.

        QQ adapters may expose both a short-lived remote URL and a local file
        path. Prefer the local file when it exists so AstrBot's provider does
        not need to download an expiring or access-controlled QQ URL.
        """
        candidates: list[str] = []
        for attribute in ("file", "path", "url", "src", "download_url"):
            value = getattr(component, attribute, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None
            if isinstance(value, Mapping):
                value = cls._first_non_empty_string(
                    value.get("url"),
                    value.get("file"),
                    value.get("path"),
                )
            text = cls._to_string(value)
            if text and text not in candidates:
                candidates.append(text)

        for candidate in candidates:
            try:
                local_path = Path(candidate).expanduser()
                if local_path.is_file():
                    return str(local_path.resolve())
            except (OSError, ValueError):
                continue

        for candidate in candidates:
            if candidate.startswith(("http://", "https://", "data:")):
                return candidate

        return candidates[0] if candidates else ""

    @staticmethod
    def _call_event_method(
        event: AstrMessageEvent,
        method_name: str,
    ) -> Any:
        method = getattr(event, method_name, None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception:
            return None

    @staticmethod
    def _mapping_value(data: Any, key: str) -> Any:
        return data.get(key) if isinstance(data, Mapping) else None

    @classmethod
    def _nested_mapping_value(
        cls,
        data: Any,
        parent_key: str,
        child_key: str,
    ) -> Any:
        return cls._mapping_value(
            cls._mapping_value(data, parent_key),
            child_key,
        )

    @classmethod
    def _first_non_empty_string(cls, *values: Any) -> str:
        for value in values:
            text = cls._to_string(value)
            if text:
                return text
        return ""

    @staticmethod
    def _first_non_empty_value(*values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    @staticmethod
    def _to_string(value: Any) -> str:
        return "" if value is None else str(value).strip()

    @classmethod
    def _normalize_identifier(cls, value: Any) -> str:
        text = cls._to_string(value)
        return "" if text in {"", "0", "None"} else text

    @staticmethod
    def _deduplicate(values: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _reply_message_id(data: dict[str, Any]) -> str:
        reply = data.get("reply")
        return str(reply.get("messageId") or "") if isinstance(reply, dict) else ""

    @classmethod
    def _message_key(cls, data: dict[str, Any]) -> str:
        return "|".join(
            (
                str(data.get("platform") or "qq"),
                str(data.get("groupId") or ""),
                str(data.get("messageId") or ""),
            )
        )

    async def terminate(self) -> None:
        """Cancel pending raw uploads and release the HTTP client."""
        self._terminating = True
        pending_tasks = tuple(self._tasks)
        for task in pending_tasks:
            task.cancel()

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._tasks.clear()
        self._processing_tasks.clear()
        self._seen_message_keys.clear()
        self._media_event_context.clear()
        getattr(self, "_media_context_aliases", {}).clear()
        cleanup_handles = getattr(
            self,
            "_media_context_cleanup_handles",
            {},
        )
        for handle in cleanup_handles.values():
            handle.cancel()
        cleanup_handles.clear()

        try:
            await self.api_client.close()
        except Exception:
            logger.exception("LogisticsAI failed to close the API client.")

        logger.info("LogisticsAI plugin stopped.")
