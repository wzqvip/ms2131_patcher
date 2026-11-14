#!/usr/bin/env python3
"""Identify MS2130/MS2131 firmware images by byte signatures and container metadata.

This examines a file only. It does not connect to a USB device or flash firmware.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
CODE_OFFSET = 0x30
MAGIC = b"\x3c\xc3"

# Patterns are file-offset byte signatures. "??" is a wildcard byte. Each
# profile also validates the firmware's embedded code length and checksums.
KNOWN_SIGNATURES = [
    {
        "family": "MS2130",
        "sample": "MS2130 4K2 firmware (unpatched)",
        "code_length": 0xD1FA,
        "header_checksum": 0x2627,
        "code_checksum": 0x09DB,
        "patterns": [(0x00, "3C C3 D1 FA FF FF FF FF 80 00 00 00 ?? ?? ?? ??"),
                     (0x9634, "00 06"), (0x963D, "00 06"), (0x93AE, "12 89 C9")],
    },
    {
        "family": "MS2130",
        "sample": "MS2130 patcher output (patched scaler/sharpening settings)",
        "code_length": 0xD1FA,
        "header_checksum": 0x2627,
        "code_checksum": 0x0949,
        "patterns": [(0x00, "3C C3 D1 FA FF FF FF FF 80 00 00 00 ?? ?? ?? ??"),
                     (0x9634, "BF 44"), (0x963D, "BF 44"), (0x93AE, "00 7E 10")],
    },
    {
        "family": "MS2131",
        "sample": "MS2131 086-1/2403 stock code (also present in goodZB25WD40@SOIC8.BIN)",
        "code_length": 0xFBA0,
        "header_checksum": 0x25F7,
        "code_checksum": 0x70B6,
        "patterns": [(0x00, "3C C3 FB A0 FF FF FF FF 80 00 00 00 ?? ?? ?? ??"),
                     (0xB3AA, "29")],
    },
    {
        "family": "MS2131",
        "sample": "Sipeed NanoKVM-USB 30125D build (2024-12-25; filename-labeled MS2131)",
        "code_length": 0xF58E,
        "header_checksum": 0x261F,
        "code_checksum": 0xB565,
        "patterns": [
            (0x00, "3C C3 F5 8E FF FF FF FF C0 00 00 00 ?? ?? ?? ??"),
            (0x4E, "44 65 63 20 32 35 20 32 30 32 34"),  # "Dec 25 2024"
            (0xB9, "48 44 20 54 4F 20 55 53 42"),        # "HD TO USB"
            (0xC3B6, "90 F6 B4 74 7A F0"),               # direct write F6B4 <- 7A
        ],
    },
    {
        "family": "MS2131",
        "sample": "ZMT02 stock firmware",
        "code_length": 0xFBA0,
        "header_checksum": 0x25F7,
        "code_checksum": 0x9E01,
        "patterns": [(0x00, "3C C3 FB A0 FF FF FF FF 80 00 00 00 19 08 1B 12"),
                     (0xB3AA, "7A")],
    },
    {
        "family": "MS2131",
        "sample": "ZMT02 de-sharpening candidate",
        "code_length": 0xFBA0,
        "header_checksum": 0x25F7,
        "code_checksum": 0x9D87,
        "patterns": [(0x00, "3C C3 FB A0 FF FF FF FF 80 00 00 00 19 08 1B 12"),
                     (0xB3AA, "00")],
    },
    {
        "family": "MS2131-layout",
        "sample": "ZB25WD40@SOIC8 image labeled crap (silicon identity/quality unverified)",
        "code_length": 0xFBA0,
        "header_checksum": 0x25F7,
        "code_checksum": 0x62D6,
        "patterns": [(0x00, "3C C3 FB A0 FF FF FF FF 80 00 00 00 ?? ?? ?? ??"),
                     (0xB3AA, "10")],
    },
]

# Behavior observed and documented by hsdaoh for the MS2130 transparent mode.
# These are register writes, not a complete silicon register specification.
HSDAOH_REGISTER_SETTINGS = [
    {"register": 0xF039, "value": 0x00, "function": "force YCbCr 4:2:2/YUV input"},
    {"register": 0xF030, "value": 0x02, "function": "YUV input mode companion setting"},
    {"register": 0xF6B0, "value": 0x00, "function": "disable sharpening"},
    {"register": 0xF6BE, "value": 0x11, "function": "disable luma processing"},
    {"register": 0xF6BF, "value": 0x11, "function": "disable chroma processing"},
    {"register": 0xF65C, "value": 0x10, "function": "disable horizontal rescaling"},
    {"register": 0xF65E, "value": 0x10, "function": "disable vertical rescaling"},
    {"register": 0xF600, "value": 0x80, "function": "disable chroma interpolation"},
]
TRACKED_DIRECT_WRITE_REGISTERS = {entry["register"] for entry in HSDAOH_REGISTER_SETTINGS} | {0xF6B4}


def pattern_matches(data: bytes, offset: int, pattern: str) -> bool:
    tokens = pattern.split()
    if offset < 0 or offset + len(tokens) > len(data):
        return False
    return all(token == "??" or data[offset + i] == int(token, 16)
               for i, token in enumerate(tokens))


def parse_firmware(data: bytes) -> dict:
    result = {
        "magic": data[:2].hex(),
        "recognized_container": data[:2] == MAGIC,
        "file_size_bytes": len(data),
        "code_offset": CODE_OFFSET,
        "code_length_bytes": None,
        "embedded_image_end_offset": None,
        "header_checksum_stored": None,
        "header_checksum_calculated": None,
        "code_checksum_stored": None,
        "code_checksum_calculated": None,
        "checksums_valid": False,
        "trailing_bytes_after_embedded_image": None,
        "trailing_region_fill_byte": None,
        "patch_byte_at_0xB3AA": None,
    }
    if not result["recognized_container"] or len(data) < CODE_OFFSET:
        return result

    code_length = int.from_bytes(data[2:4], "big")
    end = CODE_OFFSET + code_length
    checksum_end = end + 4
    result["code_length_bytes"] = code_length
    result["embedded_image_end_offset"] = checksum_end
    result["trailing_bytes_after_embedded_image"] = max(0, len(data) - checksum_end)

    if len(data) >= checksum_end:
        header_calc = sum(data[i] for i in range(2, CODE_OFFSET) if not 0x0C <= i <= 0x0F) & 0xFFFF
        code_calc = sum(data[CODE_OFFSET:end]) & 0xFFFF
        result["header_checksum_stored"] = int.from_bytes(data[end:end + 2], "big")
        result["header_checksum_calculated"] = header_calc
        result["code_checksum_stored"] = int.from_bytes(data[end + 2:end + 4], "big")
        result["code_checksum_calculated"] = code_calc
        result["checksums_valid"] = (
            result["header_checksum_stored"] == header_calc
            and result["code_checksum_stored"] == code_calc
        )
        if result["trailing_bytes_after_embedded_image"]:
            tail = data[checksum_end:]
            if tail and all(byte == tail[0] for byte in tail):
                result["trailing_region_fill_byte"] = tail[0]

    if len(data) > 0xB3AA:
        result["patch_byte_at_0xB3AA"] = data[0xB3AA]
    return result


def scan_8051_direct_writes(data: bytes, code_length: int | None) -> list[dict]:
    """Find MOV DPTR,#addr; MOV A,#value; MOVX @DPTR,A in the code region."""
    if code_length is None or len(data) < CODE_OFFSET + code_length:
        return []
    code = data[CODE_OFFSET:CODE_OFFSET + code_length]
    writes = []
    i = 0
    while i + 5 < len(code):
        # 8051 encodings: 90 hi lo (MOV DPTR,#imm16), 74 xx (MOV A,#imm), F0 (MOVX @DPTR,A)
        if code[i] == 0x90 and code[i + 3] == 0x74 and code[i + 5] == 0xF0:
            address = (code[i + 1] << 8) | code[i + 2]
            if address in TRACKED_DIRECT_WRITE_REGISTERS:
                writes.append({
                    "file_offset": CODE_OFFSET + i,
                    "register": address,
                    "value": code[i + 4],
                    "instruction_bytes": code[i:i + 6].hex(" ").upper(),
                    "decoder": "8051 MOV DPTR / MOV A immediate / MOVX store pattern",
                    "limitation": "byte-pattern decode only; full instruction boundaries/control flow are not recovered",
                })
        i += 1
    return writes


