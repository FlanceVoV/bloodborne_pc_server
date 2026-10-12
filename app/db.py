from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def now() -> int:
    return int(time.time())


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    port_family: Mapped[str] = mapped_column(String(80))
    port_version: Mapped[str] = mapped_column(String(40))
    game_version: Mapped[str] = mapped_column(String(40))
    protocol: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Server(Base):
    __tablename__ = "servers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"))
    mode: Mapped[str] = mapped_column(String(24), default="managed")
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=31313)
    webapi_url: Mapped[str] = mapped_column(String(500))
    community_url: Mapped[str] = mapped_column(String(500), default="https://thehuntersdream.com")
    max_players: Mapped[int] = mapped_column(Integer, default=16)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    last_heartbeat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now)
    profile: Mapped[Profile] = relationship()


class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    npid: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now)
    last_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    banned_until: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ban_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    client_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_ip: Mapped[str] = mapped_column(String(100))
    started_at: Mapped[int] = mapped_column(Integer, default=now)
    last_seen: Mapped[int] = mapped_column(Integer, default=now)
    ended_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disconnect_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="new")
    created_at: Mapped[int] = mapped_column(Integer, default=now)


class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    title: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[int] = mapped_column(Integer, default=now)
    expires_at: Mapped[int] = mapped_column(Integer)


class RoomMember(Base):
    __tablename__ = "room_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    joined_at: Mapped[int] = mapped_column(Integer, default=now)


class Mod(Base):
    __tablename__ = "mods"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(40))
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    downloads: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[int] = mapped_column(Integer, default=now)


class Token(Base):
    __tablename__ = "tokens"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[int] = mapped_column(Integer)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=now)


def database(config: dict, root: Path):
    url = config["database_url"]
    if url.startswith("sqlite:///./") or url.startswith("sqlite:///data/"):
        relative = url.removeprefix("sqlite:///").removeprefix("./")
        target = (root / relative).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{target.as_posix()}"
    engine = create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False} if url.startswith("sqlite:") else {})
    if url.startswith("sqlite:"):
        @event.listens_for(engine, "connect")
        def sqlite_settings(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
