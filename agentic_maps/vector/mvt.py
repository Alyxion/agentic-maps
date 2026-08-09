"""Read geometry back out of a Mapbox Vector Tile.

The tiles we serve are the only place where the real course of a street is
written down — the geocoder answers with a single point, and the router only
follows the streets it happens to need. Anyone who wants to know where a
kerb actually runs (fitting a plot outline to a block, checking that an
outline sits flush with its access roads) has to read the tile.

Deliberately a reader, not a library: MVT is a small protobuf schema, and the
twenty lines of wire-format decoding here are cheaper than a dependency that
pulls in protobuf. Only what a caller needs is decoded — layer name, feature
attributes, and geometry as WGS84 coordinates. Polygons come back as their
rings, unclassified; nothing here needs winding order.

Spec: https://github.com/mapbox/vector-tile-spec/tree/master/2.1
"""

from __future__ import annotations

import math
import struct

# Wire types
_VARINT, _FIXED64, _BYTES, _FIXED32 = 0, 1, 2, 5

# Geometry commands (lower three bits of a command integer)
_MOVE_TO, _LINE_TO, _CLOSE_PATH = 1, 2, 7


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(buf: bytes, start: int = 0, end: int | None = None):
    """Yield ``(field_number, value)`` for one protobuf message.

    Length-delimited fields yield the raw slice; varints yield the int.
    Fixed widths yield their raw 4/8-byte slice — `Value` needs them, since
    a building height like 11.3 m is written as a float/double field, and
    skipping those silently dropped every non-integer attribute.
    """
    pos, end = start, len(buf) if end is None else end
    while pos < end:
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 0x7
        if wire == _VARINT:
            value, pos = _varint(buf, pos)
            yield field, value
        elif wire == _BYTES:
            length, pos = _varint(buf, pos)
            yield field, buf[pos:pos + length]
            pos += length
        elif wire == _FIXED64:
            yield field, buf[pos:pos + 8]
            pos += 8
        elif wire == _FIXED32:
            yield field, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")


def _packed_varints(buf: bytes) -> list[int]:
    out, pos, end = [], 0, len(buf)
    while pos < end:
        value, pos = _varint(buf, pos)
        out.append(value)
    return out


def _value(buf: bytes):
    """One `Value` message — exactly one of its seven typed fields is set."""
    for field, raw in _fields(buf):
        if field == 1:
            return raw.decode("utf-8", "replace")
        if field == 2:                          # float (fixed32)
            return struct.unpack("<f", raw)[0]
        if field == 3:                          # double (fixed64)
            return struct.unpack("<d", raw)[0]
        if field in (4, 5):
            return raw
        if field == 6:
            return (raw >> 1) ^ -(raw & 1)      # sint64 zig-zag
        if field == 7:
            return bool(raw)
    return None


def _tile_to_lonlat(z: int, x: int, y: int, extent: int, px: int, py: int) -> tuple[float, float]:
    """Tile-local integer coordinates to WGS84."""
    scale = 1 << z
    lon = (x + px / extent) / scale * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * (y + py / extent) / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def _geometry(commands: list[int], z: int, x: int, y: int, extent: int) -> list[list[tuple[float, float]]]:
    """Decode a feature's command stream into WGS84 rings/lines."""
    parts: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    cx = cy = 0
    i = 0
    while i < len(commands):
        header = commands[i]
        i += 1
        command, count = header & 0x7, header >> 3
        if command == _CLOSE_PATH:
            if current:
                current.append(current[0])
            continue
        for _ in range(count):
            dx, dy = commands[i], commands[i + 1]
            i += 2
            cx += (dx >> 1) ^ -(dx & 1)
            cy += (dy >> 1) ^ -(dy & 1)
            if command == _MOVE_TO:
                if current:
                    parts.append(current)
                current = []
            current.append(_tile_to_lonlat(z, x, y, extent, cx, cy))
    if current:
        parts.append(current)
    return parts


def read_tile(data: bytes, z: int, x: int, y: int, *, layers: set[str] | None = None) -> list[dict]:
    """Decode one tile into ``[{layer, type, id, props, parts}]``.

    ``parts`` are lists of ``(lon, lat)``; ``type`` is 1 point, 2 line,
    3 polygon; ``id`` is the feature's own id or None — the id is what lets a
    caller reading several tiles recognise the same building on both sides of
    a tile border. ``layers`` filters by layer name — reading only `roads`
    out of a tile that also carries buildings, land use and every label is
    most of the speed.
    """
    out: list[dict] = []
    for field, raw in _fields(data):
        if field != 3:                      # Tile.layers
            continue
        name, extent, keys, values, features = "", 4096, [], [], []
        for lfield, lraw in _fields(raw):
            if lfield == 1:
                name = lraw.decode("utf-8", "replace")
            elif lfield == 2:
                features.append(lraw)
            elif lfield == 3:
                keys.append(lraw.decode("utf-8", "replace"))
            elif lfield == 4:
                values.append(_value(lraw))
            elif lfield == 5:
                extent = lraw
        if layers is not None and name not in layers:
            continue
        for feature in features:
            tags: list[int] = []
            feature_id, geom_type, commands = None, 0, []
            for ffield, fraw in _fields(feature):
                if ffield == 1:
                    feature_id = fraw
                elif ffield == 2:
                    tags = _packed_varints(fraw)
                elif ffield == 3:
                    geom_type = fraw
                elif ffield == 4:
                    commands = _packed_varints(fraw)
            props = {keys[tags[i]]: values[tags[i + 1]]
                     for i in range(0, len(tags) - 1, 2)
                     if tags[i] < len(keys) and tags[i + 1] < len(values)}
            out.append({"layer": name, "type": geom_type, "id": feature_id,
                        "props": props,
                        "parts": _geometry(commands, z, x, y, extent)})
    return out