def interpret_features(matched: dict | None, register_writes: list[dict]) -> list[dict]:
    findings = []
    for write in register_writes:
        if write["register"] == 0xF6B4:
            value = write["value"]
            if value == 0x00:
                setting = "zeroed experimental value; associated with the de-sharpening candidate"
            elif value == 0x7A:
                setting = "stock tuning value seen in the repository's MS2130/MS2131 samples"
            else:
                setting = "other/unclassified tuning value"
            findings.append({
                "feature": "F6B4 image-processing parameter (likely sharpening-related)",
                "register": "0xF6B4",
                "value": f"0x{value:02X}",
                "setting": setting,
                "confidence": "inferred; register meaning is not defined in hsdaoh",
                "evidence": [
                    f"8051 direct-write instruction at file offset 0x{write['file_offset']:X}: {write['instruction_bytes']}",
                    "The MS2131 stock-to-candidate edit changes only this immediate value from 0x7A to 0x00 (plus checksum bytes).",
                    "The controlled chart comparison measured a wider edge response on the candidate; this links the setting to sharpness behavior but does not establish the register's full semantics.",
                ],
            })

    if matched and matched["sample"].startswith("MS2130 patcher output"):
        findings.append({
            "feature": "MS2130 transparent/less-processed video patch settings",
            "confidence": "documented patch intent; inferred runtime effect",
            "settings": [
                {"register": "0xF6B0", "operation": "clear mask 0x01", "bits": [0], "effect": "disable sharpening"},
                {"register": "0xF6BE", "operation": "set mask 0x11", "bits": [0, 4], "effect": "disable luma processing"},
                {"register": "0xF6BF", "operation": "set mask 0x11", "bits": [0, 4], "effect": "disable chroma processing"},
                {"register": "0xF65C", "operation": "write 0x10", "effect": "disable horizontal rescaling"},
                {"register": "0xF65E", "operation": "write 0x10", "effect": "disable vertical rescaling"},
                {"register": "0xF600", "operation": "set mask 0x80", "bits": [7], "effect": "disable chroma interpolation"},
            ],
            "evidence": [
                "The matched byte signature is the output of the local MS2130 patcher.",
                "The patcher's source comments describe the corresponding code changes and register operations.",
                "hsdaoh documents these values as transparent-mode settings and notes they were found experimentally.",
            ],
        })
    return findings


