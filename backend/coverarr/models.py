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
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, list[str]: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    libraries: Mapped[list[ServerLibrary]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )


class Library(Base):
    """A logical library, e.g. "Films", grouping the matching library on each server."""

    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # movie | show
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

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
    # Perceptual hash of the poster and the poster_ref it was computed for.
    poster_hash: Mapped[str | None] = mapped_column(String(32))
    poster_hash_ref: Mapped[str | None] = mapped_column(String(500))
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL")
    )
    seen_run: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    media_item: Mapped[MediaItem | None] = relationship(back_populates="server_items")

    @property
    def has_kometa_overlay(self) -> bool:
        return any(label.lower() == "overlay" for label in self.labels or [])


class Collection(Base):
    """A logical collection matched by name across servers."""

    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("library_id", "normalized_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int | None] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500))
    sort_title: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    poster_source: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

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
    member_remote_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("collections.id", ondelete="SET NULL")
    )
    seen_run: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
