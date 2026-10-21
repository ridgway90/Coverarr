"""Database models.

Two layers of records:

* ``Server*`` rows mirror what each media server reports (one row per remote object).
* ``MediaItem`` / ``Collection`` / ``Library`` are Coverarr's own logical records. Server rows
  link to them after matching, so one logical movie maps to its copy on Plex, Jellyfin and Emby.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes.

    SQLite stores datetimes without an offset and hands them back naive, which made the API
    send times the browser read as local time. Store UTC and always return it as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, list[str]: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # plex | jellyfin | emby
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    token: Mapped[str] = mapped_column(String(500))
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remote_id: Mapped[str | None] = mapped_column(String(200))  # Plex machineIdentifier etc.
    version: Mapped[str | None] = mapped_column(String(50))
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    libraries: Mapped[list[ServerLibrary]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )


class Library(Base):
    """A logical library, e.g. "Films", grouping the matching library on each server."""

    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # movie | show
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    server_libraries: Mapped[list[ServerLibrary]] = relationship(back_populates="library")


class ServerLibrary(Base):
    __tablename__ = "server_libraries"
    __table_args__ = (UniqueConstraint("server_id", "remote_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    remote_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # movie | show
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    server: Mapped[Server] = relationship(back_populates="libraries")
    library: Mapped[Library | None] = relationship(back_populates="server_libraries")


class MediaItem(Base):
    """A logical movie, show, season or episode."""

    __tablename__ = "media_items"
    __table_args__ = (
        Index("ix_media_items_lib_kind", "library_id", "kind"),
        Index("ix_media_items_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # movie | show | season | episode
    title: Mapped[str] = mapped_column(String(500))
    sort_title: Mapped[str | None] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    tmdb_id: Mapped[str | None] = mapped_column(String(50), index=True)
    tvdb_id: Mapped[str | None] = mapped_column(String(50), index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(50), index=True)
    # Season -> show, episode -> season.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"))
    season_number: Mapped[int | None] = mapped_column(Integer)
    episode_number: Mapped[int | None] = mapped_column(Integer)
    # Where the clean base poster comes from: "server:<id>", later also tmdb/upload/kometa.
    poster_source: Mapped[str | None] = mapped_column(String(100))
    # Resolution / HDR / DV / edition / network merged from every server (services/mediainfo).
    facts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    server_items: Mapped[list[ServerItem]] = relationship(back_populates="media_item")


class ServerItem(Base):
    __tablename__ = "server_items"
    __table_args__ = (
        UniqueConstraint("server_id", "remote_id"),
        Index("ix_server_items_media", "media_item_id"),
        Index("ix_server_items_lib_kind", "server_library_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    server_library_id: Mapped[int] = mapped_column(
        ForeignKey("server_libraries.id", ondelete="CASCADE")
    )
    remote_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(500))
    sort_title: Mapped[str | None] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    tmdb_id: Mapped[str | None] = mapped_column(String(50))
    tvdb_id: Mapped[str | None] = mapped_column(String(50))
    imdb_id: Mapped[str | None] = mapped_column(String(50))
    # Remote id of the show (for seasons/episodes) and season (for episodes).
    show_remote_id: Mapped[str | None] = mapped_column(String(200))
    season_remote_id: Mapped[str | None] = mapped_column(String(200))
    season_number: Mapped[int | None] = mapped_column(Integer)
    episode_number: Mapped[int | None] = mapped_column(Integer)
    # Opaque per-server poster reference: Plex thumb path, Jellyfin/Emby image tag.
    # It changes whenever the poster changes, which is how we avoid re-downloading.
    poster_ref: Mapped[str | None] = mapped_column(String(500))
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Media file (movie) or folder (show) as the server sees it, and the mount-independent
    # key used to spot the same file matched to different titles on different servers.
    path: Mapped[str | None] = mapped_column(String(1000))
    file_key: Mapped[str | None] = mapped_column(String(500), index=True)
    # Perceptual hash of the poster and the poster_ref it was computed for.
    poster_hash: Mapped[str | None] = mapped_column(String(32))
    poster_hash_ref: Mapped[str | None] = mapped_column(String(500))
    # The poster_ref right after Coverarr last uploaded a poster here, and what it uploaded
    # ("base:<source>" or the render key of an overlaid poster).
    pushed_ref: Mapped[str | None] = mapped_column(String(500))
    pushed_key: Mapped[str | None] = mapped_column(String(100))
    # 16x24 greyscale thumbnail (hex) for telling overlays apart (services/posters.py).
    poster_edges: Mapped[str | None] = mapped_column(String(800))
    # Resolution / HDR / DV / edition / network as this server reports them (for overlays).
    media: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL")
    )
    seen_run: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    media_item: Mapped[MediaItem | None] = relationship(back_populates="server_items")

    @property
    def has_kometa_overlay(self) -> bool:
        return any(label.lower() == "overlay" for label in self.labels or [])


class Collection(Base):
    """A logical collection matched by name across servers."""

    __tablename__ = "collections"
    # Not unique: one server can have two collections whose names normalise the same.
    __table_args__ = (Index("ix_collections_library_norm", "library_id", "normalized_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500))
    sort_title: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    poster_source: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    server_collections: Mapped[list[ServerCollection]] = relationship(back_populates="collection")


class ServerCollection(Base):
    __tablename__ = "server_collections"
    __table_args__ = (UniqueConstraint("server_id", "remote_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    # Plex collections belong to a section; Jellyfin/Emby box sets don't, so this is
    # inferred from the members' library for those.
    server_library_id: Mapped[int | None] = mapped_column(
        ForeignKey("server_libraries.id", ondelete="SET NULL")
    )
    remote_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(500))
    sort_title: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    smart: Mapped[bool] = mapped_column(Boolean, default=False)
    poster_ref: Mapped[str | None] = mapped_column(String(500))
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    poster_hash: Mapped[str | None] = mapped_column(String(32))
    poster_hash_ref: Mapped[str | None] = mapped_column(String(500))
    pushed_ref: Mapped[str | None] = mapped_column(String(500))
    pushed_key: Mapped[str | None] = mapped_column(String(100))
    poster_edges: Mapped[str | None] = mapped_column(String(800))
    member_remote_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("collections.id", ondelete="SET NULL")
    )
    seen_run: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    collection: Mapped[Collection | None] = relationship(back_populates="server_collections")


class Issue(Base):
    """A discrepancy found by the review pass. Recomputed after every import.

    ``key`` is stable across imports so an ignored issue stays ignored.
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(300), unique=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE")
    )
    collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(String(500))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    log: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Artwork(Base):
    """A clean base image Coverarr keeps itself (a Kometa original, later uploads/TMDb).

    Files live under ``<config>/assets``; ``file_path`` is relative to the config dir.
    """

    __tablename__ = "artwork"
    __table_args__ = (
        Index("ix_artwork_media", "media_item_id"),
        Index("ix_artwork_collection", "collection_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE")
    )
    collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String(20))  # kometa | upload | tmdb
    file_path: Mapped[str] = mapped_column(String(1000))
    original_name: Mapped[str | None] = mapped_column(String(500))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    poster_hash: Mapped[str | None] = mapped_column(String(32))
    poster_edges: Mapped[str | None] = mapped_column(String(800))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CollectionDefinition(Base):
    """A collection defined by rules (e.g. imported from a Kometa YAML file)."""

    __tablename__ = "collection_definitions"
    __table_args__ = (UniqueConstraint("library_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(20), default="kometa")
    source_file: Mapped[str | None] = mapped_column(String(300))
    # [{"type": "tmdb_list", "value": "8694016"}, {"type": "trakt_list", "value": "https://..."}]
    builders: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # sync_mode, collection_order, sort_title, label, summary, ...
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Kometa keys Coverarr doesn't support (yet), kept so nothing is silently dropped.
    unsupported: Mapped[list[str]] = mapped_column(JSON, default=list)
    check_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Kept in sync on every server (on its own schedule and in the weekly scan).
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
