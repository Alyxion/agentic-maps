"""What do the vector tiles actually carry at z3–z5? (road coverage probe)

The continental-zoom road web (owner: "at least show the main roads here
like google") can only be styled from the tiles if the tiles carry motorway
geometry at those levels. This probe decodes the served MVTs — no styling,
no browser — and reports, per tile, the layers present and the `roads`
features broken down by kind/kind_detail, so the decision "style the tiles"
vs "ship a low-zoom road asset" is made from data, not from hope.

Pure-python MVT reader on purpose: the venv has no mapbox_vector_tile and
this needs exactly the subset a probe needs (layer names, feature tags,
geometry command count).

Usage: .venv/bin/python tools/probe_lowzoom_roads.py [base_url]
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8195"
TEMPLATE = BASE + "/api/v1/maps/vector/auto/tiles/{z}/{x}/{y}.mvt"

# Central/western Europe: Germany/France/Alps, plus one Iberia and one
# Scandinavia tile per level so partial coverage shows up as such.
TILES = [
    (3, 4, 2),
    (4, 8, 5), (4, 7, 5), (4, 8, 4),
    (5, 16, 10), (5, 15, 11), (5, 16, 11), (5, 15, 10), (5, 17, 10),
]


def _varint(buf, pos):
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(buf):
    """Yield (field_number, wire_type, value) over a protobuf message."""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            length, pos = _varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
        elif wire == 5:
            value = buf[pos:pos + 4]
            pos += 4
        elif wire == 1:
            value = buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"wire type {wire}")
        yield field, wire, value


def _value(buf):
    """MVT Value message -> python scalar."""
    for field, _, val in _fields(buf):
        if field == 1:
            return val.decode("utf-8", "replace")
        if field in (4, 5):
            return val
        if field == 6:                       # sint64, zigzag
            return (val >> 1) ^ -(val & 1)
        if field == 7:
            return bool(val)
        if field in (2, 3):
            return "<float>"
    return None


def _packed(buf):
    out, pos = [], 0
    while pos < len(buf):
        v, pos = _varint(buf, pos)
        out.append(v)
    return out


def decode_layer(buf):
    name, keys, values, features = "", [], [], []
    for field, _, val in _fields(buf):
        if field == 1:
            name = val.decode()
        elif field == 3:
            keys.append(val.decode())
        elif field == 4:
            values.append(_value(val))
        elif field == 2:
            features.append(val)
    decoded = []
    for feature in features:
        tags, geom_cmds = [], 0
        for f_field, _, f_val in _fields(feature):
            if f_field == 2:
                tags = _packed(f_val)
            elif f_field == 4:
                geom_cmds = len(_packed(f_val))
        props = {keys[tags[i]]: values[tags[i + 1]]
                 for i in range(0, len(tags) - 1, 2)}
        decoded.append({"props": props, "geom_len": geom_cmds})
    return name, decoded


def probe(z, x, y):
    data = urllib.request.urlopen(TEMPLATE.format(z=z, x=x, y=y)).read()
    layers = {}
    for field, _, val in _fields(data):
        if field == 3:
            name, feats = decode_layer(val)
            layers[name] = feats
    roads = layers.get("roads", [])
    by_kind = {}
    for r in roads:
        key = (r["props"].get("kind"), r["props"].get("kind_detail"))
        entry = by_kind.setdefault("/".join(str(k) for k in key), [0, 0])
        entry[0] += 1
        entry[1] += r["geom_len"]
    return {
        "tile": f"{z}/{x}/{y}",
        "bytes": len(data),
        "layers": {k: len(v) for k, v in sorted(layers.items())},
        "roads_by_kind": by_kind,
        "road_sample": roads[0]["props"] if roads else None,
    }


if __name__ == "__main__":
    for z, x, y in TILES:
        print(json.dumps(probe(z, x, y), ensure_ascii=False))
