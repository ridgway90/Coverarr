"""Long-lived clients for request-path work (thumbnails), so connections are reused."""

from __future__ import annotations

import asyncio

from coverarr.clients import MediaServerClient, client_for
from coverarr.models import Server


class ClientPool:
    def __init__(self) -> None:
        self._clients: dict[int, tuple[tuple, MediaServerClient]] = {}
        self._lock = asyncio.Lock()

    async def get(self, server: Server) -> MediaServerClient:
        signature = (server.kind, server.url, server.token, server.verify_ssl)
        async with self._lock:
            cached = self._clients.get(server.id)
            if cached and cached[0] == signature:
                return cached[1]
            if cached:
                await cached[1].aclose()
            client = client_for(server, concurrency=8)
            self._clients[server.id] = (signature, client)
            return client

    async def drop(self, server_id: int) -> None:
        async with self._lock:
            cached = self._clients.pop(server_id, None)
        if cached:
            await cached[1].aclose()

    async def close(self) -> None:
        async with self._lock:
            clients = [c for _, c in self._clients.values()]
            self._clients.clear()
        for client in clients:
            await client.aclose()
