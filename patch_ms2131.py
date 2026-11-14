#!/usr/bin/env python3
"""Patch the verified MS2131 SPI dump without modifying the source backup."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "ZMT02_MS2131_VER1.0_25082718_2541_SPI5E3213_ZB25WD40_stock.bin"
DEFAULT_OUTPUT = ROOT / "ZMT02_MS2131_VER1.0_25082718_2541_SPI5E3213_ZB25WD40_deblur_candidate.bin"

FLASH_SIZE = 0x80000
CODE_OFFSET = 0x30
CODE_LENGTH = 0xFBA0
HEADER_SUM_EXPECTED = 0x25F7
CODE_SUM_SOURCE = 0x9E01
CODE_SUM_PATCHED = 0x9D87
PATCH_OFFSET = 0xB3AA
PATCH_BEFORE = 0x7A
PATCH_AFTER = 0x00
STOCK_SHA256 = "01006bc4e7046193fe83825b0882fbcbd9c0393c2dd1d62e70f3038285f1c0e1"
PATCHED_SHA256 = "757e660faaa7f99518a357707b60b2472f129913447d62f5e27ffe0b8d65f629"


def checksum(data: bytes | bytearray) -> int:
    return sum(data) & 0xFFFF


def header_checksum(image: bytes | bytearray) -> int:
    return sum(image[i] for i in range(0x02, CODE_OFFSET) if not 0x0C <= i <= 0x0F) & 0xFFFF


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT,
                        help="verified 512 KiB MS2131 SPI backup")
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT,
                        help="output candidate path (default: *_deblur_candidate.bin)")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    args = parser.parse_args()

    src = args.input.resolve()
    dst = args.output.resolve()
    if src == dst:
        parser.error("refusing to overwrite the input backup; choose a separate output path")
    if dst.exists() and not args.force:
        parser.error(f"output already exists: {dst} (use --force to replace it)")

    try:
        image = bytearray(src.read_bytes())
    except OSError as exc:
        print(f"error: cannot read {src}: {exc}", file=sys.stderr)
        return 1

    if len(image) != FLASH_SIZE:
        print(f"error: input is {len(image)} bytes; expected a {FLASH_SIZE}-byte SPI dump", file=sys.stderr)
        return 1
    digest = hashlib.sha256(image).hexdigest()
    if digest != STOCK_SHA256:
        print("error: SHA-256 does not match the verified MS2131 stock backup; refusing to patch", file=sys.stderr)
        print(f"       observed: {digest}", file=sys.stderr)
        return 1
    if image[:2] != b"\x3c\xc3":
        print("error: unexpected firmware magic", file=sys.stderr)
        return 1
    code_length = int.from_bytes(image[0x02:0x04], "big")
    if code_length != CODE_LENGTH:
        print(f"error: code length 0x{code_length:04X}; expected 0x{CODE_LENGTH:04X}", file=sys.stderr)
        return 1

    checksum_offset = CODE_OFFSET + code_length
    stored_header = int.from_bytes(image[checksum_offset:checksum_offset + 2], "big")
    stored_code = int.from_bytes(image[checksum_offset + 2:checksum_offset + 4], "big")
    actual_header = header_checksum(image)
    actual_code = checksum(image[CODE_OFFSET:CODE_OFFSET + code_length])
    if (actual_header, stored_header) != (HEADER_SUM_EXPECTED, HEADER_SUM_EXPECTED):
        print("error: firmware header checksum mismatch", file=sys.stderr)
        return 1
    if (actual_code, stored_code) != (CODE_SUM_SOURCE, CODE_SUM_SOURCE):
        print("error: source code checksum mismatch", file=sys.stderr)
        return 1
    if image[PATCH_OFFSET] != PATCH_BEFORE:
        print(f"error: byte at 0x{PATCH_OFFSET:X} is 0x{image[PATCH_OFFSET]:02X}, expected 0x{PATCH_BEFORE:02X}", file=sys.stderr)
        return 1

    image[PATCH_OFFSET] = PATCH_AFTER
    patched_sum = checksum(image[CODE_OFFSET:CODE_OFFSET + code_length])
    if patched_sum != CODE_SUM_PATCHED:
        print(f"error: patched code checksum is 0x{patched_sum:04X}, expected 0x{CODE_SUM_PATCHED:04X}", file=sys.stderr)
        return 1
    image[checksum_offset + 2:checksum_offset + 4] = patched_sum.to_bytes(2, "big")
    result_digest = hashlib.sha256(image).hexdigest()
    if result_digest != PATCHED_SHA256:
        print("error: generated image hash differs from the reviewed candidate", file=sys.stderr)
        print(f"       observed: {result_digest}", file=sys.stderr)
        return 1

    try:
        dst.write_bytes(image)
    except OSError as exc:
        print(f"error: cannot write {dst}: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote: {dst}")
    print(f"Patch: file offset 0x{PATCH_OFFSET:X}, 0x{PATCH_BEFORE:02X} -> 0x{PATCH_AFTER:02X}")
    print(f"Code checksum: 0x{CODE_SUM_SOURCE:04X} -> 0x{patched_sum:04X}")
    print(f"SHA-256: {result_digest}")
    print("The input backup was not modified. No device was flashed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
