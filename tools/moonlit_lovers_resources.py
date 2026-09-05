#!/usr/bin/env python3
"""Shared Moonlit Lovers resource-container helpers.

Moonlit Lovers uses two resource layouts behind the same PIDX0 container
header:

* Named PIDX node tables (SCENARIO, SLG, GADAT000/030/031/032, GAML, SE,
  SVOICE).  Leaf nodes are 24-byte records and may point to either compressed
  `` 3;1`` Ikusa-LZ streams or stored/XOR `` 3;0`` streams.
* FSTS banks (SLGSTAGE, SLGRES, SLGEFF, ADV).  These have no PIDX leaf table;
  each FSTS bank owns a 16-byte resource table whose offsets are relative to
  the FSTS header.  These resources also use both `` 3;1`` and `` 3;0``.

Older Moonlit Lovers tools reused ``galaxy_angel_build.records()``, which only
recognizes 0x800-aligned `` 3;1`` PIDX leaves.  That silently missed stored
selection scripts, stored battle resources, and all non-sector-aligned FSTS
resources.  This module is the canonical iterator for exhaustive extraction
and later reinsertion.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

import ikusa_lz


PIDX_MAGIC = b"PIDX0\0\0\0"
LZ_MAGIC = b" 3;1"
STORED_MAGIC = b" 3;0"
PIDX_NODE_OFFSET = 0x50
PIDX_NODE_SIZE = 24
FSTS_HEADER_SIZE = 32
FSTS_RECORD_SIZE = 16


@dataclass
class Resource:
    container: str
    source_kind: str
    offset: int
    raw_size: int
    compressed_size: int
    codec: str
    name: str | None = None
    path: str | None = None
    node_index: int | None = None
    record_positions: list[int] = field(default_factory=list)
    resource_ids: list[int] = field(default_factory=list)
    fsts_bases: list[int] = field(default_factory=list)

    @property
    def stable_id(self) -> str:
        return f"{self.container}:{self.offset:08x}"


def codec_at(data: bytes | bytearray | memoryview, offset: int) -> str:
    magic = bytes(data[offset : offset + 4])
    if magic == LZ_MAGIC:
        return "ikusa_lz"
    if magic == STORED_MAGIC:
        return "stored_xor"
    return f"unknown:{magic.hex()}"


def decompress_resource(
    data: bytes | bytearray | memoryview,
    offset: int,
    raw_size: int,
    compressed_size: int,
) -> bytes:
    """Decode one validated `` 3;1`` or `` 3;0`` resource exactly."""
    if offset < 0 or offset + 8 > len(data):
        raise ValueError(f"resource header outside container: {offset:#x}")
    magic = bytes(data[offset : offset + 4])
    declared = struct.unpack_from("<I", data, offset + 4)[0]
    if declared != raw_size:
        raise ValueError(
            f"raw-size mismatch at {offset:#x}: header={declared} table={raw_size}"
        )
    if magic == LZ_MAGIC:
        raw, consumed = ikusa_lz.decompress(data, offset)
        if len(raw) != raw_size or consumed != compressed_size:
            raise ValueError(
                f"LZ size mismatch at {offset:#x}: "
                f"raw={len(raw)}/{raw_size} compressed={consumed}/{compressed_size}"
            )
        return raw
    if magic == STORED_MAGIC:
        expected = raw_size + 8
        if compressed_size != expected:
            raise ValueError(
                f"stored size mismatch at {offset:#x}: {compressed_size}!={expected}"
            )
        end = offset + expected
        if end > len(data):
            raise ValueError(f"stored resource truncated at {offset:#x}")
        return bytes(value ^ ikusa_lz.KEY for value in data[offset + 8 : end])
    raise ValueError(f"unsupported resource codec {magic!r} at {offset:#x}")


def encode_stored(raw: bytes) -> bytes:
    return (
        STORED_MAGIC
        + struct.pack("<I", len(raw))
        + bytes(value ^ ikusa_lz.KEY for value in raw)
    )


def pidx_header(container: bytes | bytearray | memoryview) -> dict:
    if bytes(container[:8]) != PIDX_MAGIC:
        raise ValueError("not a PIDX0 container")
    if len(container) < PIDX_NODE_OFFSET:
        raise ValueError("truncated PIDX0 header")
    return {
        "node_count": struct.unpack_from("<I", container, 0x10)[0],
        "node_table_end": struct.unpack_from("<I", container, 0x18)[0],
        "secondary_size": struct.unpack_from("<I", container, 0x1C)[0],
        "string_base": struct.unpack_from("<I", container, 0x20)[0],
        "string_size": struct.unpack_from("<I", container, 0x24)[0],
    }


def parse_named_nodes(
    container: bytes | bytearray | memoryview,
) -> tuple[list[tuple[int, int, int, int, int, int]], dict[int, str], dict[int, str]]:
    """Parse a named PIDX node table using CP932 names.

    Returns ``(nodes, names_by_index, paths_by_index)``.  An empty node table is
    valid and signals the FSTS-backed PIDX variant.
    """
    header = pidx_header(container)
    count = header["node_count"]
    if count == 0:
        return [], {}, {}
    table_end = PIDX_NODE_OFFSET + count * PIDX_NODE_SIZE
    if table_end > len(container):
        raise ValueError(f"PIDX node table outside container: count={count}")
    if header["node_table_end"] not in (0, table_end):
        raise ValueError(
            f"PIDX node-table end mismatch: {header['node_table_end']:#x}!={table_end:#x}"
        )
    string_base = header["string_base"]
    if not 0 <= string_base < len(container):
        raise ValueError(f"invalid PIDX string base {string_base:#x}")

    nodes = [
        struct.unpack_from("<6I", container, PIDX_NODE_OFFSET + index * PIDX_NODE_SIZE)
        for index in range(count)
    ]
    names: dict[int, str] = {}
    for index, node in enumerate(nodes):
        name_offset = node[1]
        start = string_base + name_offset
        if not string_base <= start < len(container):
            raise ValueError(f"invalid PIDX name offset at node {index}: {name_offset:#x}")
        end = bytes(container).find(b"\0", start)
        if end < 0:
            raise ValueError(f"unterminated PIDX name at node {index}")
        raw_name = bytes(container[start:end])
        try:
            name = raw_name.decode("cp932")
        except UnicodeDecodeError:
            name = raw_name.decode("cp932", errors="replace")
        names[index] = name

    paths: dict[int, str] = {}
    visiting: set[int] = set()

    def walk(index: int, prefix: str = "") -> None:
        if not 0 <= index < len(nodes):
            raise ValueError(f"PIDX child index out of range: {index}")
        if index in visiting:
            raise ValueError(f"cyclic PIDX directory tree at node {index}")
        visiting.add(index)
        kind, _name_offset, child_count, first_child, _raw_size, _compressed_size = nodes[index]
        name = names[index]
        path = f"{prefix}/{name}".strip("/")
        paths[index] = path
        if kind == 1:
            if first_child + child_count > len(nodes):
                raise ValueError(f"PIDX child range outside table at {path}")
            for child in range(first_child, first_child + child_count):
                walk(child, path)
        elif kind != 0:
            raise ValueError(f"unknown PIDX node type {kind} at {path}")
        visiting.remove(index)

    walk(0)
    # Some archives retain unreachable/legacy nodes.  Keep them extractable and
    # give them their literal node name rather than silently dropping data.
    for index in range(len(nodes)):
        paths.setdefault(index, names[index])
    return nodes, names, paths


def pidx_resources(container: bytes | bytearray | memoryview, stem: str) -> list[Resource]:
    nodes, names, paths = parse_named_nodes(container)
    resources: list[Resource] = []
    for index, node in enumerate(nodes):
        kind, _name_offset, _child_count, data_offset, raw_size, compressed_size = node
        if kind != 0:
            continue
        if data_offset + 8 > len(container):
            raise ValueError(f"{stem} PIDX leaf outside container: node={index}")
        codec = codec_at(container, data_offset)
        if codec.startswith("unknown"):
            raise ValueError(
                f"{stem} PIDX leaf has unknown codec at {data_offset:#x}: {codec}"
            )
        resources.append(
            Resource(
                container=stem.upper(),
                source_kind="pidx",
                offset=data_offset,
                raw_size=raw_size,
                compressed_size=compressed_size,
                codec=codec,
                name=names[index],
                path=paths[index],
                node_index=index,
                record_positions=[PIDX_NODE_OFFSET + index * PIDX_NODE_SIZE + 12],
            )
        )
    return resources


def iter_fsts_entries(
    data: bytes | bytearray | memoryview,
) -> list[tuple[int, int, int, int, int, int]]:
    """Return every FSTS table entry.

    Tuple layout: ``(absolute_offset, raw_size, compressed_size, record_pos,
    resource_id, fsts_base)``.
    """
    entries: list[tuple[int, int, int, int, int, int]] = []
    raw_data = bytes(data)
    cursor = 0
    while True:
        base = raw_data.find(b"FSTS", cursor)
        if base < 0:
            break
        cursor = base + 4
        if base + FSTS_HEADER_SIZE > len(raw_data):
            continue
        _magic, count, header_size, table_size = struct.unpack_from("<4I", raw_data, base)
        if header_size != FSTS_HEADER_SIZE:
            continue
        if table_size != FSTS_HEADER_SIZE + count * FSTS_RECORD_SIZE:
            continue
        if base + table_size > len(raw_data):
            continue
        for index in range(count):
            record = base + FSTS_HEADER_SIZE + index * FSTS_RECORD_SIZE
            resource_id, relative_offset, raw_size, compressed_size = struct.unpack_from(
                "<4I", raw_data, record
            )
            absolute_offset = base + relative_offset
            if absolute_offset + 8 > len(raw_data):
                raise ValueError(
                    f"FSTS resource outside container: base={base:#x} offset={relative_offset:#x}"
                )
            entries.append(
                (
                    absolute_offset,
                    raw_size,
                    compressed_size,
                    record,
                    resource_id,
                    base,
                )
            )
    return entries


def fsts_resources(container: bytes | bytearray | memoryview, stem: str) -> list[Resource]:
    grouped: dict[int, Resource] = {}
    for offset, raw_size, compressed_size, record, resource_id, base in iter_fsts_entries(container):
        codec = codec_at(container, offset)
        if codec.startswith("unknown"):
            raise ValueError(
                f"{stem} FSTS resource has unknown codec at {offset:#x}: {codec}"
            )
        current = grouped.get(offset)
        if current is None:
            current = Resource(
                container=stem.upper(),
                source_kind="fsts",
                offset=offset,
                raw_size=raw_size,
                compressed_size=compressed_size,
                codec=codec,
            )
            grouped[offset] = current
        elif (current.raw_size, current.compressed_size) != (raw_size, compressed_size):
            raise ValueError(
                f"{stem} duplicate FSTS offset with conflicting sizes at {offset:#x}"
            )
        current.record_positions.append(record)
        current.resource_ids.append(resource_id)
        current.fsts_bases.append(base)
    return [grouped[offset] for offset in sorted(grouped)]


def container_resources(
    container: bytes | bytearray | memoryview,
    stem: str,
) -> list[Resource]:
    header = pidx_header(container)
    if header["node_count"]:
        return pidx_resources(container, stem)
    resources = fsts_resources(container, stem)
    if not resources:
        raise ValueError(f"{stem}: PIDX0 has neither named nodes nor valid FSTS banks")
    return resources


def pidx_record_map(
    container: bytes | bytearray | memoryview,
    stem: str,
) -> dict[int, tuple[int, int, int]]:
    """Compatibility map matching ``galaxy_angel_build.records`` shape.

    Unlike the legacy helper, this includes unaligned and stored PIDX leaves.
    FSTS-backed containers intentionally return an empty map because they do not
    mirror leaf tuples through the named PIDX table.
    """
    header = pidx_header(container)
    if not header["node_count"]:
        return {}
    return {
        item.offset: (item.record_positions[0], item.raw_size, item.compressed_size)
        for item in pidx_resources(container, stem)
    }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_component(value: str) -> str:
    """Make a PIDX component safe on Windows without discarding Japanese text."""
    translated = value
    for char in '<>:"/\\|?*':
        translated = translated.replace(char, "_")
    translated = translated.rstrip(" .")
    return translated or "_"


def safe_resource_path(resource: Resource, inferred_suffix: str = ".bin") -> Path:
    if resource.path:
        parts = [safe_component(part) for part in resource.path.replace("\\", "/").split("/")]
        return Path(*parts)
    return Path(f"block_{resource.offset:08x}{inferred_suffix}")
