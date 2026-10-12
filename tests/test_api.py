import hashlib
import io
import json
import zipfile

from fastapi.testclient import TestClient

from app.api import PROFILE_ID, create_app


def client_for(tmp_path):
    config = {
        "listen_host": "127.0.0.1", "listen_port": 18080,
        "public_base_url": "http://127.0.0.1:18080",
        "database_url": "sqlite:///data/server.db",
        "admin": {"username": "admin", "password": "test-password"},
        "session_secret": "a" * 64, "allow_player_registration": True,
        "shadnet": {"enabled": False},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return TestClient(create_app(path))


def admin(client):
    response = client.post("/api/v1/admin/login", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    return {"X-BB-CSRF": response.json()["csrf"]}


def server_payload(**changes):
    return {"name": "Local co-op", "profile_id": PROFILE_ID, "host": "127.0.0.1",
            "port": 31313, "webapi_url": "http://127.0.0.1:31315",
            "community_url": "https://thehuntersdream.com", "max_players": 3,
            "enabled": True, **changes}


def test_profiles_admin_auth_and_server_listing(tmp_path):
    client = client_for(tmp_path)
    assert client.get("/api/v1/profiles").json()[0]["id"] == PROFILE_ID
    assert client.get("/api/v1/admin/stats").status_code == 401
    assert client.get("/api/v1/admin/analytics").status_code == 401
    csrf = admin(client)
    assert client.post("/api/v1/admin/servers", json=server_payload()).status_code == 403
    created = client.post("/api/v1/admin/servers", headers=csrf, json=server_payload())
    assert created.status_code == 200, created.text
    server = created.json()
    assert server["mode"] == "managed"
    assert len(client.get("/api/v1/servers", params={"profile_id": PROFILE_ID}).json()) == 1
    assert client.get("/api/v1/servers", params={"profile_id": "other-version"}).json() == []
    update = client.put(f"/api/v1/admin/servers/{server['id']}", headers=csrf,
                        json=server_payload(enabled=False))
    assert update.status_code == 200
    assert client.get("/api/v1/servers").json() == []


def test_presence_ban_and_feedback_are_persisted(tmp_path):
    client = client_for(tmp_path)
    csrf = admin(client)
    server = client.post("/api/v1/admin/servers", headers=csrf, json=server_payload()).json()
    started = client.post("/api/v1/connections/start", json={"npid": "HunterOne", "server_id": server["id"]})
    assert started.status_code == 200, started.text
    conn = started.json()
    assert client.get("/api/v1/admin/stats").json()["online_players"] == 1
    public = client.get("/api/v1/public/stats").json()
    assert public["online_hunters"] == 1
    assert public["active_servers"] == 1
    analytics = client.get("/api/v1/admin/analytics").json()
    assert analytics["total_joins"] == 1
    assert analytics["joins_24h"] == 1
    assert analytics["server_activity"][0]["online"] == 1
    assert len(analytics["daily_joins"]) == 7
    assert analytics["gameplay_telemetry"]["available"] is False
    bearer = {"Authorization": f"Bearer {conn['token']}"}
    assert client.post(f"/api/v1/connections/{conn['connection_id']}/heartbeat", headers=bearer).status_code == 200
    players = client.get("/api/v1/admin/players").json()
    assert players[0]["npid"] == "HunterOne"
    banned = client.post(f"/api/v1/admin/players/{players[0]['id']}/ban", headers=csrf,
                         json={"until": 4102444800, "reason": "test"})
    assert banned.status_code == 200
    assert client.post("/api/v1/connections/start", json={"npid": "HunterOne", "server_id": server["id"]}).status_code == 403
    assert client.post("/api/v1/connections/start", json={"npid": "hunterone", "server_id": server["id"]}).status_code == 403
    assert client.get("/api/v1/admin/connections").json()[0]["disconnect_reason"] == "banned"
    assert client.post("/api/v1/feedback", json={"name": "HunterOne", "message": "Please add a new event"}).status_code == 200
    assert client.get("/api/v1/admin/feedback").json()[0]["message"] == "Please add a new event"
    assert client_for(tmp_path).get("/api/v1/servers").status_code == 200


def test_player_host_room_and_mod_delivery(tmp_path):
    client = client_for(tmp_path)
    csrf = admin(client)
    for name in ("HostOne", "GuestOne"):
        assert client.post("/api/v1/players/register", json={"npid": name, "password": "secret-value-123"}).status_code == 200
    host_token = client.post("/api/v1/players/login", json={"npid": "HostOne", "password": "secret-value-123"}).json()["token"]
    guest_token = client.post("/api/v1/players/login", json={"npid": "GuestOne", "password": "secret-value-123"}).json()["token"]
    host = {"Authorization": f"Bearer {host_token}"}
    guest = {"Authorization": f"Bearer {guest_token}"}
    created = client.post("/api/v1/host/servers", headers=host, json=server_payload(name="Host room"))
    assert created.status_code == 200, created.text
    server_id = created.json()["id"]
    room = client.post("/api/v1/rooms", headers=host, json={"server_id": server_id,
                       "title": "Central Yharnam", "capacity": 2, "password": "bells"})
    assert room.status_code == 200, room.text
    room_id = room.json()["id"]
    assert client.post(f"/api/v1/rooms/{room_id}/join", headers=guest, json={"password": "wrong"}).status_code == 403
    joined = client.post(f"/api/v1/rooms/{room_id}/join", headers=guest, json={"password": "bells"})
    assert joined.json()["server"]["id"] == server_id
    assert client.get("/api/v1/rooms", params={"profile_id": PROFILE_ID}).json()[0]["members"] == 2

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("mod.json", json.dumps({"slug": "sample-mod", "version": "1.0"}))
        package.writestr("dvdroot_ps4/chr/example.dcx", b"sample")
    raw = archive.getvalue()
    upload = client.post("/api/v1/admin/mods", headers=csrf,
                         data={"slug": "sample-mod", "name": "Sample Mod", "version": "1.0", "profile_id": PROFILE_ID},
                         files={"archive": ("sample.zip", raw, "application/zip")})
    assert upload.status_code == 200, upload.text
    mod = upload.json()
    assert mod["sha256"] == hashlib.sha256(raw).hexdigest()
    assert client.get(mod["download_url"]).content == raw
    assert client.get("/api/v1/mods", params={"profile_id": "other-version"}).json() == []
