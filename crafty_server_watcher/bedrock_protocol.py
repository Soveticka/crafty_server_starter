"""Minecraft Bedrock / RakNet protocol helpers for the hibernation proxy.

Implements just enough of the RakNet protocol to:
- Respond to Unconnected Ping (0x01) with Unconnected Pong (0x1C) showing
  the hibernation MOTD.
- Reject connection attempts (Open Connection Request 1, 0x05) so the
  client sees "unable to connect" — at which point we've already triggered
  the server start on the first valid ping.

Bedrock uses UDP on port 19132 (default). The protocol is documented at
https://wiki.vg/Raknet_Protocol and https://bedrock.dev.
"""

from __future__ import annotations

import struct

# 16-byte offline message ID (RakNet "magic")
RAKNET_MAGIC = bytes(
    [
        0x00,
        0xFF,
        0xFF,
        0x00,
        0xFE,
        0xFE,
        0xFE,
        0xFE,
        0xFD,
        0xFD,
        0xFD,
        0xFD,
        0x12,
        0x34,
        0x56,
        0x78,
    ]
)

# Packet IDs
ID_UNCONNECTED_PING = 0x01
ID_UNCONNECTED_PONG = 0x1C
ID_OPEN_CONNECTION_REQUEST_1 = 0x05
ID_OPEN_CONNECTION_REPLY_1 = 0x06
ID_OPEN_CONNECTION_REQUEST_2 = 0x07
ID_INCOMPATIBLE_PROTOCOL = 0x19

# Current RakNet protocol version used by Bedrock
RAKNET_PROTOCOL_VERSION = 11


def parse_unconnected_ping(data: bytes) -> tuple[int, int] | None:
    """Parse an Unconnected Ping packet.

    Returns (client_timestamp, client_guid) or None if the packet is invalid.
    """
    if len(data) < 33 or data[0] != ID_UNCONNECTED_PING:
        return None

    # Verify magic at offset 9
    if data[9:25] != RAKNET_MAGIC:
        return None

    client_time = struct.unpack(">Q", data[1:9])[0]
    client_guid = struct.unpack(">q", data[25:33])[0]
    return client_time, client_guid


def build_unconnected_pong(
    client_time: int,
    server_guid: int,
    motd: str,
    protocol_version: int = 729,
    version_name: str = "1.21.80",
    players_online: int = 0,
    max_players: int = 20,
    port_v4: int = 19132,
    port_v6: int = 19133,
) -> bytes:
    """Build an Unconnected Pong response.

    The server name string follows the Bedrock convention:
    MCPE;motd;protocol;version;online;max;guid;motd2;gamemode;gamemodenum;port4;port6
    """
    server_name = ";".join(
        [
            "MCPE",
            motd,
            str(protocol_version),
            version_name,
            str(players_online),
            str(max_players),
            str(server_guid),
            motd,  # MOTD line 2
            "Survival",
            "1",
            str(port_v4),
            str(port_v6),
        ]
    )

    name_bytes = server_name.encode("utf-8")

    buf = bytearray()
    buf.append(ID_UNCONNECTED_PONG)
    buf.extend(struct.pack(">Q", client_time))
    buf.extend(struct.pack(">q", server_guid))
    buf.extend(RAKNET_MAGIC)
    buf.extend(struct.pack(">H", len(name_bytes)))
    buf.extend(name_bytes)
    return bytes(buf)


def build_incompatible_protocol(server_guid: int) -> bytes:
    """Build an Incompatible Protocol Version response.

    This tells the client we don't support their RakNet version,
    effectively rejecting the connection attempt gracefully.
    """
    buf = bytearray()
    buf.append(ID_INCOMPATIBLE_PROTOCOL)
    buf.append(RAKNET_PROTOCOL_VERSION)
    buf.extend(RAKNET_MAGIC)
    buf.extend(struct.pack(">q", server_guid))
    return bytes(buf)


def is_open_connection_request_1(data: bytes) -> bool:
    """Check if a packet is an Open Connection Request 1."""
    if len(data) < 25 or data[0] != ID_OPEN_CONNECTION_REQUEST_1:
        return False
    return data[1:17] == RAKNET_MAGIC
