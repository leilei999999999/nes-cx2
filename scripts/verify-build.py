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
    # Construct regex escapes with chr() to keep this script's quoting unambiguous.
    bs = chr(92)
    pattern = (
        bs + "b" + return_type + bs + "s+" + re.escape(name) +
        bs + "s*" + bs + "(" + "[^)]*" + bs + ")" + bs + "s*" + bs + "{"
    )
    match = re.search(pattern, source)
    if not match:
        return ""

    opening = match.end() - 1
    depth = 0
    state = "code"
    quote = 0
    i = opening
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if state == "line_comment":
            if ord(ch) == 10:
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 1
        elif state == "string":
            if ord(ch) == 92:
                i += 1
            elif ord(ch) == quote:
                state = "code"
        elif state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                i += 1
            elif ch in ('"', "'"):
                quote = ord(ch)
                state = "string"
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return source[opening + 1:i]
        i += 1
    return ""



def simulate_audio_test_rom(data: bytes) -> tuple[bool, str]:
    """Execute the test ROM's small 6502 program and inspect PPU/APU writes."""
    if len(data) != 16 + 16384 + 8192:
        return False, "wrong iNES file size"
    if data[:4] != bytes((0x4E, 0x45, 0x53, 0x1A)):
        return False, "invalid iNES magic"
    if data[4:8] != bytes((1, 1, 0, 0)):
        return False, "test ROM is not mapper-0 NROM-128 with 8 KiB CHR"

    prg = data[16:16 + 16384]
    memory = bytearray(65536)
    memory[0x8000:0xC000] = prg
    pc = prg[0x3FFC] | (prg[0x3FFD] << 8)
    a = x = y = 0
    zero = False
    ppu_addr = 0
    ppu_second_write = False
    ppu_ctrl = ppu_mask = 0
    ppu_data: dict[int, int] = {}
    apu: dict[int, int] = {}
    reached_idle = False

    for instruction_count in range(10000):
        instruction_address = pc
        opcode = memory[pc]
        pc = (pc + 1) & 0xFFFF

        if opcode in (0x78, 0xD8):  # SEI, CLD
            pass
        elif opcode == 0x9A:  # TXS
            pass
        elif opcode == 0xA9:  # LDA immediate
            a = memory[pc]
            pc = (pc + 1) & 0xFFFF
            zero = (a == 0)
        elif opcode == 0xA2:  # LDX immediate
            x = memory[pc]
            pc = (pc + 1) & 0xFFFF
            zero = (x == 0)
        elif opcode == 0xA0:  # LDY immediate
            y = memory[pc]
            pc = (pc + 1) & 0xFFFF
            zero = (y == 0)
        elif opcode == 0x8D:  # STA absolute
            address = memory[pc] | (memory[(pc + 1) & 0xFFFF] << 8)
            pc = (pc + 2) & 0xFFFF
            memory[address] = a
            if address == 0x2000:
                ppu_ctrl = a
            elif address == 0x2001:
                ppu_mask = a
            elif address == 0x2006:
                if not ppu_second_write:
                    ppu_addr = ((a << 8) | (ppu_addr & 0x00FF)) & 0x3FFF
                    ppu_second_write = True
                else:
                    ppu_addr = ((ppu_addr & 0x3F00) | a) & 0x3FFF
                    ppu_second_write = False
            elif address == 0x2007:
                ppu_data[ppu_addr] = a
                ppu_addr = (ppu_addr + (32 if (ppu_ctrl & 0x04) else 1)) & 0x3FFF
            elif 0x4000 <= address <= 0x4017:
                apu[address] = a
        elif opcode == 0xC8:  # INY
            y = (y + 1) & 0xFF
            zero = (y == 0)
        elif opcode == 0xCA:  # DEX
            x = (x - 1) & 0xFF
            zero = (x == 0)
        elif opcode == 0xC0:  # CPY immediate
            value = memory[pc]
            pc = (pc + 1) & 0xFFFF
            zero = (y == value)
        elif opcode == 0xD0:  # BNE relative
            offset = memory[pc]
            pc = (pc + 1) & 0xFFFF
            if not zero:
                if offset & 0x80:
                    offset -= 0x100
                pc = (pc + offset) & 0xFFFF
        elif opcode == 0x4C:  # JMP absolute
            target = memory[pc] | (memory[(pc + 1) & 0xFFFF] << 8)
            if target == instruction_address:
                reached_idle = True
                break
            pc = target
        else:
            return False, f"unsupported 6502 opcode 0x{opcode:02X} at 0x{instruction_address:04X}"

        if not 0x8000 <= pc < 0xC000:
            return False, f"program counter escaped NROM PRG at 0x{pc:04X}"
    else:
        return False, "program did not reach its stable self-loop within 10,000 instructions"

    nametable_ok = all(ppu_data.get(0x2000 + i) == 1 for i in range(960))
    attributes_ok = all(ppu_data.get(0x23C0 + i) == 0 for i in range(64))
    palette_ok = ppu_data.get(0x3F00) == 0x0F and ppu_data.get(0x3F01) == 0x16
    apu_expected = {
        0x4010: 0x00, 0x4017: 0x40, 0x4001: 0x00,
        0x4000: 0xBF, 0x4002: 0xFD, 0x4003: 0x00, 0x4015: 0x01,
    }
    apu_ok = all(apu.get(address) == value for address, value in apu_expected.items())
    chr_ok = data[16 + 16384 + 16:16 + 16384 + 24] == bytes((0xFF,) * 8)
    screen_ok = ppu_ctrl == 0 and ppu_mask == 0x0A and nametable_ok and attributes_ok and palette_ok
    ok = reached_idle and screen_ok and apu_ok and chr_ok
    detail = (
        f"instructions={instruction_count + 1}, idle_loop={reached_idle}, "
        f"screen={screen_ok}, APU={apu_ok}, CHR={chr_ok}"
    )
    return ok, detail


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
    parser.add_argument("--sdk-root", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()

    root = args.source_dir
    passed: list[str] = []
    failed: list[str] = []
    warnings: list[str] = []

    system_path = root / "src/ndless/InfoNES_System_Ndless.cpp"
    bridge_path = root / "src/ndless/NesNspire_Audio.cpp"
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

    readable = [p.is_file() for p in (system_path, bridge_path, usb_header, makefile_path)]
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
            ("USB header has C linkage guard", 'extern "C" {' in c_header and c_header.count('extern "C" {') == 1
             and "}" + chr(10) + "#endif" in c_header),
            ("hardware timer is used instead of process clock", "NES_TIMER_VALUE" in bridge
             and "NES_TIMER_TICKS_PER_SECOND 32768U" in bridge and not re.search(r"(?<![A-Za-z0-9_])clock\s*\(", bridge)),
            ("timer state is restored on audio close", "nes_timer_restore();" in function_body(bridge, "nes_audio_close", "void")),
            ("USB device is opened and streaming starts", "usb_audio_open(nes_audio_now_ms" in bridge and "usb_audio_start(g_usb" in bridge),
            ("USB service failures are surfaced", "usb_audio_service(g_usb)" in bridge and "g_stream.failed = true;" in bridge),
            ("USB diagnostics include device descriptor and stream state",
             "usb_audio_describe(g_usb" in bridge and "usb_audio_capture_stream(g_usb)" in bridge
             and "usb_audio_last_stream()" in bridge and "usb_audio_last_control()" in bridge
             and "usb_audio_transfer_fault()" in bridge),
            ("emulator logs detailed USB diagnostics on stream failure",
             "nes_audio_diagnostics(details, sizeof(details));" in function_body(system, "InfoNES_Wait", "void")
             and "USB audio diagnostics:" in function_body(system, "InfoNES_Wait", "void")),
            ("emulator logs detailed USB diagnostics when device open fails",
             "nes_audio_diagnostics(details, sizeof(details));" in function_body(system, "InfoNES_SoundOpen", "int")
             and "USB audio open diagnostics:" in function_body(system, "InfoNES_SoundOpen", "int")),
            ("audio ring buffer is power-of-two sized", "#define NES_AUDIO_CAPACITY 16384U" in bridge),
            ("audio ring buffer has safe prefill", "#define NES_AUDIO_PREFILL 9000U" in bridge),
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

        for rel in ("src/ndless/NesNspire_Audio.o", "src/audio/usb_audio.o",
                    "src/audio/usb_audio_formats.o", "src/audio/audio_buffer.o"):
            p = root / rel
            check(p.is_file() and p.stat().st_size > 0,
                  f"compiled object exists: {rel}", passed, failed)

    if args.tns:
        tns = args.tns
        check(tns.is_file() and 65536 <= tns.stat().st_size <= 16 * 1024 * 1024,
              "TNS package exists and has plausible size", passed, failed)
        if tns.is_file() and args.sdk_root:
            loader = args.sdk_root / "tools/zehn_loader/zehn_loader.tns"
            loader_ok = loader.is_file() and loader.stat().st_size > 0
            if loader_ok:
                loader_bytes = loader.read_bytes()
                with tns.open("rb") as stream:
                    loader_ok = stream.read(len(loader_bytes)) == loader_bytes
            check(loader_ok, "TNS begins with the matching Ndless Zehn loader", passed, failed)
        else:
            check(False, "Ndless SDK root supplied for TNS loader check", passed, failed)

    if args.test_rom:
        rom = args.test_rom
        valid = False
        if rom.is_file():
            data = rom.read_bytes()
            valid, simulation_detail = simulate_audio_test_rom(data)
        else:
            simulation_detail = "ROM file is missing"
        check(valid, "6502 test ROM executes to idle and produces the expected PPU/APU writes: "
              + simulation_detail, passed, failed)

    # The current bridge still does not port nMedia's complete USB host-session
    # lease and native route/state callbacks. Compilation cannot validate that
    # hardware ownership path, so make the known limitation visible in the report.
    integration_requirements = (
        "native_usb_ui_clock(",
        "native_usb_ui_acquire(",
        "usb_host_session_prepare(",
        "usb_audio_set_route_lookup(",
        "usb_audio_set_state_lookup(",
        "native_usb_ui_release(",
        "monotonic_clock_now_ticks(",
    )
    if bridge_path.is_file():
        bridge_text = bridge_path.read_text(errors="replace")
        missing = [name for name in integration_requirements if name not in bridge_text]
        check(not missing, "native USB host wake/lease and route callbacks are integrated"
              + (": missing " + ", ".join(missing) if missing else ""), passed, failed)
    native_sources = (
        "src/player/native_usb_ui.c",
        "src/player/native_usb_ui_resolver.c",
        "src/player/native_interrupts.c",
        "src/player/storage_mutation.c",
        "src/platform/nspire_hardware.c",
        "src/platform/portable_reader_platform.c",
        "src/platform/portable_resolver.c",
        "src/storage/usb/usb_host_session.c",
    )
    for rel in native_sources:
        p = root / rel
        check(p.is_file() and p.stat().st_size > 0,
              f"native USB source present: {rel}", passed, failed)
    if makefile_path.is_file():
        makefile = makefile_path.read_text(errors="replace")
        for rel in native_sources:
            obj = rel.rsplit(".", 1)[0] + ".o"
            check(obj in makefile, f"Makefile links native USB object {obj}", passed, failed)
    warnings.append(
        "Physical playback has not been tested by CI; use USB-Audio-Tone-Test.nes.tns "
        "with a class-compliant USB audio DAC and confirm an audible steady tone."
    )

    report = {"passed": passed, "failed": failed, "warnings": warnings, "ok": not failed}
    print()
    print(f"USB-audio verification: {len(passed)} passed, {len(failed)} failed, {len(warnings)} warnings")
    for item in passed:
        print(f"PASS  {item}")
    for item in failed:
        print(f"FAIL  {item}")
    for item in warnings:
        print(f"WARN  {item}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + chr(10))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
