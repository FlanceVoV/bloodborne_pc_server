from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import threading
import time
import urllib.request
import zipfile
import configparser
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import ROOT, load_config
from .db import Audit, Connection, Feedback, Mod, Player, Profile, Room, RoomMember, Server, Token, database, now
from .security import new_token, password_hash, token_hash, verify_password
from .shadnet_protocol import ShadNetError, register_account

PROFILE_ID = "supermedo-windows-v1.6"
NPID = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
MAX_MOD_BYTES = 100 * 1024 * 1024


class Credentials(BaseModel):
    username: str
    password: str


class PlayerCredentials(BaseModel):
    npid: str
    password: str


class ShadNetRegistration(BaseModel):
    npid: str
    password: str
    email: str


class ProfileInput(BaseModel):
    id: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=160)
    port_family: str = Field(min_length=1, max_length=80)
    port_version: str = Field(min_length=1, max_length=40)
    game_version: str = Field(min_length=1, max_length=40)
    protocol: str = Field(min_length=1, max_length=80)
    enabled: bool = True


class ServerInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    profile_id: str
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=31313, ge=1, le=65535)
    webapi_url: str = Field(min_length=1, max_length=500)
    community_url: str = Field(default="https://thehuntersdream.com", max_length=500)
    max_players: int = Field(default=16, ge=1, le=1000)
    enabled: bool = True


class ConnectionStart(BaseModel):
    npid: str
    server_id: int


class FeedbackInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=2, max_length=4000)


class BanInput(BaseModel):
    until: int | None = None
    reason: str = Field(default="", max_length=500)


class RoomInput(BaseModel):
    server_id: int
    title: str = Field(min_length=1, max_length=160)
    capacity: int = Field(default=3, ge=2, le=16)
    password: str = Field(default="", max_length=100)


class RoomJoin(BaseModel):
    password: str = ""


def server_dict(server: Server, db: Session) -> dict:
    active_since = now() - 120
    online = db.scalar(select(func.count(Connection.id)).where(
        Connection.server_id == server.id, Connection.ended_at.is_(None), Connection.last_seen >= active_since)) or 0
    return {
        "id": server.id, "name": server.name, "profile_id": server.profile_id,
        "mode": server.mode, "host": server.host, "port": server.port,
        "webapi_url": server.webapi_url, "community_url": server.community_url,
        "max_players": server.max_players, "online_players": online,
        "enabled": server.enabled, "last_heartbeat": server.last_heartbeat,
    }


def profile_dict(profile: Profile) -> dict:
    return {"id": profile.id, "name": profile.name, "port_family": profile.port_family,
            "port_version": profile.port_version, "game_version": profile.game_version,
            "protocol": profile.protocol, "enabled": profile.enabled}


def mod_dict(mod: Mod) -> dict:
    return {"id": mod.id, "slug": mod.slug, "name": mod.name, "version": mod.version,
            "profile_id": mod.profile_id, "sha256": mod.sha256, "size": mod.size,
            "downloads": mod.downloads, "enabled": mod.enabled,
            "download_url": f"/api/v1/mods/{mod.id}/download"}


def player_by_npid(db: Session, npid: str) -> Player | None:
    return db.scalar(select(Player).where(func.lower(Player.npid) == npid.lower()))


