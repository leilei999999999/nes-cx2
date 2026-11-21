#!/usr/bin/env python3
"""Regression checks for the generated NESNspire USB-audio build."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys


def check(ok: bool, message: str, passed: list[str], failed: list[str]) -> None:
    (passed if ok else failed).append(message)


def function_body(source: str, name: str, return_type: str) -> str:
    pattern = rf"\b{return_type}\s+{re.escape(name)}\s*\([^)]*\)\s*\{{(.*?)\}}"
    match = re.search(pattern, source, re.S)
    return match.group(1) if match else ""


def run_text(command: list[str]) -> str:
    result = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=pathlib.Path)
    parser.add_argument("--elf", type=pathlib.Path)
    parser.add_argument("--tns", type=pathlib.Path)
    parser.add_argument("--test-rom", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()

    root = args.source_dir
    passed: list[str] = []
    failed: list[str] = []

    system_path = root / "src/ndless/InfoNES_System_Ndless.cpp"
    bridge_path = root / "src/ndless/NesNspire_Audio.cpp"
    bridge_header = root / "src/ndless/NesNspire_Audio.h"
    usb_header = root / "src/audio/usb_audio.h"
    makefile_path = root / "Makefile"

    required_sources = [
        "src/audio/usb_audio.c",
        "src/audio/usb_audio_formats.c",
        "src/audio/audio_buffer.c",
        "src/audio/usb_audio_schedule.inc",
        "src/audio/usb_audio_topology.inc",
        "src/audio/usb_audio_controls.inc",
        "src/audio/usb_audio_health.inc",
        "src/audio/usb_audio_split.inc",
    ]
    for rel in required_sources:
        p = root / rel
        check(p.is_file() and p.stat().st_size > 0,
              f"source present: {rel}", passed, failed)

    readable = [p.is_file() for p in (system_path, bridge_path, bridge_header, usb_header, makefile_path)]
    check(all(readable), "all generated integration files exist", passed, failed)

    if all(readable):
        system = system_path.read_text(errors="replace")
        bridge = bridge_path.read_text(errors="replace")
        c_header = usb_header.read_text(errors="replace")
        makefile = makefile_path.read_text(errors="replace")

        checks = [
            ("NES audio header is included", '#include "NesNspire_Audio.h"' in system),
            ("APU is not globally muted", "APU_Mute = 0;" in system),
            ("SoundInit initializes bridge", "nes_audio_init();" in function_body(system, "InfoNES_SoundInit", "void")),
            ("SoundOpen opens bridge and returns status", "nes_audio_open(s, r)" in function_body(system, "InfoNES_SoundOpen", "int")
             and "return ok;" in function_body(system, "InfoNES_SoundOpen", "int")),
            ("SoundOutput forwards all five APU channels", "nes_audio_output(s, w1, w2, w3, w4, w5);" in function_body(system, "InfoNES_SoundOutput", "void")),
            ("SoundClose releases audio bridge", "nes_audio_close();" in function_body(system, "InfoNES_SoundClose", "void")),
            ("frame wait services USB audio", "nes_audio_service();" in function_body(system, "InfoNES_Wait", "void")),
            ("USB header has C linkage guard", 'extern "C" {' in c_header and c_header.count('extern "C" {') == 1 and "}\n#endif" in c_header),
            ("hardware timer is used instead of process clock", "NES_TIMER_VALUE" in bridge and "NES_TIMER_TICKS_PER_SECOND 32768U" in bridge and not re.search(r"\bclock\s*\(", bridge)),
            ("timer state is restored on audio close", "nes_timer_restore();" in function_body(bridge, "nes_audio_close", "void")),
            ("USB device is opened and streaming starts", "usb_audio_open(nes_audio_now_ms" in bridge and "usb_audio_start(g_usb" in bridge),
            ("USB service failures are surfaced", "usb_audio_service(g_usb)" in bridge and "g_stream.failed = true;" in bridge),
            ("audio ring buffer is power-of-two sized", bool(re.search(r"#define\s+NES_AUDIO_CAPACITY\s+16384U", bridge))),
            ("audio ring buffer has safe prefill", bool(re.search(r"#define\s+NES_AUDIO_PREFILL\s+9000U", bridge))),
        ]
        for message, ok in checks:
            check(ok, message, passed, failed)

        for rel in ("src/ndless/NesNspire_Audio.o", "src/audio/usb_audio.o",
                    "src/audio/usb_audio_formats.o", "src/audio/audio_buffer.o"):
            check(rel in makefile, f"Makefile links {rel}", passed, failed)

    if args.elf:
        elf = args.elf
        check(elf.is_file() and elf.stat().st_size > 0, "linked ELF exists", passed, failed)
        if elf.is_file() and shutil.which("arm-none-eabi-readelf"):
            header = run_text(["arm-none-eabi-readelf", "-h", str(elf)])
            check("ELF32" in header and "little endian" in header.lower() and "ARM" in header,
                  "ELF is 32-bit little-endian ARM", passed, failed)
        else:
            check(False, "ARM readelf available for ELF check", passed, failed)
        if elf.is_file() and shutil.which("arm-none-eabi-nm"):
            symbols = run_text(["arm-none-eabi-nm", "-C", str(elf)])
            for symbol in ("nes_audio_init", "nes_audio_open", "nes_audio_output",
                           "nes_audio_service", "nes_audio_close", "usb_audio_open",
                           "usb_audio_start", "usb_audio_service"):
                check(symbol in symbols, f"linked ELF contains {symbol}", passed, failed)
        else:
            check(False, "ARM nm available for symbol checks", passed, failed)

        object_files = ("src/ndless/NesNspire_Audio.o", "src/audio/usb_audio.o",
                        "src/audio/usb_audio_formats.o", "src/audio/audio_buffer.o")
        for rel in object_files:
            p = root / rel
            check(p.is_file() and p.stat().st_size > 0,
                  f"compiled object exists: {rel}", passed, failed)

    if args.tns:
        tns = args.tns
        check(tns.is_file() and 65536 <= tns.stat().st_size <= 16 * 1024 * 1024,
              "TNS package exists and has plausible size", passed, failed)
        if tns.is_file() and shutil.which("nspire-tools"):
            loader_result = subprocess.run(
                ["nspire-tools", "_zehn_loader_path", ""], check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            loader = pathlib.Path(loader_result.stdout.strip())
            loader_ok = loader.is_file() and loader.stat().st_size > 0
            if loader_ok:
                loader_bytes = loader.read_bytes()
                with tns.open("rb") as stream:
                    loader_ok = stream.read(len(loader_bytes)) == loader_bytes
            check(loader_ok, "TNS begins with the matching Ndless Zehn loader", passed, failed)
        else:
            check(False, "nspire-tools available for TNS loader check", passed, failed)

    if args.test_rom:
        rom = args.test_rom
        valid = False
        if rom.is_file():
            data = rom.read_bytes()
            valid = (len(data) == 16 + 16384 + 8192
                     and data[:4] == b"NES\x1a"
                     and data[4] == 1 and data[5] == 1
                     and (data[6] & 0xF0) == 0
                     and data[16 + 0x3FFC:16 + 0x3FFE] == b"\x00\x80")
            prg = data[16:16 + 16384] if valid else b""
            valid = valid and all(op in prg for op in (
                b"\x8d\x00\x40",  # $4000 pulse control
                b"\x8d\x02\x40",  # $4002 timer low
                b"\x8d\x03\x40",  # $4003 timer high/length
                b"\x8d\x15\x40",  # $4015 channel enable
            ))
        check(valid, "generated audio test ROM is valid NROM and programs pulse channel 1",
              passed, failed)

    report = {"passed": passed, "failed": failed, "ok": not failed}
    print(f"\nUSB-audio verification: {len(passed)} passed, {len(failed)} failed")
    for item in passed:
        print(f"PASS  {item}")
    for item in failed:
        print(f"FAIL  {item}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
