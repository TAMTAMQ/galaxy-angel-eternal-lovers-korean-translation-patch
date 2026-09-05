#!/usr/bin/env python3
"""Audit fixed jump offsets without allowing extra script records."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "work/galaxy_angel/source/scenario"
BUILT = ROOT / "work/galaxy_angel/build/scenario"
ANCHOR = re.compile(rb"(?m)^(?::L\d+|<\d+->IDS\d+)\r?\n")
DIALOGUE = re.compile(rb"(?ms)^@\d+\(\r?\n(.*?)^@@\)")
COMMAND = re.compile(rb"(?m)^\\[A-Za-z][A-Za-z0-9]*\([^\r\n]*\)\r?$")
NUMBER = re.compile(rb"(?<![A-Za-z0-9_])-?\d+")


def normalize_command(command: bytes) -> bytes:
    def normalize(match: re.Match[bytes]) -> bytes:
        token = match.group()
        sign = b"-" if token.startswith(b"-") else b""
        digits = token[len(sign):].lstrip(b"0") or b"0"
        return sign + digits
    return NUMBER.sub(normalize, command)


def main() -> None:
    mismatches = []
    command_mismatches = []
    modified_structural_lines = []
    total = 0
    for built_path in BUILT.glob("*.txt"):
        source_path = SOURCE / built_path.name
        source = [(m.group(), m.start()) for m in ANCHOR.finditer(source_path.read_bytes())]
        built = [(m.group(), m.start()) for m in ANCHOR.finditer(built_path.read_bytes())]
        total += len(source)
        if source != built:
            mismatches.append(built_path.name)
        source_commands = [normalize_command(m.group()) for m in COMMAND.finditer(source_path.read_bytes())]
        built_commands = [normalize_command(m.group()) for m in COMMAND.finditer(built_path.read_bytes())]
        if source_commands != built_commands:
            command_mismatches.append(built_path.name)
        for number, row in enumerate(built_path.read_bytes().splitlines(), 1):
            if row.startswith((b"@@)", b"@(ss", b"@)ss")) and row not in (
                b"@@)", b"@(ss", b"@)ss"
            ):
                modified_structural_lines.append((built_path.name, number))

    target = (BUILT / "GADAT001_DAT_00006000.txt").read_bytes()
    dialogues = list(DIALOGUE.finditer(target))
    rows = dialogues[97].group(1).splitlines()
    trailing = [len(row) - len(row.rstrip(b" ")) for row in rows]
    indented = sum(
        len(re.findall(rb"(?m)^ +<\d+->IDS", path.read_bytes()))
        for path in BUILT.glob("*.txt")
    )
    print(f"anchors={total} offset_mismatch_files={len(mismatches)} indented={indented}")
    print(f"semantic_command_mismatch_files={len(command_mismatches)}")
    print(f"modified_structural_lines={len(modified_structural_lines)}")
    print(f"dialogue_0098_trailing_spaces={trailing}")
    if mismatches or indented or command_mismatches or modified_structural_lines:
        raise SystemExit(
            f"scenario audit failed: anchors={mismatches[:5]} "
            f"commands={command_mismatches[:5]} "
            f"structural={modified_structural_lines[:5]}"
        )


if __name__ == "__main__":
    main()
