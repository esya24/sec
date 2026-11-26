import asyncio
import logging
import os
import re
import socket
from contextlib import nullcontext
from secrets import compare_digest
from urllib.parse import urlsplit

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.responses import Response

LOGGER = logging.getLogger(__name__)


class MCPHTTPServer(uvicorn.Server):
    def capture_signals(self):
        # python-telegram-bot owns the process signal handlers.
        return nullcontext()


class SecretaryMCP:
    def __init__(self, secretary):
        self.secretary = secretary
        self.server = None
        self.task = None

    async def start(self):
        token = os.environ.get("SECRETARY_MCP_TOKEN", "").strip()
        if not token:
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
            raise ValueError("SECRETARY_MCP_TOKEN должен быть случайным URL-safe токеном от 32 символов.")

        public_url = os.environ.get("MCP_PUBLIC_URL", "").strip()
        public = urlsplit(public_url)
        if public_url and (
            public.scheme != "https" or not public.hostname or public.username is not None
            or public.path not in {"", "/"} or public.query or public.fragment
        ):
            raise ValueError("В MCP_PUBLIC_URL укажите HTTPS-адрес сайта без пути.")
        host = "0.0.0.0" if public_url else "127.0.0.1"
        port = int(os.environ.get("PORT", "8765"))
        allowed_hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
        if public_url:
            allowed_hosts.append(public.netloc.lower())
        mcp = FastMCP(
            "Telegram Secretary", stateless_http=True, json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
            ),
        )

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        async def secretary_status() -> str:
            """Показать состояние модели, автоответов и число ручных пауз."""
            return self.secretary.status_text()

        @mcp.tool()
        async def secretary_resume_all() -> int:
            """Снять все текущие ручные паузы секретаря. Вернуть число возобновлённых чатов."""
            return await self.secretary.manual_chats.resume_async()

        app = mcp.streamable_http_app()
        expected = f"Bearer {token}".encode("ascii")

        async def authenticated_app(scope, receive, send):
            if scope["type"] == "http":
                authorization = Headers(scope=scope).get("authorization", "").encode("latin-1")
                if not compare_digest(authorization, expected):
                    response = Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
                    await response(scope, receive, send)
                    return
            await app(scope, receive, send)

        self.server = MCPHTTPServer(uvicorn.Config(
            authenticated_app, host=host, port=port, access_log=False, lifespan="on",
            ws="none", timeout_graceful_shutdown=5,
        ))
        listener = socket.create_server((host, port))

        async def serve():
            with listener:
                await self.server.serve(sockets=[listener])

        self.task = asyncio.create_task(serve())
        while not self.server.started and not self.task.done():
            await asyncio.sleep(0.01)
        if not self.server.started:
            await self.task
            raise RuntimeError("HTTP-сервер MCP не запустился.")
        LOGGER.info("MCP секретаря запущен на порту %d", listener.getsockname()[1])

    async def stop(self):
        if self.task is not None:
            self.server.should_exit = True
            await self.task

