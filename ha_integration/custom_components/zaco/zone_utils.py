"""Zone/area cleaning and PointToGo utilities for ZACO vacuum.

CleanAreaData: base64-encoded 16-byte format (4 corners × 2 coords × int16 BE).
PointToGo: base64-encoded 4-byte format (1 point × 2 coords × int16 BE).

All coordinate properties use the same convention:
  - X stored directly, Y negated before packing
  - Each value packed as signed int16 big-endian (2 bytes)
"""

from __future__ import annotations

import base64
import struct

CLEAN_AREA_EMPTY = "AAAAAAAAAAAAAAAAAAAAAA=="


def encode_clean_area(
    x1: int, y1: int,
    x2: int, y2: int,
    x3: int, y3: int,
    x4: int, y4: int,
) -> str:
    """Encode 4 corner points to CleanAreaData base64 string.

    Args:
        x1..y4: Corner coordinates in robot units.

    Returns:
        Base64-encoded 16-byte string.
    """
    data = b""
    coords = [x1, y1, x2, y2, x3, y3, x4, y4]
    for i, val in enumerate(coords):
        if i % 2 == 1:  # Y values get negated
            val = -val
        data += struct.pack(">h", int(round(val)))
    return base64.b64encode(data).decode()


def decode_clean_area(b64_str: str) -> list[tuple[int, int]]:
    """Decode CleanAreaData base64 to 4 (x, y) corner points.

    Returns:
        List of 4 (x, y) tuples in robot coordinates.
    """
    data = base64.b64decode(b64_str)
    points: list[tuple[int, int]] = []
    for i in range(4):
        offset = i * 4
        x = struct.unpack(">h", data[offset : offset + 2])[0]
        y = -struct.unpack(">h", data[offset + 2 : offset + 4])[0]
        points.append((x, y))
    return points


def rect_to_corners(
    x1: int, y1: int, x2: int, y2: int,
) -> tuple[int, int, int, int, int, int, int, int]:
    """Convert 2-point axis-aligned rectangle to 4 corners (clockwise).

    Args:
        x1, y1: First corner (e.g. top-left).
        x2, y2: Opposite corner (e.g. bottom-right).

    Returns:
        (x1, y1, x2, y1, x2, y2, x1, y2) — clockwise from first corner.
    """
    return (x1, y1, x2, y1, x2, y2, x1, y2)


def encode_point_to_go(x: int, y: int) -> str:
    """Encode a target point for PointToGo as base64.

    Matches PointToGoBean.setPoint() from the APK:
    ByteBuffer.allocate(4).putShort(x).putShort(-y) → base64.

    Args:
        x, y: Target coordinates in robot units.

    Returns:
        Base64-encoded 4-byte string.
    """
    return base64.b64encode(
        struct.pack(">hh", int(round(x)), -int(round(y)))
    ).decode()


def decode_point_to_go(b64_str: str) -> tuple[int, int]:
    """Decode PointToGo PointData base64 to (x, y).

    Returns:
        (x, y) tuple in robot coordinates (Y un-negated).
    """
    data = base64.b64decode(b64_str)
    x, neg_y = struct.unpack(">hh", data[:4])
    return (x, -neg_y)