def identify(path: Path) -> dict:
    data = path.read_bytes()
    metadata = parse_firmware(data)
    register_writes = scan_8051_direct_writes(data, metadata["code_length_bytes"])
    matched = None
    if metadata["recognized_container"] and metadata["checksums_valid"]:
        for profile in KNOWN_SIGNATURES:
            if (metadata["code_length_bytes"] == profile["code_length"]
                    and metadata["header_checksum_stored"] == profile["header_checksum"]
                    and metadata["code_checksum_stored"] == profile["code_checksum"]
                    and all(pattern_matches(data, offset, pattern)
                            for offset, pattern in profile["patterns"])):
                matched = profile
                break

    if matched:
        family = matched["family"]
        identification = f"{family}: known firmware byte-signature match"
        confidence = "known_byte_signature"
        matched_sample = matched["sample"]
        matched_catalog_file = None
        basis = [
            "valid embedded firmware header and additive checksums",
            f"code length 0x{matched['code_length']:04X}, header checksum 0x{matched['header_checksum']:04X}, code checksum 0x{matched['code_checksum']:04X}",
            "required byte patterns matched at listed file offsets",
        ]
    else:
        family = None
        identification = "unknown / not enough evidence"
        confidence = "unknown"
        matched_sample = None
        matched_catalog_file = None
        basis = []
        if metadata["recognized_container"] and metadata["checksums_valid"]:
            code_len = metadata["code_length_bytes"]
            header_sum = metadata["header_checksum_stored"]
            if code_len == 0xFBA0 and header_sum == 0x25F7:
                family = "MS2131"
                identification = "likely MS2131-family firmware layout (not an exact known build)"
                confidence = "heuristic_layout"
                basis = [
                    "valid 0x3CC3 image header and checksums",
                    "code length 0xFBA0 and header checksum 0x25F7 match the repository's MS2131 builds",
                ]
            elif code_len == 0xD1FA and header_sum == 0x2627:
                family = "MS2130"
                identification = "likely MS2130 4K2-family firmware layout (not an exact known build)"
                confidence = "heuristic_layout"
                basis = [
                    "valid 0x3CC3 image header and checksums",
                    "code length 0xD1FA and header checksum 0x2627 match the repository's MS2130 4K2 image",
                ]

    return {
        "path": str(path.resolve()),
        "identification": identification,
        "family": family,
        "confidence": confidence,
        "matched_sample": matched_sample,
        "matched_catalog_file": matched_catalog_file,
        "matched_byte_patterns": ([
            {"file_offset": offset, "pattern": pattern}
            for offset, pattern in matched["patterns"]
        ] if matched else []),
        "direct_8051_register_writes": register_writes,
        "feature_findings": interpret_features(matched, register_writes),
        "hsdaoh_transparent_mode_register_reference": HSDAOH_REGISTER_SETTINGS,
        "basis": basis,
        "firmware_metadata": metadata,
        "scope_note": (
            "This identifies a firmware file/sample. Firmware contents alone do not prove the physical chip model "
            "of a connected board; USB identity, board markings, or direct hardware identification are separate evidence."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path, help="firmware image or SPI dump to inspect")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    try:
        result = identify(args.firmware)
    except OSError as exc:
        print(f"error: cannot read {args.firmware}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result["identification"])
        print(f"Confidence: {result['confidence']}")
        if result["matched_sample"]:
            print(f"Sample: {result['matched_sample']}")
        fw = result["firmware_metadata"]
        if fw["recognized_container"]:
            print(f"Code length: {fw['code_length_bytes']:#x} ({fw['code_length_bytes']} bytes)")
            print(f"Checksums valid: {fw['checksums_valid']}")
            if fw["code_checksum_stored"] is not None:
                print(f"Code checksum: stored {fw['code_checksum_stored']:#06x}, calculated {fw['code_checksum_calculated']:#06x}")
            if fw["patch_byte_at_0xB3AA"] is not None:
                print(f"Byte at file offset 0xB3AA: {fw['patch_byte_at_0xB3AA']:#04x}")
        for write in result["direct_8051_register_writes"]:
            print(f"8051 direct write: register 0x{write['register']:04X} <- 0x{write['value']:02X} at file offset 0x{write['file_offset']:X}")
        for feature in result["feature_findings"]:
            print(f"Feature: {feature['feature']} [{feature['confidence']}]")
            if "setting" in feature:
                print(f"  {feature['setting']}")
            if "settings" in feature:
                for setting in feature["settings"]:
                    bits = f" bits={setting['bits']}" if "bits" in setting else ""
                    print(f"  {setting['register']}: {setting['operation']}{bits} -> {setting['effect']}")
        if not fw["recognized_container"]:
            print("No recognized 0x3CC3 firmware header.")
        print(f"Note: {result['scope_note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
