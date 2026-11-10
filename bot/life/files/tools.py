"""Agent tools for the group file service (S6e).

Three thin handlers: write-and-send a document, send an existing file, download
a URL into the store. All of them re-check the Life scope so a gray release
never exposes files to a session that is not enabled.
"""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.common import event_session_enabled, unregister_tools

_TOOL_NAMES = ("send_document", "send_file", "download_file")


def register_file_tools(manager: Any, service: Any) -> None:
    """Register the file tools on the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
        service: Life service that owns the file service.
    """

    async def send_document(event: Any, name: str, content: str) -> str:
        """Write a text/markdown document and send it to this chat."""
        if service.files is None:
            return "error: file service is not enabled"
        if not event_session_enabled(service, event):
            return "error: file service is not enabled for this session"
        umo = getattr(event, "unified_msg_origin", "") or ""
        return await service.files.write_and_send(
            umo, str(name or ""), str(content or "")
        )

    async def send_file(event: Any, name: str) -> str:
        """Send one document that already exists in the file store."""
        if service.files is None:
            return "error: file service is not enabled"
        if not event_session_enabled(service, event):
            return "error: file service is not enabled for this session"
        umo = getattr(event, "unified_msg_origin", "") or ""
        return await service.files.send(umo, str(name or ""))

    async def download_file(event: Any, url: str, name: str = "") -> str:
        """Download an http(s) file into the store without sending it."""
        if service.files is None:
            return "error: file service is not enabled"
        if not event_session_enabled(service, event):
            return "error: file service is not enabled for this session"
        umo = getattr(event, "unified_msg_origin", "") or ""
        return await service.files.download(umo, str(url or ""), str(name or ""))

    definitions = (
        (
            "send_document",
            [
                {
                    "type": "string",
                    "name": "name",
                    "description": "File name with an allowed extension, e.g. notes.md.",
                },
                {
                    "type": "string",
                    "name": "content",
                    "description": "Document text written to the file.",
                },
            ],
            "Write a text or Markdown document and send it to the chat as a file. "
            "Use it when the answer is long enough to be a document instead of a message.",
            send_document,
        ),
        (
            "send_file",
            [
                {
                    "type": "string",
                    "name": "name",
                    "description": "Name of an existing file in the store.",
                },
            ],
            "Send a file that already exists in the store (for example after download_file).",
            send_file,
        ),
        (
            "download_file",
            [
                {
                    "type": "string",
                    "name": "url",
                    "description": "http(s) URL to fetch.",
                },
                {
                    "type": "string",
                    "name": "name",
                    "description": "Target name; inferred from the URL when empty.",
                },
            ],
            "Download an http(s) file into the store. It is not sent automatically.",
            download_file,
        ),
    )
    try:
        for name, args, desc, handler in definitions:
            manager.add_func(name=name, func_args=args, desc=desc, handler=handler)
    except Exception:
        logger.exception("[life] failed to register file tools")


def unregister_file_tools(manager: Any) -> None:
    """Remove the file tools from the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
    """
    unregister_tools(manager, _TOOL_NAMES, "file")
