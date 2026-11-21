#!/usr/bin/env python3
"""Prepare the uploaded NesNspire and nMedia source archives for a CX II build."""
import argparse
import pathlib
import re
import shutil
import zipfile

BRIDGE = r'''/* NES APU -> nMedia USB Audio Class output bridge. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <libndls.h>
#include <os.h>
#include "../audio/usb_audio.h"
#include "../audio/audio_buffer.h"
#include "NesNspire_Audio.h"
#ifndef IRQ_USB
#define IRQ_USB (1U << 9)
#endif
#define NES_AUDIO_CAPACITY 16384U
#define NES_AUDIO_PREFILL 9000U
#define NES_AUDIO_RATE 44100U
static UsbAudio *g_usb;
static AudioBuffer g_stream;
static int16_t *g_pcm;
static uint32_t g_write, g_sample_rate = NES_AUDIO_RATE;
static int16_t g_pulse_mix[31];
static int16_t g_tnd_mix[16][16][128];
static int32_t g_dc_x, g_dc_y;
static bool g_failed;
static char g_error[160] = "USB audio not initialized";
/* Own timer 0 for audio timing, then restore the exact OS state at exit.
 * This follows the verified timer setup used by nMedia on CX II. */
#define NES_TIMER_LOAD (*(volatile uint32_t *)0x900C0000U)
#define NES_TIMER_VALUE (*(volatile uint32_t *)0x900C0004U)
#define NES_TIMER_CONTROL (*(volatile uint32_t *)0x900C0008U)
#define NES_TIMER_CLOCK (*(volatile uint32_t *)0x900C0080U)
#define NES_TIMER_TICKS_PER_SECOND 32768U
#define NES_TIMER_ENABLE_32BIT 0x82U
#define NES_TIMER_CLOCK_32768HZ 0x0AU
static uint32_t g_timer_last, g_timer_saved_load, g_timer_saved_control, g_timer_saved_clock;
static uint64_t g_timer_ticks;
static bool g_timer_started, g_timer_saved;
static void nes_timer_restore(void) {
    if (!g_timer_saved) return;
    NES_TIMER_CONTROL &= ~0x80U;
    NES_TIMER_CONTROL = g_timer_saved_control & ~0x80U;
    NES_TIMER_CLOCK = g_timer_saved_clock;
    NES_TIMER_LOAD = g_timer_saved_load;
    NES_TIMER_CONTROL = g_timer_saved_control;
    g_timer_saved = false;
    g_timer_started = false;
}
static bool nes_timer_start(void) {
    if (g_timer_started) return true;
    g_timer_saved_load = NES_TIMER_LOAD;
    g_timer_saved_control = NES_TIMER_CONTROL;
    g_timer_saved_clock = NES_TIMER_CLOCK;
    g_timer_saved = true;
    NES_TIMER_CONTROL &= ~0x80U;
    NES_TIMER_CONTROL = NES_TIMER_ENABLE_32BIT & ~0x80U;
    NES_TIMER_CLOCK = NES_TIMER_CLOCK_32768HZ;
    NES_TIMER_LOAD = UINT32_MAX;
    NES_TIMER_CONTROL = NES_TIMER_ENABLE_32BIT;
    uint32_t previous = NES_TIMER_VALUE;
    unsigned moving = 0;
    for (unsigned polls = 0; polls < 131072U; ++polls) {
        uint32_t current = NES_TIMER_VALUE;
        uint32_t delta = previous - current;
        if (polls >= 32768U && UINT32_MAX - current <= 8192U && delta && delta < 8192U) {
            if (++moving >= 2U) {
                g_timer_last = current;
                g_timer_ticks = 0;
                g_timer_started = true;
                return true;
            }
        }
        previous = current;
    }
    nes_timer_restore();
    return false;
}
static uint32_t nes_audio_now_ms(void) {
    if (!g_timer_started) return 0;
    uint32_t current = NES_TIMER_VALUE;
    g_timer_ticks += (uint32_t)(g_timer_last - current);
    g_timer_last = current;
    return (uint32_t)((g_timer_ticks * 1000ULL) / NES_TIMER_TICKS_PER_SECOND);
}
extern "C" bool audio_platform_cx2(void) {
    return *(volatile uint32_t *)0x900a0000U == 0x202U;
}
extern "C" uint32_t audio_native_usb_irq_mask(void) { return IRQ_USB; }
extern "C" bool audio_dma_valid(void *address, size_t bytes) {
    uintptr_t start = (uintptr_t)address;
    return bytes && bytes <= 0x04000000U && start >= 0x10000000U &&
           start <= 0x14000000U - bytes;
}
static void build_mixer(void) {
    for (unsigned p = 0; p <= 30; ++p) {
        float out = p ? 95.88f / (8128.0f / (float)p + 100.0f) : 0.0f;
        g_pulse_mix[p] = (int16_t)(out * 32767.0f + 0.5f);
    }
    for (unsigned t = 0; t < 16; ++t)
      for (unsigned n = 0; n < 16; ++n)
       for (unsigned d = 0; d < 128; ++d) {
        float input = (float)t / 8227.0f + (float)n / 12241.0f + (float)d / 22638.0f;
        float out = input > 0.0f ? 159.79f / (1.0f / input + 100.0f) : 0.0f;
        g_tnd_mix[t][n][d] = (int16_t)(out * 32767.0f + 0.5f);
       }
}
void nes_audio_init(void) {
    bool timer_ok = nes_timer_start();
    build_mixer(); g_dc_x = g_dc_y = 0; g_failed = false;
    strcpy(g_error, timer_ok ? "USB audio not initialized" : "Hardware monotonic timer initialization failed");
}
int nes_audio_open(int samples_per_sync, int sample_rate) {
    (void)samples_per_sync;
    if (g_usb) return 1;
    if (!g_timer_started && !nes_timer_start()) {
        strcpy(g_error, "Hardware monotonic timer initialization failed");
        return 0;
    }
    g_sample_rate = sample_rate > 0 ? (uint32_t)sample_rate : NES_AUDIO_RATE;
    g_pcm = (int16_t *)malloc((size_t)NES_AUDIO_CAPACITY * 2U * sizeof(int16_t));
    if (!g_pcm) { strcpy(g_error, "PCM buffer allocation failed"); return 0; }
    memset(g_pcm, 0, (size_t)NES_AUDIO_CAPACITY * 2U * sizeof(int16_t));
    memset(&g_stream, 0, sizeof(g_stream));
    g_stream.pcm = g_pcm; g_stream.sample_count = UINT32_MAX;
    g_stream.sample_rate = g_sample_rate; g_stream.pcm_capacity = NES_AUDIO_CAPACITY;
    g_stream.pcm_begin = 0; g_stream.pcm_end = NES_AUDIO_PREFILL;
    g_stream.channels = 2; g_stream.pcm_bits = 16; g_stream.source_bits = 16;
    g_write = NES_AUDIO_PREFILL;
    usb_audio_prefer_rate(g_sample_rate); usb_audio_prefer_precision(16, 2);
    const char *status = NULL;
    g_usb = usb_audio_open(nes_audio_now_ms, &status);
    if (!g_usb) {
        snprintf(g_error, sizeof(g_error), "%s", status ? status : "USB audio open failed");
        free(g_pcm); g_pcm = NULL; memset(&g_stream, 0, sizeof(g_stream)); return 0;
    }
    if (!usb_audio_start(g_usb, &g_stream, 0, 1, 1, 100)) {
        snprintf(g_error, sizeof(g_error), "%s", usb_audio_error(g_usb));
        if (usb_audio_close(g_usb)) {
            g_usb = NULL; free(g_pcm); g_pcm = NULL; memset(&g_stream, 0, sizeof(g_stream));
        } else {
            g_failed = true; strcat(g_error, " (USB teardown pending; buffer retained)");
        }
        return 0;
    }
    g_failed = false; strcpy(g_error, "ready"); return 1;
}
void nes_audio_output(int samples, const unsigned char *p1, const unsigned char *p2,
 const unsigned char *tri, const unsigned char *noise, const unsigned char *dpcm) {
    if (!g_pcm || samples <= 0 || !p1 || !p2 || !tri || !noise || !dpcm) return;
    uint32_t consumed = g_usb ? (uint32_t)(usb_audio_position(g_usb) >> 32) : 0;
    if (g_write - consumed > NES_AUDIO_CAPACITY - (uint32_t)samples) {
        g_stream.failed = true; g_failed = true; return;
    }
    g_stream.pcm_begin = consumed;
    for (int i = 0; i < samples; ++i) {
        unsigned pulse = (unsigned)(p1[i] / 17U) + (unsigned)(p2[i] / 17U);
        if (pulse > 30) pulse = 30;
        unsigned t = tri[i] >> 4, n = noise[i] & 15U, d = dpcm[i] & 127U;
        int32_t x = (int32_t)g_pulse_mix[pulse] + (int32_t)g_tnd_mix[t][n][d];
        int32_t y = x - g_dc_x + (int32_t)(((int64_t)g_dc_y * 32755) >> 15);
        g_dc_x = x; if (y > 32767) y = 32767; if (y < -32768) y = -32768; g_dc_y = y;
        uint32_t slot = g_write & (NES_AUDIO_CAPACITY - 1U);
        g_pcm[slot * 2U] = (int16_t)y; g_pcm[slot * 2U + 1U] = (int16_t)y; ++g_write;
    }
    g_stream.pcm_end = g_write;
}
void nes_audio_service(void) {
    if (!g_usb || g_failed) return;
    if (!usb_audio_service(g_usb)) {
        g_failed = true; g_stream.failed = true;
        snprintf(g_error, sizeof(g_error), "%s [timer=%lu ms raw=%08lx]",
                 usb_audio_error(g_usb), (unsigned long)nes_audio_now_ms(),
                 (unsigned long)NES_TIMER_VALUE);
    }
}
void nes_audio_close(void) {
    if (g_usb) {
        usb_audio_pause(g_usb);
        if (!usb_audio_close(g_usb)) {
            strcpy(g_error, "USB teardown pending; audio memory retained for DMA safety"); return;
        }
        g_usb = NULL;
    }
    free(g_pcm); g_pcm = NULL; memset(&g_stream, 0, sizeof(g_stream));
    g_write = 0; g_failed = false;
    nes_timer_restore();
}
void nes_audio_diagnostics(char *out, size_t capacity) {
    if (!out || !capacity) return;
    char device[160];
    usb_audio_describe(g_usb, device, sizeof(device));
    if (g_usb) usb_audio_capture_stream(g_usb);
    snprintf(out, capacity,
        "error=%s; device=%s; stream=%s; control=%s; transfer=%s; queue=%lu..%lu; pcm_failed=%u; pcm_missed=%u",
        g_error, device, usb_audio_last_stream(), usb_audio_last_control(),
        usb_audio_transfer_fault(), (unsigned long)g_stream.pcm_begin,
        (unsigned long)g_stream.pcm_end, g_stream.failed ? 1U : 0U,
        g_stream.pcm_missed ? 1U : 0U);
}
const char *nes_audio_error(void) { return g_error; }
bool nes_audio_failed(void) { return g_failed; }
'''
HEADER = r'''#ifndef NESNSPIRE_AUDIO_H
#define NESNSPIRE_AUDIO_H
#include <stdbool.h>
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
void nes_audio_init(void);
int nes_audio_open(int samples_per_sync, int sample_rate);
void nes_audio_output(int samples, const unsigned char *pulse1, const unsigned char *pulse2,
 const unsigned char *triangle, const unsigned char *noise, const unsigned char *dpcm);
void nes_audio_service(void);
void nes_audio_close(void);
const char *nes_audio_error(void);
bool nes_audio_failed(void);
void nes_audio_diagnostics(char *out, size_t capacity);
#ifdef __cplusplus
}
#endif
#endif
'''
AUDIO_FILES = [
    "src/audio/usb_audio.c", "src/audio/usb_audio.h",
    "src/audio/usb_audio_schedule.inc", "src/audio/usb_audio_topology.inc",
    "src/audio/usb_audio_controls.inc", "src/audio/usb_audio_health.inc",
    "src/audio/usb_audio_split.inc", "src/audio/usb_audio_format.h",
    "src/audio/usb_audio_formats.c", "src/audio/audio_buffer.h",
    "src/audio/audio_buffer.c", "src/audio/pcm_format.h", "src/audio/input.h",
    "src/storage/usb/usb_route.h",
]
def find_root(base, marker):
    matches = list(base.rglob(marker))
    if not matches:
        raise SystemExit(f"Archive does not contain {marker}")
    for parent in matches[0].parents:
        if (parent / "Makefile").is_file():
            return parent
    raise SystemExit(f"Could not find project root containing Makefile for {marker}")
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nes-zip", required=True)
    ap.add_argument("--nmedia-zip", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    tmp = pathlib.Path("build-input")
    shutil.rmtree(tmp, ignore_errors=True)
    nes_tmp = tmp / "nes"
    nmedia_tmp = tmp / "nmedia"
    nes_tmp.mkdir(parents=True)
    nmedia_tmp.mkdir(parents=True)
    with zipfile.ZipFile(args.nes_zip) as zf:
        zf.extractall(nes_tmp)
    with zipfile.ZipFile(args.nmedia_zip) as zf:
        zf.extractall(nmedia_tmp)
    nes = find_root(nes_tmp, "InfoNES_System_Ndless.cpp")
    nmedia = find_root(nmedia_tmp, "usb_audio.c")
    out = pathlib.Path(args.out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(nes, out, ignore=shutil.ignore_patterns(".git", "*.o", "*.elf", "*.zehn", "*.tns"))
    for rel in AUDIO_FILES:
        src = nmedia / rel
        if not src.is_file(): raise SystemExit(f"Missing nMedia source file: {src}")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # usb_audio.c is compiled as C and called from C++; preserve C symbol names.
    usb_header = out / "src/audio/usb_audio.h"
    header_text = usb_header.read_text()
    if 'extern "C" {' not in header_text:
        include_marker = '#include "input.h"'
        if include_marker not in header_text:
            raise SystemExit("Unexpected usb_audio.h: input.h include not found.")
        header_text = header_text.replace(
            include_marker,
            include_marker + '\n\n#ifdef __cplusplus\nextern "C" {\n#endif',
            1)
        final_guard = header_text.rfind("#endif")
        if final_guard < 0:
            raise SystemExit("Unexpected usb_audio.h: final include guard not found.")
        header_text = (header_text[:final_guard] +
            "#ifdef __cplusplus\n}\n#endif\n\n" + header_text[final_guard:])
    usb_header.write_text(header_text)

    (out / "src/ndless/NesNspire_Audio.cpp").write_text(BRIDGE)
    (out / "src/ndless/NesNspire_Audio.h").write_text(HEADER)
    makefile = out / "Makefile"
    m = makefile.read_text()
    m = m.replace("\tsrc/ndless/InfoNES_System_Ndless.o \\\n", "\tsrc/ndless/InfoNES_System_Ndless.o \\\n\tsrc/ndless/NesNspire_Audio.o \\\n\tsrc/audio/usb_audio.o \\\n\tsrc/audio/usb_audio_formats.o \\\n\tsrc/audio/audio_buffer.o \\\n")
    m = m.replace("CFLAGS := -O3 -marm -Isrc -Wall", "CFLAGS := -O3 -marm -Isrc -Isrc/audio -Isrc/storage/usb -Wall")
    makefile.write_text(m)
    system = out / "src/ndless/InfoNES_System_Ndless.cpp"
    s = system.read_text()
    if '#include "NesNspire_Audio.h"' not in s:
        s = s.replace('#include "../InfoNES_pAPU.h"', '#include "../InfoNES_pAPU.h"\n#include "NesNspire_Audio.h"')
    s = s.replace("APU_Mute = 1;", "APU_Mute = 0;")
    s = s.replace("void InfoNES_Wait() {}", """void InfoNES_Wait() {
  static bool reported = false;
  nes_audio_service();
  if (nes_audio_failed() && !reported) {
    char details[512];
    nes_audio_diagnostics(details, sizeof(details));
    log_message("USB audio stopped: %s", nes_audio_error());
    log_message("USB audio diagnostics: %s", details);
    reported = true;
  }
}""")
    s = re.sub(r"void InfoNES_SoundInit\(void\)\s*\{\s*\}", "void InfoNES_SoundInit(void) { nes_audio_init(); }", s)
    s = re.sub(r"int\s+InfoNES_SoundOpen\(int s, int r\)\s*\{[^}]*\}", """int InfoNES_SoundOpen(int s, int r) {
  int ok = nes_audio_open(s, r);
  log_message("USB audio open: %s (%d Hz, %d samples/frame): %s", ok ? "ready" : "unavailable", r, s, nes_audio_error());
  return ok;
}""", s)
    s = re.sub(r"void InfoNES_SoundClose\(void\)\s*\{\s*\}", "void InfoNES_SoundClose(void) { nes_audio_close(); }", s)
    s = re.sub(r"void InfoNES_SoundOutput\(int s, BYTE \*w1, BYTE \*w2, BYTE \*w3, BYTE \*w4, BYTE \*w5\)\s*\{[^}]*\}", """void InfoNES_SoundOutput(int s, BYTE *w1, BYTE *w2, BYTE *w3, BYTE *w4, BYTE *w5) {
  nes_audio_output(s, w1, w2, w3, w4, w5);
}""", s)
    if "nes_audio_output(s, w1" not in s or "nes_audio_service();" not in s:
        raise SystemExit("Failed to patch the NES sound hooks; upstream source has changed.")
    s = s.replace('"%s/InfoNES.log"', '"%s/InfoNES-log.tns"')
    system.write_text(s)
    (out / "THIRD_PARTY_NOTICES.md").write_text(
        "The USB Audio Class transport is adapted from Nspire-MediaPlayer-Ndless. "
        "See src/audio/NMEDIA-GPL-3.0-LICENSE for its GPL-3.0 license.\\n")
    lic = nmedia / "LICENSE"
    if lic.exists(): shutil.copy2(lic, out / "src/audio/NMEDIA-GPL-3.0-LICENSE")
    print(f"Prepared source tree at {out}")
if __name__ == "__main__": main()
