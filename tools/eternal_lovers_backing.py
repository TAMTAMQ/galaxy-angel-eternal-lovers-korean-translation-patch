#!/usr/bin/env python3
"""Hand out sectors from the reserved backing region.

A container that outgrew its original allocation keeps its extent and points the
records that no longer fit at this region instead, the way galaxy_angel_build.py
points GADAT001's oversized records at an appended copy.  The region is created
by eternal_lovers_reserve_backing_region.py and shared by every container, so
the cursor lives in its JSON file and is advanced as space is handed out.
"""

from __future__ import annotations

import json
from pathlib import Path

import galaxy_angel_build as builder

U32_MAX = 0xFFFFFFFF


class BackingRegion:
    def __init__(self, path: Path):
        self.path = path
        self.state = json.loads(path.read_text(encoding="utf-8"))

    @property
    def remaining(self) -> int:
        return self.state["offset"] + self.state["size"] - self.state["cursor"]

    def allocate(self, length: int, container_begin: int) -> int:
        """Reserve `length` bytes; returns the physical offset.

        Raises when the region is exhausted or when the result would not be
        expressible as a 32-bit offset from `container_begin`, so a build can
        never silently fall back to moving the container.
        """
        start = builder.align(self.state["cursor"], builder.SECTOR)
        end = start + length
        if end > self.state["offset"] + self.state["size"]:
            raise SystemExit(
                f"backing region exhausted: need {length} bytes, "
                f"{self.remaining} left in {self.path}"
            )
        if end - container_begin > U32_MAX:
            raise SystemExit(
                f"backing region is out of 32-bit reach of container base "
                f"{container_begin}: {end - container_begin:#x}"
            )
        self.state["cursor"] = end
        self.path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return start


def load(path: Path | None) -> BackingRegion | None:
    if path is None:
        return None
    return BackingRegion(path)
