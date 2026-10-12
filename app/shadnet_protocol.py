"""Small shadNet v1 account-registration client.

Only the documented Create command is used. This module is deliberately separate
from the lobby DB: it never stores a player's shadNet password.
"""
from __future__ import annotations

import socket
import struct


ERRORS = {
    0: "NoError", 1: "Malformed", 2: "Invalid", 3: "InvalidInput", 4: "TooSoon",
    10: "CreationError", 11: "CreationExistingUsername", 12: "CreationBannedEmail",
    13: "CreationExistingEmail", 23: "Unauthorized", 24: "DbFail",
}
HEADER = struct.Struct("<BHIQ")


class ShadNetError(Exception):
    pass


def varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number: int, value: str) -> bytes:
    data = value.encode("utf-8")
    return varint((number << 3) | 2) + varint(len(data)) + data


def recv_exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise ShadNetError("shadNet closed the connection")
        result.extend(chunk)
    return bytes(result)


def packet(sock: socket.socket) -> tuple[int, int, int, bytes]:
    kind, command, total, packet_id = HEADER.unpack(recv_exact(sock, HEADER.size))
    if total < HEADER.size or total > 8 * 1024 * 1024:
        raise ShadNetError("Invalid shadNet packet size")
    return kind, command, packet_id, recv_exact(sock, total - HEADER.size)


def register_account(host: str, port: int, npid: str, password: str, email: str,
                     secret_key: str = "") -> None:
    body = field(1, npid) + field(2, password) + field(4, email)
    if secret_key:
        body += field(5, secret_key)
    payload = struct.pack("<I", len(body)) + body
    try:
        with socket.create_connection((host, port), timeout=6) as sock:
            sock.settimeout(6)
            kind, _command, _id, info = packet(sock)
            if kind != 3 or len(info) < 4 or struct.unpack_from("<I", info)[0] != 1:
                raise ShadNetError("Incompatible shadNet protocol")
            sock.sendall(HEADER.pack(0, 2, HEADER.size + len(payload), 1) + payload)
            for _ in range(8):
                kind, command, packet_id, response = packet(sock)
                if kind == 1 and command == 2 and packet_id == 1:
                    error = response[0] if response else 1
                    if error:
                        raise ShadNetError(ERRORS.get(error, f"shadNet error {error}"))
                    return
            raise ShadNetError("No Create reply from shadNet")
    except OSError as error:
        raise ShadNetError(f"Could not reach shadNet: {error}") from error
