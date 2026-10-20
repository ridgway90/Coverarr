from coverarr.clients.base import (
    MatchTarget,
    MediaServerClient,
    MediaServerError,
    RemoteCollection,
    RemoteItem,
    RemoteLibrary,
    ServerInfo,
)
from coverarr.clients.jellyfin import EmbyClient, JellyfinClient
from coverarr.clients.plex import PlexClient

CLIENTS: dict[str, type[MediaServerClient]] = {
    "plex": PlexClient,
    "jellyfin": JellyfinClient,
    "emby": EmbyClient,
}


def make_client(
    kind: str, url: str, token: str, *, verify_ssl: bool = True, **kwargs
) -> MediaServerClient:
    try:
        cls = CLIENTS[kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported server kind {kind!r}") from exc
    return cls(url, token, verify_ssl=verify_ssl, **kwargs)


def client_for(server, **kwargs) -> MediaServerClient:
    """Build a client for a ``Server`` row."""
    return make_client(
        server.kind, server.url, server.token, verify_ssl=server.verify_ssl, **kwargs
    )


__all__ = [
    "CLIENTS",
    "EmbyClient",
    "JellyfinClient",
    "MatchTarget",
    "MediaServerClient",
    "MediaServerError",
    "PlexClient",
    "RemoteCollection",
    "RemoteItem",
    "RemoteLibrary",
    "ServerInfo",
    "client_for",
    "make_client",
]