def create_app(config_path: Path | None = None) -> FastAPI:
    config = load_config(config_path)
    root = Path(config_path).resolve().parent if config_path else ROOT
    engine, session_factory = database(config, root)
    app = FastAPI(title="Bloodborne PC Server", version="1.0.0", docs_url=None, redoc_url=None)
    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")
    app.state.config = config
    app.state.root = root
    app.state.engine = engine
    attempts: dict[tuple[str, str], list[int]] = {}
    attempts_lock = threading.Lock()

    @app.middleware("http")
    async def throttle(request: Request, call_next):
        limits = {"/api/v1/admin/login": 10, "/api/v1/players/register": 5,
                  "/api/v1/players/login": 15, "/api/v1/feedback": 10,
                  "/api/v1/connections/start": 30}
        limit = limits.get(request.url.path)
        if limit and request.method == "POST":
            key = (request.client.host if request.client else "unknown", request.url.path)
            stamp = now()
            with attempts_lock:
                recent = [x for x in attempts.get(key, []) if x > stamp - 60]
                if len(recent) >= limit:
                    return Response(content='{"detail":"Too many requests"}', status_code=429,
                                    media_type="application/json")
                recent.append(stamp)
                attempts[key] = recent
        return await call_next(request)

    with session_factory() as db:
        if not db.get(Profile, PROFILE_ID):
            db.add(Profile(id=PROFILE_ID, name="Supermedo Windows 1.6 / Bloodborne 1.09",
                           port_family="supermedo", port_version="1.6", game_version="01.09",
                           protocol="shadnet-p2p-v1", enabled=True))
            db.commit()

    def get_db():
        with session_factory() as db:
            yield db

    def audit(db: Session, actor: str, action: str, detail: str = ""):
        db.add(Audit(actor=actor, action=action, detail=detail[:2000]))

    def session_token(request: Request, db: Session, admin: bool) -> Token:
        if admin:
            raw = request.cookies.get("bb_admin", "")
        else:
            auth = request.headers.get("authorization", "")
            raw = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = db.get(Token, token_hash(raw)) if raw else None
        if not token or token.expires_at < now() or token.is_admin != admin:
            raise HTTPException(401, "Authentication required")
        return token

    def admin_read(request: Request, db: Session = Depends(get_db)) -> Token:
        return session_token(request, db, True)

    def admin_write(request: Request, db: Session = Depends(get_db)) -> Token:
        token = session_token(request, db, True)
        origin = request.headers.get("origin")
        allowed_origins = {config["public_base_url"].rstrip("/"), str(request.base_url).rstrip("/")}
        if origin and origin.rstrip("/") not in allowed_origins:
            raise HTTPException(403, "Invalid origin")
        raw = request.cookies.get("bb_admin", "")
        expected = hmac.new(config["session_secret"].encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-bb-csrf", ""), expected):
            raise HTTPException(403, "Invalid CSRF token")
        return token

    def player_auth(request: Request, db: Session = Depends(get_db)) -> Player:
        token = session_token(request, db, False)
        player = db.get(Player, token.player_id)
        if not player or (player.banned_until and player.banned_until > now()):
            raise HTTPException(403, "Player is banned")
        return player

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    @app.get("/host", response_class=HTMLResponse)
    def host_page():
        return (ROOT / "web" / "host.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health():
        return {"ok": True, "service": "bloodborne-pc-server", "version": "1.0.0"}

    @app.get("/api/v1/profiles")
    def profiles(db: Session = Depends(get_db)):
        return [profile_dict(p) for p in db.scalars(select(Profile).where(Profile.enabled.is_(True))).all()]

    @app.get("/api/v1/servers")
    def servers(profile_id: str | None = None, db: Session = Depends(get_db)):
        query = select(Server).where(Server.enabled.is_(True))
        if profile_id:
            query = query.where(Server.profile_id == profile_id)
        result = []
        for server in db.scalars(query).all():
            if server.mode == "player_hosted" and (server.last_heartbeat or 0) < now() - 180:
                continue
            result.append(server_dict(server, db))
        return result

    @app.get("/api/v1/public/stats")
    def public_stats(db: Session = Depends(get_db)):
        stamp = now()
        visible = db.scalars(select(Server).where(Server.enabled.is_(True))).all()
        active_servers = sum(1 for server in visible if server.mode != "player_hosted" or
                             (server.last_heartbeat or 0) >= stamp - 180)
        return {
            "online_hunters": db.scalar(select(func.count(Connection.id)).where(
                Connection.ended_at.is_(None), Connection.last_seen >= stamp - 120)) or 0,
            "registered_hunters": db.scalar(select(func.count(Player.id)).where(
                Player.password_hash.is_not(None))) or 0,
            "active_servers": active_servers,
            "open_rooms": db.scalar(select(func.count(Room.id)).where(Room.expires_at > stamp)) or 0,
            "total_joins": db.scalar(select(func.count(Connection.id))) or 0,
            "available_mods": db.scalar(select(func.count(Mod.id)).where(Mod.enabled.is_(True))) or 0,
            "source": "lobby",
            "updated_at": stamp,
        }

    @app.get("/api/v1/mods")
    def mods(profile_id: str | None = None, db: Session = Depends(get_db)):
        query = select(Mod).where(Mod.enabled.is_(True))
        if profile_id:
            query = query.where(Mod.profile_id == profile_id)
        return [mod_dict(m) for m in db.scalars(query).all()]

    @app.get("/api/v1/mods/{mod_id}/download")
    def mod_download(mod_id: int, db: Session = Depends(get_db)):
        mod = db.get(Mod, mod_id)
        if not mod or not mod.enabled:
            raise HTTPException(404, "Mod not found")
        path = root / "data" / "mods" / mod.filename
        if not path.is_file():
            raise HTTPException(404, "Mod archive missing")
        mod.downloads += 1
        db.commit()
        return FileResponse(path, media_type="application/zip", filename=f"{mod.slug}-{mod.version}.zip")

    @app.post("/api/v1/feedback")
    def submit_feedback(payload: FeedbackInput, db: Session = Depends(get_db)):
        row = Feedback(name=payload.name.strip(), message=payload.message.strip())
        db.add(row)
        db.commit()
        return {"id": row.id, "status": row.status}

    @app.post("/api/v1/connections/start")
    def connection_start(payload: ConnectionStart, request: Request, db: Session = Depends(get_db)):
        npid = payload.npid.strip()
        if not NPID.fullmatch(npid):
            raise HTTPException(422, "Invalid NPID")
        server = db.get(Server, payload.server_id)
        if not server or not server.enabled:
            raise HTTPException(404, "Server unavailable")
        player = player_by_npid(db, npid)
        if not player:
            player = Player(npid=npid)
            db.add(player)
            db.flush()
        if player.banned_until and player.banned_until > now():
            raise HTTPException(403, "Player is banned")
        active = db.scalar(select(func.count(Connection.id)).where(
            Connection.server_id == server.id, Connection.ended_at.is_(None), Connection.last_seen >= now() - 120)) or 0
        if active >= server.max_players:
            raise HTTPException(409, "Server is full")
        raw = new_token()
        row = Connection(player_id=player.id, server_id=server.id, client_token_hash=token_hash(raw),
                         client_ip=request.client.host if request.client else "unknown")
        player.last_seen = now()
        db.add(row)
        db.flush()
        audit(db, npid, "connection.start", f"server={server.id}")
        db.commit()
        return {"connection_id": row.id, "token": raw, "heartbeat_seconds": 45}

    def connection_access(conn_id: int, authorization: str, db: Session) -> Connection:
        raw = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        row = db.get(Connection, conn_id)
        if not raw or not row or not hmac.compare_digest(row.client_token_hash, token_hash(raw)):
            raise HTTPException(401, "Invalid connection token")
        return row

    @app.post("/api/v1/connections/{conn_id}/heartbeat")
    def connection_heartbeat(conn_id: int, authorization: str = Header(default=""), db: Session = Depends(get_db)):
        row = connection_access(conn_id, authorization, db)
        if row.ended_at is not None:
            raise HTTPException(409, "Connection ended")
        row.last_seen = now()
        db.get(Player, row.player_id).last_seen = now()
        db.commit()
        return {"ok": True}

    @app.post("/api/v1/connections/{conn_id}/end")
    def connection_end(conn_id: int, authorization: str = Header(default=""), db: Session = Depends(get_db)):
        row = connection_access(conn_id, authorization, db)
        if row.ended_at is None:
            row.ended_at = now()
            row.disconnect_reason = "client_exit"
            db.commit()
        return {"ok": True}

    @app.post("/api/v1/players/register")
    def player_register(payload: PlayerCredentials, db: Session = Depends(get_db)):
        if not config.get("allow_player_registration", False):
            raise HTTPException(403, "Registration disabled")
        if not NPID.fullmatch(payload.npid) or len(payload.password) < 10:
            raise HTTPException(422, "NPID must be 3-32 simple characters; password at least 10")
        player = player_by_npid(db, payload.npid)
        if player and player.password_hash:
            raise HTTPException(409, "Player already registered")
        if not player:
            player = Player(npid=payload.npid)
            db.add(player)
        player.password_hash = password_hash(payload.password)
        db.commit()
        return {"id": player.id, "npid": player.npid}

    @app.post("/api/v1/players/login")
    def player_login(payload: PlayerCredentials, db: Session = Depends(get_db)):
        player = player_by_npid(db, payload.npid)
        if not player or not verify_password(payload.password, player.password_hash):
            raise HTTPException(401, "Invalid credentials")
        if player.banned_until and player.banned_until > now():
            raise HTTPException(403, "Player is banned")
        raw = new_token()
        db.add(Token(hash=token_hash(raw), player_id=player.id, is_admin=False, expires_at=now() + 86400))
        db.commit()
        return {"token": raw, "expires_in": 86400, "npid": player.npid}

    @app.get("/api/v1/rooms")
    def rooms(profile_id: str | None = None, db: Session = Depends(get_db)):
        query = select(Room).where(Room.expires_at > now())
        result = []
        for room in db.scalars(query).all():
            server = db.get(Server, room.server_id)
            if not server or not server.enabled or (profile_id and server.profile_id != profile_id):
                continue
            members = db.scalar(select(func.count(RoomMember.id)).where(RoomMember.room_id == room.id)) or 0
            result.append({"id": room.id, "title": room.title, "server_id": room.server_id,
                           "owner_npid": db.get(Player, room.owner_id).npid, "members": members,
                           "capacity": room.capacity, "locked": bool(room.password_hash),
                           "expires_at": room.expires_at})
        return result

    @app.post("/api/v1/rooms")
    def room_create(payload: RoomInput, player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        server = db.get(Server, payload.server_id)
        if not server or not server.enabled:
            raise HTTPException(404, "Server unavailable")
        row = Room(owner_id=player.id, server_id=server.id, title=payload.title.strip(),
                   capacity=payload.capacity, password_hash=password_hash(payload.password) if payload.password else None,
                   expires_at=now() + 4 * 3600)
        db.add(row)
        db.flush()
        db.add(RoomMember(room_id=row.id, player_id=player.id))
        db.commit()
        return {"id": row.id, "expires_at": row.expires_at}

    @app.post("/api/v1/rooms/{room_id}/join")
    def room_join(room_id: int, payload: RoomJoin, player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        room = db.get(Room, room_id)
        if not room or room.expires_at <= now():
            raise HTTPException(404, "Room unavailable")
        if room.password_hash and not verify_password(payload.password, room.password_hash):
            raise HTTPException(403, "Wrong room password")
        existing = db.scalar(select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.player_id == player.id))
        if not existing:
            count = db.scalar(select(func.count(RoomMember.id)).where(RoomMember.room_id == room_id)) or 0
            if count >= room.capacity:
                raise HTTPException(409, "Room is full")
            db.add(RoomMember(room_id=room_id, player_id=player.id))
            db.commit()
        return {"server": server_dict(db.get(Server, room.server_id), db)}

    @app.post("/api/v1/rooms/{room_id}/heartbeat")
    def room_heartbeat(room_id: int, player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        room = db.get(Room, room_id)
        if not room or room.owner_id != player.id:
            raise HTTPException(403, "Room owner required")
        room.expires_at = now() + 4 * 3600
        db.commit()
        return {"expires_at": room.expires_at}

    @app.delete("/api/v1/rooms/{room_id}")
    def room_delete(room_id: int, player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        room = db.get(Room, room_id)
        if not room or room.owner_id != player.id:
            raise HTTPException(403, "Room owner required")
        room.expires_at = now()
        db.commit()
        return {"ok": True}

    @app.post("/api/v1/admin/login")
    def admin_login(payload: Credentials, response: Response, db: Session = Depends(get_db)):
        admin = config["admin"]
        valid_name = hmac.compare_digest(payload.username, admin["username"])
        valid_pass = hmac.compare_digest(payload.password, admin["password"])
        if not (valid_name and valid_pass):
            raise HTTPException(401, "Invalid credentials")
        raw = new_token()
        csrf = hmac.new(config["session_secret"].encode(), raw.encode(), hashlib.sha256).hexdigest()
        db.add(Token(hash=token_hash(raw), is_admin=True, expires_at=now() + 8 * 3600))
        audit(db, admin["username"], "admin.login")
        db.commit()
        response.set_cookie("bb_admin", raw, max_age=8 * 3600, httponly=True, samesite="strict",
                            secure=config["public_base_url"].startswith("https://"))
        return {"ok": True, "csrf": csrf}

    @app.get("/api/v1/admin/me")
    def admin_me(request: Request, _token: Token = Depends(admin_read)):
        raw = request.cookies["bb_admin"]
        csrf = hmac.new(config["session_secret"].encode(), raw.encode(), hashlib.sha256).hexdigest()
        return {"username": config["admin"]["username"], "csrf": csrf}

    @app.post("/api/v1/admin/logout")
    def admin_logout(request: Request, response: Response, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        db.delete(_token)
        db.commit()
        response.delete_cookie("bb_admin")
        return {"ok": True}

    @app.get("/api/v1/admin/stats")
    def admin_stats(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        return {
            "online_players": db.scalar(select(func.count(Connection.id)).where(
                Connection.ended_at.is_(None), Connection.last_seen >= now() - 120)) or 0,
            "players": db.scalar(select(func.count(Player.id))) or 0,
            "servers": db.scalar(select(func.count(Server.id))) or 0,
            "rooms": db.scalar(select(func.count(Room.id)).where(Room.expires_at > now())) or 0,
            "mods": db.scalar(select(func.count(Mod.id))) or 0,
            "feedback_new": db.scalar(select(func.count(Feedback.id)).where(Feedback.status == "new")) or 0,
            "backend_configured": bool(config.get("shadnet", {}).get("enabled")),
        }

    @app.get("/api/v1/admin/analytics")
    def admin_analytics(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        stamp = now()
        day = 86400
        series = []
        for offset in range(6, -1, -1):
            start = (stamp // day - offset) * day
            series.append({"day_start": start, "joins": db.scalar(select(func.count(Connection.id)).where(
                Connection.started_at >= start, Connection.started_at < start + day)) or 0})
        server_rows = db.scalars(select(Server).order_by(Server.id)).all()
        server_activity = [{"id": server.id, "name": server.name, "mode": server.mode,
                            "enabled": server.enabled,
                            "online": db.scalar(select(func.count(Connection.id)).where(
                                Connection.server_id == server.id, Connection.ended_at.is_(None),
                                Connection.last_seen >= stamp - 120)) or 0,
                            "joins": db.scalar(select(func.count(Connection.id)).where(
                                Connection.server_id == server.id)) or 0}
                           for server in server_rows]
        recent = db.execute(select(Connection, Player.npid, Server.name).join(
            Player, Connection.player_id == Player.id).join(
            Server, Connection.server_id == Server.id).order_by(Connection.id.desc()).limit(8)).all()
        return {
            "online_hunters": db.scalar(select(func.count(Connection.id)).where(
                Connection.ended_at.is_(None), Connection.last_seen >= stamp - 120)) or 0,
            "registered_hunters": db.scalar(select(func.count(Player.id)).where(
                Player.password_hash.is_not(None))) or 0,
            "total_joins": db.scalar(select(func.count(Connection.id))) or 0,
            "joins_24h": db.scalar(select(func.count(Connection.id)).where(
                Connection.started_at >= stamp - day)) or 0,
            "unique_hunters_24h": db.scalar(select(func.count(func.distinct(Connection.player_id))).where(
                Connection.started_at >= stamp - day)) or 0,
            "active_rooms": db.scalar(select(func.count(Room.id)).where(Room.expires_at > stamp)) or 0,
            "player_hosted_servers": db.scalar(select(func.count(Server.id)).where(
                Server.mode == "player_hosted", Server.enabled.is_(True),
                Server.last_heartbeat >= stamp - 180)) or 0,
            "mod_downloads": db.scalar(select(func.coalesce(func.sum(Mod.downloads), 0))) or 0,
            "feedback_total": db.scalar(select(func.count(Feedback.id))) or 0,
            "feedback_new": db.scalar(select(func.count(Feedback.id)).where(
                Feedback.status == "new")) or 0,
            "banned_hunters": db.scalar(select(func.count(Player.id)).where(
                Player.banned_until > stamp)) or 0,
            "daily_joins": series,
            "server_activity": server_activity,
            "recent_joins": [{"npid": npid, "server": server_name,
                              "started_at": connection.started_at,
                              "ended_at": connection.ended_at} for connection, npid, server_name in recent],
            "gameplay_telemetry": {"available": False,
                                   "reason": "游戏内死亡、击杀、灯笼、圣杯与建言由社区服务处理；本服尚无这些事件的采集接口。"},
            "updated_at": stamp,
        }

    @app.get("/api/v1/admin/backend")
    def admin_backend(_token: Token = Depends(admin_read)):
        backend = config.get("shadnet", {})
        if not backend.get("enabled"):
            return {"configured": False, "reachable": False}
        try:
            with urllib.request.urlopen(backend.get("status_url", ""), timeout=2) as reply:
                status = json.load(reply)
            return {"configured": True, "reachable": bool(status.get("ok")), "status": status}
        except (OSError, ValueError) as error:
            return {"configured": True, "reachable": False, "error": str(error)}

    @app.post("/api/v1/admin/shadnet/register")
    def admin_shadnet_register(payload: ShadNetRegistration, _token: Token = Depends(admin_write),
                               db: Session = Depends(get_db)):
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,16}", payload.npid):
            raise HTTPException(422, "shadNet NPID must be 3-16 letters, digits, _ or -")
        if not 10 <= len(payload.password) <= 128 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", payload.email):
            raise HTTPException(422, "Invalid password or email")
        backend = config.get("shadnet", {})
        if not backend.get("enabled"):
            raise HTTPException(503, "Local shadNet backend is disabled")
        executable = Path(backend.get("executable", ""))
        if not executable.is_absolute():
            executable = root / executable
        settings = configparser.ConfigParser(interpolation=None)
        settings.read(executable.parent / "shadnet.cfg", encoding="utf-8")
        secret = settings.get("General", "RegistrationSecretKey", fallback="")
        try:
            register_account(backend.get("registration_host", "127.0.0.1"),
                             int(backend.get("registration_port", 31313)),
                             payload.npid, payload.password, payload.email, secret)
        except ShadNetError as error:
            raise HTTPException(502, str(error)) from error
        if not player_by_npid(db, payload.npid):
            db.add(Player(npid=payload.npid))
        audit(db, "admin", "shadnet.register", payload.npid)
        db.commit()
        return {"ok": True, "npid": payload.npid}

    @app.get("/api/v1/admin/profiles")
    def admin_profiles(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        return [profile_dict(p) for p in db.scalars(select(Profile)).all()]

    @app.post("/api/v1/admin/profiles")
    def admin_profile_create(payload: ProfileInput, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        if db.get(Profile, payload.id):
            raise HTTPException(409, "Profile exists")
        db.add(Profile(**payload.model_dump()))
        audit(db, "admin", "profile.create", payload.id)
        db.commit()
        return {"ok": True}

    @app.get("/api/v1/admin/servers")
    def admin_servers(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        return [server_dict(s, db) for s in db.scalars(select(Server)).all()]

    @app.post("/api/v1/admin/servers")
    def admin_server_create(payload: ServerInput, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        if not db.get(Profile, payload.profile_id):
            raise HTTPException(422, "Unknown profile")
        row = Server(**payload.model_dump(), mode="managed")
        db.add(row)
        db.flush()
        audit(db, "admin", "server.create", str(row.id))
        db.commit()
        return server_dict(row, db)

    @app.put("/api/v1/admin/servers/{server_id}")
    def admin_server_update(server_id: int, payload: ServerInput, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        row = db.get(Server, server_id)
        if not row:
            raise HTTPException(404, "Server not found")
        if not db.get(Profile, payload.profile_id):
            raise HTTPException(422, "Unknown profile")
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        audit(db, "admin", "server.update", str(row.id))
        db.commit()
        return server_dict(row, db)

    @app.get("/api/v1/admin/players")
    def admin_players(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        result = []
        for p in db.scalars(select(Player).order_by(Player.id.desc()).limit(500)).all():
            result.append({"id": p.id, "npid": p.npid, "created_at": p.created_at,
                           "last_seen": p.last_seen, "banned_until": p.banned_until,
                           "ban_reason": p.ban_reason, "registered": bool(p.password_hash)})
        return result

    @app.post("/api/v1/admin/players/{player_id}/ban")
    def admin_ban(player_id: int, payload: BanInput, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        player = db.get(Player, player_id)
        if not player:
            raise HTTPException(404, "Player not found")
        player.banned_until = payload.until if payload.until and payload.until > now() else None
        player.ban_reason = payload.reason.strip() if player.banned_until else None
        if player.banned_until:
            for conn in db.scalars(select(Connection).where(
                    Connection.player_id == player.id, Connection.ended_at.is_(None))).all():
                conn.ended_at = now()
                conn.disconnect_reason = "banned"
            for token in db.scalars(select(Token).where(Token.player_id == player.id)).all():
                db.delete(token)
        audit(db, "admin", "player.ban" if player.banned_until else "player.unban", player.npid)
        db.commit()
        return {"ok": True}

    @app.get("/api/v1/admin/connections")
    def admin_connections(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        rows = db.scalars(select(Connection).order_by(Connection.id.desc()).limit(500)).all()
        return [{"id": c.id, "npid": db.get(Player, c.player_id).npid,
                 "server_id": c.server_id, "client_ip": c.client_ip,
                 "started_at": c.started_at, "last_seen": c.last_seen,
                 "ended_at": c.ended_at, "disconnect_reason": c.disconnect_reason} for c in rows]

    @app.post("/api/v1/admin/connections/{conn_id}/disconnect")
    def admin_disconnect(conn_id: int, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        row = db.get(Connection, conn_id)
        if not row:
            raise HTTPException(404, "Connection not found")
        row.ended_at = now()
        row.disconnect_reason = "admin"
        audit(db, "admin", "connection.disconnect", str(conn_id))
        db.commit()
        return {"ok": True}

    @app.get("/api/v1/admin/feedback")
    def admin_feedback(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        rows = db.scalars(select(Feedback).order_by(Feedback.id.desc()).limit(500)).all()
        return [{"id": f.id, "name": f.name, "message": f.message,
                 "status": f.status, "created_at": f.created_at} for f in rows]

    @app.post("/api/v1/admin/feedback/{feedback_id}/resolve")
    def admin_feedback_resolve(feedback_id: int, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        row = db.get(Feedback, feedback_id)
        if not row:
            raise HTTPException(404, "Feedback not found")
        row.status = "resolved"
        audit(db, "admin", "feedback.resolve", str(feedback_id))
        db.commit()
        return {"ok": True}

    @app.get("/api/v1/admin/mods")
    def admin_mods(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        return [mod_dict(m) for m in db.scalars(select(Mod).order_by(Mod.id.desc())).all()]

    @app.post("/api/v1/admin/mods")
    async def admin_mod_upload(slug: str = Form(...), name: str = Form(...), version: str = Form(...),
                               profile_id: str = Form(...), archive: UploadFile = File(...),
                               _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        if not SLUG.fullmatch(slug) or not name.strip() or len(name) > 160 or not version or len(version) > 40:
            raise HTTPException(422, "Invalid mod metadata")
        if not db.get(Profile, profile_id):
            raise HTTPException(422, "Unknown profile")
        payload = await archive.read(MAX_MOD_BYTES + 1)
        if len(payload) > MAX_MOD_BYTES:
            raise HTTPException(413, "Archive exceeds 100 MiB")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as source:
                entries = source.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > 500 * 1024 * 1024:
                    raise ValueError("Archive expands too large")
                for item in entries:
                    path = item.filename.replace("\\", "/")
                    parts = path.rstrip("/").split("/")
                    if (path.startswith("/") or "\\" in item.filename or
                            any(part in ("", ".", "..") or ":" in part or part.rstrip(" .") != part or
                                part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL",
                                    *(f"COM{i}" for i in range(1, 10)),
                                    *(f"LPT{i}" for i in range(1, 10))} for part in parts)):
                        raise ValueError("Unsafe archive path")
                    if (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Symlinks are unsupported")
                if not any(x.filename == "mod.json" for x in entries):
                    raise ValueError("mod.json is required at archive root")
                manifest = json.loads(source.read("mod.json"))
                if manifest.get("slug") != slug or manifest.get("version") != version:
                    raise ValueError("mod.json slug/version mismatch")
                if source.testzip():
                    raise ValueError("Corrupt ZIP entry")
        except (ValueError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
            raise HTTPException(422, f"Invalid mod package: {error}") from error
        digest = hashlib.sha256(payload).hexdigest()
        folder = root / "data" / "mods"
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{digest}.zip"
        target = folder / filename
        if not target.exists():
            target.write_bytes(payload)
        existing = db.scalar(select(Mod).where(Mod.slug == slug, Mod.version == version, Mod.profile_id == profile_id))
        if existing:
            raise HTTPException(409, "Mod version already exists")
        row = Mod(slug=slug, name=name.strip(), version=version, profile_id=profile_id,
                  sha256=digest, size=len(payload), filename=filename)
        db.add(row)
        db.flush()
        audit(db, "admin", "mod.upload", f"{slug}@{version}")
        db.commit()
        return mod_dict(row)

    @app.post("/api/v1/admin/mods/{mod_id}/disable")
    def admin_mod_disable(mod_id: int, _token: Token = Depends(admin_write), db: Session = Depends(get_db)):
        mod = db.get(Mod, mod_id)
        if not mod:
            raise HTTPException(404, "Mod not found")
        mod.enabled = False
        audit(db, "admin", "mod.disable", str(mod_id))
        db.commit()
        return {"ok": True}

    @app.get("/api/v1/admin/audit")
    def admin_audit(_token: Token = Depends(admin_read), db: Session = Depends(get_db)):
        rows = db.scalars(select(Audit).order_by(Audit.id.desc()).limit(200)).all()
        return [{"id": a.id, "actor": a.actor, "action": a.action,
                 "detail": a.detail, "created_at": a.created_at} for a in rows]

    @app.post("/api/v1/host/servers")
    def host_server_create(payload: ServerInput, player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        if not db.get(Profile, payload.profile_id):
            raise HTTPException(422, "Unknown profile")
        if db.scalar(select(func.count(Server.id)).where(Server.owner_id == player.id, Server.enabled.is_(True))) >= 3:
            raise HTTPException(409, "Host server limit reached")
        row = Server(**payload.model_dump(), mode="player_hosted", owner_id=player.id, last_heartbeat=now())
        db.add(row)
        db.flush()
        audit(db, player.npid, "host.create", str(row.id))
        db.commit()
        return server_dict(row, db)

    @app.get("/api/v1/host/servers")
    def host_servers(player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        return [server_dict(s, db) for s in db.scalars(select(Server).where(Server.owner_id == player.id)).all()]

    @app.post("/api/v1/host/servers/{server_id}/heartbeat")
    def host_server_heartbeat(server_id: int, player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        row = db.get(Server, server_id)
        if not row or row.owner_id != player.id or row.mode != "player_hosted":
            raise HTTPException(403, "Server owner required")
        row.last_heartbeat = now()
        db.commit()
        return {"ok": True}

    @app.delete("/api/v1/host/servers/{server_id}")
    def host_server_disable(server_id: int, player: Player = Depends(player_auth), db: Session = Depends(get_db)):
        row = db.get(Server, server_id)
        if not row or row.owner_id != player.id or row.mode != "player_hosted":
            raise HTTPException(403, "Server owner required")
        row.enabled = False
        audit(db, player.npid, "host.disable", str(server_id))
        db.commit()
        return {"ok": True}

    return app
