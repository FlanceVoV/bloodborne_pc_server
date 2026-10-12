from __future__ import annotations

import subprocess
import sys
import secrets
from pathlib import Path

import uvicorn

from .api import create_app
from .config import ROOT, load_config


def main() -> None:
    config = load_config()
    process = None
    backend = config.get("shadnet", {})
    if backend.get("enabled"):
        raw = backend.get("executable", "")
        executable = Path(raw)
        if not executable.is_absolute():
            executable = ROOT / executable
        executable = executable.resolve()
        if not executable.is_file():
            raise SystemExit(f"shadNet executable missing: {executable}. Run build-backend first or disable shadnet.enabled.")
        backend_config = executable.parent / "shadnet.cfg"
        if not backend_config.exists():
            backend_config.write_text(
                "[General]\nHost=127.0.0.1\nUnsecuredPort=31313\nMatchingUdpPort=31314\n"
                "WebApiPort=31315\nMatching2Enabled=true\nBloodborneSeamlessCoop=false\n"
                "StatsEnabled=false\nRegistrationSecretKey=" + secrets.token_urlsafe(24) + "\n",
                encoding="utf-8")
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        log = (log_dir / "shadnet.log").open("ab")
        process = subprocess.Popen([str(executable), *backend.get("arguments", [])],
                                   cwd=executable.parent, stdout=log, stderr=subprocess.STDOUT)
        print(f"Started shadNet PID {process.pid}; log: {log.name}", flush=True)
    try:
        uvicorn.run(create_app(), host=config["listen_host"], port=config["listen_port"], log_level="info")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        if process:
            log.close()


if __name__ == "__main__":
    main()
