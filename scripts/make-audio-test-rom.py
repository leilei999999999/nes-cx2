#!/usr/bin/env python3
"""Create a valid NROM-128 ROM that plays a continuous 440 Hz pulse tone."""
from __future__ import annotations

import argparse
import pathlib


def build_rom() -> bytes:
    # 6502 program at $8000. Pulse 1 timer = 253 -> about 440 Hz on NTSC NES.
    code = bytes([
        0x78,                   # SEI
        0xD8,                   # CLD
        0xA2, 0xFF,             # LDX #$FF
        0x9A,                   # TXS
        0xA9, 0x00,             # LDA #$00
        0x8D, 0x10, 0x40,       # STA $4010: disable DMC IRQ/rate
        0xA9, 0x40,             # LDA #$40
        0x8D, 0x17, 0x40,       # STA $4017: inhibit frame IRQ
        0xA9, 0x00,             # LDA #$00
        0x8D, 0x01, 0x40,       # STA $4001: disable pulse sweep
        0xA9, 0xBF,             # LDA #$BF: 50% duty, halt length, constant volume 15
        0x8D, 0x00, 0x40,       # STA $4000
        0xA9, 0xFD,             # LDA #$FD: timer low for ~440 Hz
        0x8D, 0x02, 0x40,       # STA $4002
        0xA9, 0x00,             # LDA #$00
        0x8D, 0x03, 0x40,       # STA $4003: timer high and length reload
        0xA9, 0x01,             # LDA #$01
        0x8D, 0x15, 0x40,       # STA $4015: enable pulse 1
        0x4C, 0x2D, 0x80,       # JMP $802D (stable idle loop)
    ])
    # Correct the loop address from the actual program length; keep it self-looping.
    loop_addr = 0x8000 + len(code) - 3
    code = code[:-2] + bytes((loop_addr & 0xFF, (loop_addr >> 8) & 0xFF))
    prg = bytearray([0xEA] * 16384)  # NOP-filled unused ROM space
    prg[:len(code)] = code
    # NMI, RESET, IRQ vectors all point to the startup at $8000.
    prg[0x3FFA:0x4000] = bytes([0x00, 0x80, 0x00, 0x80, 0x00, 0x80])
    header = bytearray(b"NES\x1a")
    header += bytes([1, 1, 0, 0])  # 16 KiB PRG, 8 KiB CHR, mapper 0
    header += bytes(8)
    chr_rom = bytes(8192)
    return bytes(header) + bytes(prg) + chr_rom


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    data = build_rom()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"Created {args.output} ({len(data)} bytes), mapper 0, pulse 1 ~440 Hz")


if __name__ == "__main__":
    main()
