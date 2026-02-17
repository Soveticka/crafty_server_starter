"""Minimal Minecraft Java Edition protocol helpers.

Implements just enough of the protocol to:
- Parse a Handshake packet.
- Respond to a Status (Server List Ping) request with a fake MOTD.
- Respond to a Login Start with a Disconnect (kick) message.

Reference: https://minecraft.wiki/w/Protocol
"""

from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass
from typing import Any

# =====================================================================
# VarInt helpers
# =====================================================================


def read_varint(stream: io.BytesIO) -> int:
    """Read a Minecraft VarInt from a byte stream."""
    result = 0
    for i in range(5):  # VarInt is at most 5 bytes
        byte = stream.read(1)
        if not byte:
            raise EOFError("Unexpected end of stream while reading VarInt")
        b = byte[0]
        result |= (b & 0x7F) << (7 * i)
        if not (b & 0x80):
            break
    else:
        raise ValueError("VarInt is too big")
    # Sign-extend for 32-bit
    if result & (1 << 31):
        result -= 1 << 32
    return result


def write_varint(value: int) -> bytes:
    """Encode an integer as a Minecraft VarInt."""
    # Treat as unsigned 32-bit for encoding.
    if value < 0:
        value += 1 << 32
    out = bytearray()
    for _ in range(5):
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        out.append(byte)
        if not value:
            break
    return bytes(out)


def read_utf(stream: io.BytesIO) -> str:
    """Read a Minecraft-style UTF-8 string (VarInt length-prefixed)."""
    length = read_varint(stream)
    data = stream.read(length)
    if len(data) < length:
        raise EOFError("Unexpected end of stream while reading string")
    return data.decode("utf-8")


def write_utf(value: str) -> bytes:
    """Encode a string as a Minecraft-style VarInt-prefixed UTF-8 string."""
    encoded = value.encode("utf-8")
    return write_varint(len(encoded)) + encoded


def read_unsigned_short(stream: io.BytesIO) -> int:
    """Read a big-endian unsigned short."""
    data = stream.read(2)
    if len(data) < 2:
        raise EOFError("Unexpected end of stream while reading unsigned short")
    return struct.unpack(">H", data)[0]


# =====================================================================
# Packet framing
# =====================================================================


async def read_packet(reader: Any) -> tuple[int, io.BytesIO]:
    """Read a single MC packet from an asyncio StreamReader.

    Returns (packet_id, payload_stream).  The payload stream's position
    is right after the packet ID.
    """
    # Read the length VarInt byte-by-byte from the async reader.
    length = await _read_varint_async(reader)
    if length <= 0:
        raise EOFError("Invalid packet length")
    if length > 2 * 1024 * 1024:  # 2 MB sanity cap
        raise ValueError(f"Packet too large: {length} bytes")
    data = await reader.readexactly(length)
    stream = io.BytesIO(data)
    packet_id = read_varint(stream)
    return packet_id, stream


async def _read_varint_async(reader: Any) -> int:
    """Read a VarInt one byte at a time from an asyncio StreamReader."""
    result = 0
    for i in range(5):
        byte = await reader.readexactly(1)
        b = byte[0]
        result |= (b & 0x7F) << (7 * i)
        if not (b & 0x80):
            break
    else:
        raise ValueError("VarInt too big")
    if result & (1 << 31):
        result -= 1 << 32
    return result


def build_packet(packet_id: int, payload: bytes) -> bytes:
    """Frame a packet: length-prefix(packet_id + payload)."""
    inner = write_varint(packet_id) + payload
    return write_varint(len(inner)) + inner


# =====================================================================
# Parsed packets
# =====================================================================


@dataclass
class Handshake:
    """Client → Server handshake (packet 0x00 in the handshake state)."""

    protocol_version: int
    server_address: str
    server_port: int
    next_state: int  # 1 = Status, 2 = Login

    @classmethod
    def parse(cls, stream: io.BytesIO) -> Handshake:
        protocol_version = read_varint(stream)
        server_address = read_utf(stream)
        server_port = read_unsigned_short(stream)
        next_state = read_varint(stream)
        return cls(protocol_version, server_address, server_port, next_state)


@dataclass
class LoginStart:
    """Client → Server login start (packet 0x00 in the login state)."""

    player_name: str

    @classmethod
    def parse(cls, stream: io.BytesIO) -> LoginStart:
        name = read_utf(stream)
        # Modern protocol also has a UUID, but we only need the name.
        return cls(name)


# =====================================================================
# Response builders
# =====================================================================


def build_status_response(
    motd: str,
    version_name: str = "Hibernating",
    protocol: int = -1,
    max_players: int = 0,
    online_players: int = 0,
    favicon: str = "",
) -> bytes:
    """Build a Status Response packet (0x00 in the status state).

    Using ``protocol: -1`` makes the entry show as "incompatible" in the
    server list, but the MOTD and player counts still display.
    """
    payload: dict[str, Any] = {
        "version": {"name": version_name, "protocol": protocol},
        "players": {"max": max_players, "online": online_players, "sample": []},
        "description": {"text": motd},
    }
    if favicon:
        payload["favicon"] = favicon

    json_str = json.dumps(payload, ensure_ascii=False)
    return build_packet(0x00, write_utf(json_str))


def build_pong(payload_long: bytes) -> bytes:
    """Build a Pong packet (0x01 in the status state).

    *payload_long* is the raw 8-byte long from the Ping packet.
    """
    return build_packet(0x01, payload_long)


def build_disconnect(reason: str) -> bytes:
    """Build a Disconnect packet (0x00 in the login state).

    The reason is a JSON Chat component.
    """
    chat = json.dumps({"text": reason}, ensure_ascii=False)
    return build_packet(0x00, write_utf(chat))
