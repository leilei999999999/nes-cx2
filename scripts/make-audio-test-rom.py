#!/usr/bin/env python3
"""Create a visual NROM test ROM that displays a color field and plays ~440 Hz."""
from __future__ import annotations

import argparse
import pathlib


def build_rom() -> bytes:
    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def emit(*values: int) -> None:
        code.extend(values)

    def label(name: str) -> None:
        labels[name] = len(code)

    def branch(opcode: int, target: str) -> None:
        operand = len(code) + 1
        emit(opcode, 0)
        fixups.append((operand, target))

    # CPU reset and PPU setup.
    emit(0x78)  # SEI
    emit(0xD8)  # CLD
    emit(0xA2, 0xFF)  # LDX #$FF
    emit(0x9A)  # TXS
    emit(0xA9, 0x00, 0x8D, 0x00, 0x20)  # PPUCTRL = 0
    emit(0xA9, 0x00, 0x8D, 0x01, 0x20)  # PPUMASK = 0 during setup

    # Palette 0: black universal backdrop and bright orange/red tile color.
    emit(0xA9, 0x3F, 0x8D, 0x06, 0x20)  # PPUADDR high = $3F
    emit(0xA9, 0x00, 0x8D, 0x06, 0x20)  # PPUADDR low = $00
    emit(0xA9, 0x0F, 0x8D, 0x07, 0x20)  # universal backdrop
    emit(0xA9, 0x16, 0x8D, 0x07, 0x20)  # palette color 1

    # Fill all 960 nametable tile entries with tile 1.
    emit(0xA9, 0x20, 0x8D, 0x06, 0x20)  # PPUADDR = $2000
    emit(0xA9, 0x00, 0x8D, 0x06, 0x20)
    emit(0xA2, 0x03)  # LDX #3 -> 3 * 256 tile entries
    emit(0xA0, 0x00)  # LDY #0
    label("nametable_256")
    emit(0xA9, 0x01, 0x8D, 0x07, 0x20)  # LDA #1; STA $2007
    emit(0xC8)  # INY
    branch(0xD0, "nametable_256")  # BNE until Y wraps
    emit(0xCA)  # DEX
    branch(0xD0, "nametable_256")  # repeat three 256-byte blocks
    emit(0xA0, 0x00)  # LDY #0 for the last 192 tiles
    label("nametable_192")
    emit(0xA9, 0x01, 0x8D, 0x07, 0x20)
    emit(0xC8)  # INY
    emit(0xC0, 0xC0)  # CPY #$C0
    branch(0xD0, "nametable_192")

    # Explicitly clear all 64 attribute bytes so the background uses palette 0.
    emit(0xA9, 0x23, 0x8D, 0x06, 0x20)  # PPUADDR high = $23
    emit(0xA9, 0xC0, 0x8D, 0x06, 0x20)  # PPUADDR low = $C0
    emit(0xA9, 0x00, 0x8D, 0x07, 0x20)  # first attribute byte
    emit(0xA2, 0x3F)  # 63 remaining bytes
    label("attributes")
    emit(0x8D, 0x07, 0x20)  # STA $2007
    emit(0xCA)  # DEX
    branch(0xD0, "attributes")

    emit(0xA9, 0x0A, 0x8D, 0x01, 0x20)  # show background

    # Pulse 1: 50% duty, constant maximum volume, timer 253 ~= 440.4 Hz.
    emit(0xA9, 0x00, 0x8D, 0x10, 0x40)  # disable DMC
    emit(0xA9, 0x40, 0x8D, 0x17, 0x40)  # inhibit frame IRQ
    emit(0xA9, 0x00, 0x8D, 0x01, 0x40)  # disable pulse sweep
    emit(0xA9, 0xBF, 0x8D, 0x00, 0x40)  # 50% duty, length halt, constant volume 15
    emit(0xA9, 0xFD, 0x8D, 0x02, 0x40)  # timer low = 253
    emit(0xA9, 0x00, 0x8D, 0x03, 0x40)  # timer high = 0
    emit(0xA9, 0x01, 0x8D, 0x15, 0x40)  # enable pulse 1

    # Patch all relative branches and verify their destinations.
    for operand, target in fixups:
        displacement = labels[target] - (operand + 1)
        if not -128 <= displacement <= 127:
            raise ValueError(f"Branch to {target} is out of 6502 range")
        code[operand] = displacement & 0xFF
    for operand, target in fixups:
        displacement = code[operand] if code[operand] < 128 else code[operand] - 256
        if operand + 1 + displacement != labels[target]:
            raise ValueError(f"Invalid branch fixup to {target}")

    # Finish with a self-loop; hardware APU continues producing the tone.
    loop_address = 0x8000 + len(code)
    emit(0x4C, loop_address & 0xFF, (loop_address >> 8) & 0xFF)

    prg = bytearray([0xEA] * 16384)  # NOP-filled unused space
    prg[:len(code)] = code
    prg[0x3FFA:0x4000] = bytes([0x00, 0x80, 0x00, 0x80, 0x00, 0x80])

    chr_rom = bytearray(8192)
    chr_rom[16:24] = bytes([0xFF] * 8)  # tile 1 = solid palette color 1
    header = bytes((0x4E, 0x45, 0x53, 0x1A, 1, 1, 0, 0)) + bytes(8)
    return header + bytes(prg) + bytes(chr_rom)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    data = build_rom()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"Created {args.output} ({len(data)} bytes): red/orange screen, ~440 Hz pulse tone")


if __name__ == "__main__":
    main()
