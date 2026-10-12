from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path | None = None) -> dict:
    path = Path(path or os.environ.get("BB_SERVER_CONFIG", ROOT / "config.json")).resolve()
    if not path.exists():
        password = secrets.token_urlsafe(18)
        config = {
            "listen_host": "127.0.0.1",
            "listen_port": 18080,
            "public_base_url": "http://127.0.0.1:18080",
            "database_url": "sqlite:///data/server.db",
            "admin": {"username": "admin", "password": password},
            "session_secret": secrets.token_hex(32),
            "allow_player_registration": True,
            "shadnet": {
                "enabled": False,
                "executable": "",
                "arguments": [],
                "status_url": "http://127.0.0.1:31315/status",
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(f"Created {path}\nAdmin username: admin\nAdmin password: {password}", flush=True)
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    if not config.get("admin", {}).get("username") or not config["admin"].get("password"):
        raise ValueError("config.json requires admin.username and admin.password")
    if len(config.get("session_secret", "")) < 32:
        raise ValueError("config.json session_secret must be at least 32 characters")
    if not isinstance(config.get("listen_port"), int) or not 1 <= config["listen_port"] <= 65535:
        raise ValueError("invalid listen_port")
    return config
