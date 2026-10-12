#!/usr/bin/env python3
"""Install a verified shadnet-p2p CI artifact for this OS (GitHub auth required)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from app.config import ROOT, load_config

REPO = "Wozzardman/shadnet-p2p"
COMMIT = "5a849a192fd4450593c57b10697316ac324c856e"
ARTIFACTS = {
    "Windows": (9269236902, "0407e0a17758dd6e06f06123276c0698b234cff77e7425cbf842b44af4bfbcb5", "prebuilt-win64"),
    "Linux": (9269224626, "509fd40dad158b4484cb3c4eabd2160b372bbe5dc9ccb4103246b3fbb2c4da41", "prebuilt-linux64"),
}


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if value:
        return value
    result = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                            text=True, capture_output=True, timeout=10,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    if result.returncode == 0:
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if fields.get("password"):
            return fields["password"]
    raise RuntimeError("GitHub Actions artifacts require GITHUB_TOKEN or a Git credential. Use build-backend instead.")


def json_api(url: str, credential: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + credential,
                           "Accept": "application/vnd.github+json", "User-Agent": "BloodbornePCServer"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def artifact_bytes(artifact_id: int, credential: str) -> bytes:
    url = f"https://api.github.com/repos/{REPO}/actions/artifacts/{artifact_id}/zip"
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + credential,
                                      "Accept": "application/vnd.github+json", "User-Agent": "BloodbornePCServer"})
    try:
        urllib.request.build_opener(NoRedirect).open(request, timeout=15)
        raise RuntimeError("GitHub did not redirect to an artifact")
    except urllib.error.HTTPError as error:
        if error.code not in (302, 303, 307):
            raise RuntimeError(f"Artifact download failed: HTTP {error.code}") from error
        signed_url = error.headers.get("Location")
    if not signed_url:
        raise RuntimeError("Artifact redirect lacks Location")
    # Deliberately omit the GitHub credential on the cross-host signed URL.
    result = bytearray()
    deadline = time.monotonic() + 90
    with urllib.request.urlopen(urllib.request.Request(signed_url, headers={"User-Agent": "BloodbornePCServer"}),
                                timeout=15) as response:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Artifact download exceeded 90 seconds")
            chunk = response.read(1 << 20)
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > 60 * 1024 * 1024:
                raise ValueError("Artifact exceeds 60 MiB")
    return bytes(result)


def extract(archive: bytes, system: str, destination: Path) -> str:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as outer:
        if system == "Windows":
            for member in outer.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                    raise ValueError("Unsafe artifact path")
                if member.filename == "vc_redist.x64.exe" or member.is_dir():
                    continue
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(outer.read(member))
            name = "shadnet.exe"
        else:
            nested = outer.read("shadnet-linux64.tar.gz")
            with tarfile.open(fileobj=io.BytesIO(nested), mode="r:gz") as tar:
                members = tar.getmembers()
                if len(members) != 1 or members[0].name != "shadnet" or not members[0].isfile():
                    raise ValueError("Unexpected Linux artifact layout")
                (destination / "shadnet").write_bytes(tar.extractfile(members[0]).read())
                (destination / "shadnet").chmod(0o755)
            name = "shadnet"
    return name


def main() -> None:
    system = platform.system()
    if system not in ARTIFACTS or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("Only x86-64 Windows and Linux artifacts are available")
    artifact_id, expected, directory = ARTIFACTS[system]
    credential = token()
    metadata = json_api(f"https://api.github.com/repos/{REPO}/actions/artifacts/{artifact_id}", credential)
    if metadata.get("expired") or metadata.get("digest") != "sha256:" + expected or \
            metadata.get("workflow_run", {}).get("head_sha") != COMMIT:
        raise RuntimeError("Artifact metadata differs from pinned commit or digest")
    raw = artifact_bytes(artifact_id, credential)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("Artifact SHA-256 mismatch")
    destination = ROOT / "third_party" / directory
    name = extract(raw, system, destination)
    for filename in ("worlds.cfg", "scoreboards.cfg"):
        data = json_api(f"https://api.github.com/repos/{REPO}/contents/{filename}?ref={COMMIT}", credential)
        (destination / filename).write_bytes(base64.b64decode(data["content"]))
    config = load_config(ROOT / "config.json")
    config["shadnet"]["enabled"] = True
    config["shadnet"]["executable"] = f"third_party/{directory}/{name}"
    (ROOT / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {COMMIT[:7]} and installed {name}. Start with run.bat or run.sh.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError, zipfile.BadZipFile) as error:
        raise SystemExit(f"Backend fetch failed: {error}") from error
