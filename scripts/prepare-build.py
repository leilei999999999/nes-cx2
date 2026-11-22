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
extern "C" {
#include "../player/native_usb_ui.h"
#include "../player/native_interrupts.h"
#include "../platform/nspire_hardware.h"
#include "../storage/usb/usb_host_session.h"
}
#include "NesNspire_Audio.h"
#ifndef IRQ_USB
#define IRQ_USB (1U << 9)
#endif
#define NES_AUDIO_CAPACITY 32768U
#define NES_AUDIO_START_BUFFER_MS 128U
#define NES_AUDIO_QUEUE_TARGET_MS 128U
#define NES_AUDIO_SERVO_GAIN_PPM 300000U
#define NES_AUDIO_SERVO_LIMIT_PPM 120000
#define NES_AUDIO_SERVO_UPDATE_MS 16U
#define NES_AUDIO_SERVO_FILTER_SHIFT 4U
#define NES_AUDIO_RATE_WINDOW_MS 512U
#define NES_AUDIO_RATE 44100U
static UsbAudio *g_usb;
static AudioBuffer g_stream;
static int16_t *g_pcm;
static uint32_t g_write, g_sample_rate = NES_AUDIO_RATE;
static int16_t g_pulse_mix[31];
static int16_t g_tnd_mix[16][16][128];
static int32_t g_dc_x, g_dc_y;
static bool g_failed, g_stream_started;
static uint32_t g_output_calls, g_output_samples;
static uint32_t g_pace_last_frame, g_pace_last_samples, g_pace_source_rate;
static uint32_t g_pace_effective_rate, g_queue_samples, g_queue_target;
static uint32_t g_queue_last_frame;
static int32_t g_queue_correction_ppm;
static bool g_pace_measured;
static uint32_t g_nonzero[5], g_peak[5];
static char g_error[320] = "USB audio not initialized";
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
static uint32_t g_player_timer_control, g_player_timer_clock;
static const volatile uint32_t *g_native_clock_counter;
static uint32_t g_native_clock_hz, g_native_clock_last, g_native_clock_fraction;
static bool g_native_clock_active;
static bool g_native_ui_owned, g_native_host_owned;
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
static uint64_t nes_clock_ticks_now(void) {
    if (g_native_clock_active && g_native_clock_counter && g_native_clock_hz) {
        uint32_t current = *g_native_clock_counter;
        uint32_t elapsed = current - g_native_clock_last;
        uint64_t scaled = (uint64_t)elapsed * NES_TIMER_TICKS_PER_SECOND +
                          g_native_clock_fraction;
        g_native_clock_last = current;
        g_timer_ticks += scaled / g_native_clock_hz;
        g_native_clock_fraction = (uint32_t)(scaled % g_native_clock_hz);
        return g_timer_ticks;
    }
    if (g_timer_started) {
        uint32_t current = NES_TIMER_VALUE;
        g_timer_ticks += (uint32_t)(g_timer_last - current);
        g_timer_last = current;
    }
    return g_timer_ticks;
}
extern "C" uint64_t monotonic_clock_now_ticks(void) {
    return nes_clock_ticks_now();
}
extern "C" uint32_t monotonic_clock_now_ms(void) {
    return (uint32_t)((nes_clock_ticks_now() * 1000ULL) / NES_TIMER_TICKS_PER_SECOND);
}
static uint32_t nes_audio_now_ms(void) {
    return monotonic_clock_now_ms();
}
static bool nes_usb_progress(void *context) {
    (void)context;
    return true;
}
static bool nes_timer_enter_native(const volatile uint32_t *counter, uint32_t hz) {
    if (!g_timer_started || !g_timer_saved || !counter || !hz || g_native_clock_active)
        return false;
    (void)nes_clock_ticks_now();
    g_player_timer_control = NES_TIMER_CONTROL;
    g_player_timer_clock = NES_TIMER_CLOCK;
    NES_TIMER_CONTROL &= ~0x80U;
    NES_TIMER_CONTROL = g_timer_saved_control & ~0x80U;
    NES_TIMER_CLOCK = g_timer_saved_clock;
    NES_TIMER_LOAD = g_timer_saved_load;
    NES_TIMER_CONTROL = g_timer_saved_control;
    g_native_clock_counter = counter;
    g_native_clock_hz = hz;
    g_native_clock_last = *counter;
    g_native_clock_fraction = 0;
    g_native_clock_active = true;
    return true;
}
static bool nes_timer_leave_native(void) {
    if (!g_native_clock_active) return true;
    (void)nes_clock_ticks_now();
    g_native_clock_active = false;
    g_native_clock_counter = NULL;
    NES_TIMER_CONTROL &= ~0x80U;
    NES_TIMER_CONTROL = g_player_timer_control & ~0x80U;
    NES_TIMER_CLOCK = g_player_timer_clock;
    NES_TIMER_LOAD = UINT32_MAX;
    NES_TIMER_CONTROL = NES_TIMER_ENABLE_32BIT;
    uint32_t previous = NES_TIMER_VALUE;
    for (unsigned polls = 0; polls < 131072U; ++polls) {
        uint32_t current = NES_TIMER_VALUE;
        uint32_t delta = previous - current;
        if (polls >= 32768U && UINT32_MAX - current <= 8192U && delta && delta < 8192U) {
            g_timer_last = current;
            return true;
        }
        previous = current;
    }
    return false;
}
static bool nes_native_enter_clock(void) {
    const volatile uint32_t *counter = NULL;
    uint32_t hz = 0;
    if (!native_usb_ui_clock(&counter, &hz, nes_usb_progress, NULL)) {
        strcpy(g_error, "Native USB clock discovery failed");
        return false;
    }
    if (!nes_timer_enter_native(counter, hz)) {
        strcpy(g_error, "Could not enter native USB clock domain");
        return false;
    }
    return true;
}
static bool nes_native_usb_open_stream(void) {
    unsigned saved = native_critical_enter();
    if (!nes_native_enter_clock()) {
        native_critical_leave(saved);
        return false;
    }
    unsigned kernel = TCT_Local_Control_Interrupts(0);
    native_critical_enter();
    bool ready = false;
    int ui_result = native_usb_ui_acquire(nes_usb_progress, NULL);
    if (ui_result) {
        snprintf(g_error, sizeof(g_error), "Native USB UI acquisition failed (%d)", ui_result);
        goto cleanup;
    }
    g_native_ui_owned = true;
    native_critical_leave(0);
    {
        char message[160] = "Preparing USB host...";
        if (!usb_host_session_prepare(nes_audio_now_ms, nes_usb_progress, NULL,
                                      message, sizeof(message))) {
            snprintf(g_error, sizeof(g_error), "USB host preparation failed: %.120s", message);
            native_critical_enter();
            goto cleanup;
        }
    }
    g_native_host_owned = true;
    usb_audio_set_route_lookup(native_usb_ui_bus_route, NULL);
    usb_audio_set_state_lookup(native_usb_ui_bus_device_state, NULL);
    usb_audio_set_progress(nes_usb_progress, NULL);
    {
        const char *status = NULL;
        g_usb = usb_audio_open(nes_audio_now_ms, &status);
        if (!g_usb) {
            snprintf(g_error, sizeof(g_error), "USB audio discovery failed: %.200s",
                     status ? status : "unknown");
            native_critical_enter();
            goto cleanup;
        }
    }
    /* Configure the device first; playback starts after real APU samples arrive. */
    g_stream_started = false;
    g_pace_last_frame = 0;
    g_pace_last_samples = 0;
    g_pace_source_rate = g_sample_rate;
    g_pace_measured = false;
    ready = true;
    strcpy(g_error, "waiting for NES audio samples");
    native_critical_enter();
cleanup:
    if (!ready && !g_usb && g_native_host_owned) {
        usb_host_session_release();
        g_native_host_owned = false;
    }
    if (!ready && !g_usb && g_native_ui_owned) {
        int release_result = native_usb_ui_release();
        if (release_result)
            snprintf(g_error, sizeof(g_error), "%.140s; native UI release=%d",
                     g_error, release_result);
        else
            g_native_ui_owned = false;
    }
    if (!nes_timer_leave_native()) {
        snprintf(g_error, sizeof(g_error), "%.170s; player timer restore failed", g_error);
        ready = false;
    }
    TCT_Local_Control_Interrupts(kernel);
    native_critical_leave(saved);
    return ready;
}
static bool nes_native_usb_shutdown(void) {
    if (!g_usb && !g_native_host_owned && !g_native_ui_owned) return true;
    unsigned saved = native_critical_enter();
    if (!nes_native_enter_clock()) {
        native_critical_leave(saved);
        return false;
    }
    unsigned kernel = TCT_Local_Control_Interrupts(0);
    native_critical_enter();
    native_critical_leave(0);
    bool closed = true;
    if (g_usb) {
        usb_audio_set_progress(NULL, NULL);
        (void)usb_audio_pause(g_usb);
        if (usb_audio_close(g_usb)) g_usb = NULL;
        else {
            strcpy(g_error, "USB teardown pending; audio memory retained for DMA safety");
            closed = false;
        }
    }
    native_critical_enter();
    if (closed && g_native_host_owned) {
        usb_host_session_release();
        g_native_host_owned = false;
    }
    if (closed && g_native_ui_owned) {
        int release_result = native_usb_ui_release();
        if (release_result) {
            snprintf(g_error, sizeof(g_error), "Native USB UI restore failed (%d)",
                     release_result);
            closed = false;
        } else g_native_ui_owned = false;
    }
    if (!nes_timer_leave_native()) {
        strcpy(g_error, "Player timer restore failed after USB shutdown");
        closed = false;
    }
    TCT_Local_Control_Interrupts(kernel);
    native_critical_leave(saved);
    return closed;
}
extern "C" void app_task_io_pause_service(bool paused) { (void)paused; }
extern "C" bool sram_uses_native_clone(void) { return false; }
extern "C" uint32_t sram_expected_ttbr(void) { return 0; }
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
    g_stream.pcm_begin = 0; g_stream.pcm_end = 0;
    g_stream.channels = 2; g_stream.pcm_bits = 16; g_stream.source_bits = 16;
    g_write = 0;
    g_stream_started = false;
    g_output_calls = g_output_samples = 0;
    g_pace_last_frame = 0;
    g_pace_last_samples = 0;
    g_pace_source_rate = g_sample_rate;
    g_pace_effective_rate = g_sample_rate;
    g_queue_samples = 0;
    g_queue_target = (uint32_t)(((uint64_t)g_sample_rate *
                                 NES_AUDIO_QUEUE_TARGET_MS + 999ULL) / 1000ULL);
    g_queue_last_frame = 0;
    g_queue_correction_ppm = 0;
    g_pace_measured = false;
    memset(g_nonzero, 0, sizeof(g_nonzero)); memset(g_peak, 0, sizeof(g_peak));
    usb_audio_prefer_rate(g_sample_rate); usb_audio_prefer_precision(16, 2);
    if (!nes_native_usb_open_stream()) {
        if (!g_usb && !g_failed && !g_native_host_owned && !g_native_ui_owned) {
            free(g_pcm); g_pcm = NULL; memset(&g_stream, 0, sizeof(g_stream));
        }
        return 0;
    }
    g_failed = false;
    strcpy(g_error, "waiting for NES audio samples");
    return 1;
}
static bool nes_audio_start_if_ready(void) {
    if (!g_usb || g_stream_started) return true;
    /* Queue the 64 ms USB schedule plus a 64 ms PCM reserve. The larger reserve
     * absorbs normal frame-time jitter without adding steady-state playback latency. */
    uint32_t needed = (uint32_t)(((uint64_t)g_sample_rate *
                                 NES_AUDIO_START_BUFFER_MS + 999ULL) / 1000ULL) + 2U;
    if (g_write < needed) return true;
    if (!usb_audio_start(g_usb, &g_stream, 0, 1, 1, 100)) {
        snprintf(g_error, sizeof(g_error), "USB audio start failed: %.220s",
                 usb_audio_error(g_usb));
        g_stream.failed = true;
        g_failed = true;
        return false;
    }
    g_pace_last_frame = usb_audio_frame_count(g_usb);
    g_pace_last_samples = g_output_samples;
    g_pace_source_rate = g_sample_rate;
    g_pace_effective_rate = g_sample_rate;
    g_queue_samples = g_write;
    g_queue_target = (uint32_t)(((uint64_t)g_sample_rate *
                                 NES_AUDIO_QUEUE_TARGET_MS + 999ULL) / 1000ULL);
    g_queue_last_frame = usb_audio_frame_count(g_usb);
    g_queue_correction_ppm = 0;
    g_pace_measured = false;
    g_stream_started = true;
    strcpy(g_error, "ready");
    return true;
}
void nes_audio_output(int samples, const unsigned char *p1, const unsigned char *p2,
 const unsigned char *tri, const unsigned char *noise, const unsigned char *dpcm) {
    if (g_failed || !g_pcm || samples <= 0 || !p1 || !p2 || !tri || !noise || !dpcm) return;
    uint32_t consumed = (g_usb && g_stream_started)
        ? (uint32_t)(usb_audio_position(g_usb) >> 32) : 0;
    uint32_t queued = g_write - consumed;
    if (queued > NES_AUDIO_CAPACITY - (uint32_t)samples) {
        g_stream.failed = true; g_failed = true;
        snprintf(g_error, sizeof(g_error),
                 "PCM ring overrun: queued=%lu batch=%d pace=%luHz",
                 (unsigned long)queued, samples, (unsigned long)g_pace_source_rate);
        return;
    }
    g_stream.pcm_begin = consumed;
    ++g_output_calls; g_output_samples += (uint32_t)samples;
    for (int i = 0; i < samples; ++i) {
        unsigned raw[5] = {p1[i], p2[i], tri[i], noise[i], dpcm[i]};
        for (unsigned c = 0; c < 5; ++c) {
            if (raw[c]) ++g_nonzero[c];
            if (raw[c] > g_peak[c]) g_peak[c] = raw[c];
        }
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
    if (!nes_audio_start_if_ready()) return;
}
static void nes_audio_update_source_rate(void) {
    if (!g_usb || !g_stream_started) return;
    /* u->frame is advanced from the controller's 1 ms FRINDEX clock inside
     * usb_audio_service(). It remains valid across the TI-OS/native clock swap. */
    uint32_t frame = usb_audio_frame_count(g_usb);
    uint32_t elapsed_frames = frame - g_pace_last_frame;
    if (elapsed_frames < NES_AUDIO_RATE_WINDOW_MS) return;
    uint32_t produced = g_output_samples - g_pace_last_samples;
    if (produced) {
        uint64_t measured64 = (uint64_t)produced * 1000ULL / elapsed_frames;
        if (measured64 < 12000ULL) measured64 = 12000ULL;
        if (measured64 > 66000ULL) measured64 = 66000ULL;
        uint32_t measured = (uint32_t)measured64;
        if (!g_pace_measured) {
            g_pace_source_rate = measured;
            g_pace_measured = true;
        } else {
            /* Smooth half-second measurements to avoid audible resampler pitch jumps. */
            g_pace_source_rate = (g_pace_source_rate * 3U + measured + 2U) / 4U;
        }
    }
    g_pace_last_frame = frame;
    g_pace_last_samples = g_output_samples;
}
/* Correct long-term source/device clock mismatch from the live PCM reserve.
 * A 30% proportional gain moves the playback rate far enough to recover from
 * the measured drift; clamp at +/-12% and smooth the correction to avoid jumps.
 * This changes the playback clock, not the startup buffer or scheduling lead. */
static void nes_audio_apply_buffer_servo(void) {
    if (!g_usb || !g_stream_started || !g_pace_source_rate) return;
    /* InfoNES_Wait runs once per scanline. Do not modulate USB playback on
     * every scanline; sample the queue at a stable 16 ms control interval. */
    uint32_t frame = usb_audio_frame_count(g_usb);
    if ((uint32_t)(frame - g_queue_last_frame) < NES_AUDIO_SERVO_UPDATE_MS)
        return;
    g_queue_last_frame = frame;
    uint32_t consumed = (uint32_t)(usb_audio_position(g_usb) >> 32);
    uint32_t queued = g_write - consumed;
    uint32_t target = (uint32_t)(((uint64_t)g_sample_rate *
                                  NES_AUDIO_QUEUE_TARGET_MS + 999ULL) / 1000ULL);
    if (!target) target = 1;
    g_queue_samples = queued;
    g_queue_target = target;

    int64_t error = (int64_t)queued - (int64_t)target;
    int64_t wanted = error * (int64_t)NES_AUDIO_SERVO_GAIN_PPM / (int64_t)target;
    if (wanted > NES_AUDIO_SERVO_LIMIT_PPM) wanted = NES_AUDIO_SERVO_LIMIT_PPM;
    if (wanted < -NES_AUDIO_SERVO_LIMIT_PPM) wanted = -NES_AUDIO_SERVO_LIMIT_PPM;

    /* A 16-sample control filter spans about 256 ms at 16 ms updates. */
    g_queue_correction_ppm =
        (g_queue_correction_ppm * 15 + (int32_t)wanted) / 16;
    int64_t factor = 1000000LL + g_queue_correction_ppm;
    uint32_t rate = (uint32_t)(((uint64_t)g_pace_source_rate *
                                (uint64_t)factor + 500000ULL) / 1000000ULL);
    if (rate < 12000U) rate = 12000U;
    if (rate > 66000U) rate = 66000U;
    if (rate != g_pace_effective_rate) {
        usb_audio_set_source_rate(g_usb, rate);
        g_pace_effective_rate = rate;
    }
}
void nes_audio_service(void) {
    if (!g_usb || !g_stream_started || g_failed) return;
    if (!usb_audio_service(g_usb)) {
        g_failed = true; g_stream.failed = true;
        snprintf(g_error, sizeof(g_error), "%s [timer=%lu ms raw=%08lx]",
                 usb_audio_error(g_usb), (unsigned long)nes_audio_now_ms(),
                 (unsigned long)NES_TIMER_VALUE);
        return;
    }
    nes_audio_update_source_rate();
    nes_audio_apply_buffer_servo();
}
void nes_audio_close(void) {
    if (!nes_native_usb_shutdown()) return;
    free(g_pcm); g_pcm = NULL; memset(&g_stream, 0, sizeof(g_stream));
    g_write = 0; g_failed = false; g_stream_started = false;
    nes_timer_restore();
}
void nes_audio_diagnostics(char *out, size_t capacity) {
    if (!out || !capacity) return;
    char device[160];
    usb_audio_describe(g_usb, device, sizeof(device));
    if (g_usb) usb_audio_capture_stream(g_usb);
    snprintf(out, capacity,
        "error=%s; device=%s; stream=%s; control=%s; transfer=%s; queue=%lu..%lu; pcm_failed=%u; pcm_missed=%u; started=%u pace=%luHz applied=%luHz frame=%lu measured=%u reserve=%lu/%lu servo=%ldppm; APU calls=%lu samples=%lu nonzero[p1,p2,tri,noise,dpcm]=%lu,%lu,%lu,%lu,%lu peaks=%lu,%lu,%lu,%lu,%lu",
        g_error, device, usb_audio_last_stream(), usb_audio_last_control(),
        usb_audio_transfer_fault(), (unsigned long)g_stream.pcm_begin,
        (unsigned long)g_stream.pcm_end, g_stream.failed ? 1U : 0U,
        g_stream.pcm_missed ? 1U : 0U, g_stream_started ? 1U : 0U,
        (unsigned long)g_pace_source_rate, (unsigned long)g_pace_effective_rate,
        (unsigned long)(g_usb ? usb_audio_frame_count(g_usb) : 0U),
        g_pace_measured ? 1U : 0U, (unsigned long)g_queue_samples,
        (unsigned long)g_queue_target, (long)g_queue_correction_ppm,
        (unsigned long)g_output_calls,
        (unsigned long)g_output_samples, (unsigned long)g_nonzero[0],
        (unsigned long)g_nonzero[1], (unsigned long)g_nonzero[2],
        (unsigned long)g_nonzero[3], (unsigned long)g_nonzero[4],
        (unsigned long)g_peak[0], (unsigned long)g_peak[1],
        (unsigned long)g_peak[2], (unsigned long)g_peak[3],
        (unsigned long)g_peak[4]);
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
NATIVE_FILES = [
    "src/player/native_usb_ui.c", "src/player/native_usb_ui.h",
    "src/player/native_usb_ui_resolver.c", "src/player/native_usb_ui_resolver.h",
    "src/player/native_usb_ui_patterns.h", "src/player/native_usb_bus.inc",
    "src/player/native_usb_timing.h", "src/player/native_interrupts.c",
    "src/player/native_interrupts.h", "src/player/storage_mutation.c",
    "src/player/storage_mutation.h", "src/platform/nspire_hardware.c",
    "src/platform/nspire_hardware.h", "src/platform/portable_reader_platform.c",
    "src/platform/portable_reader_platform.h", "src/platform/portable_resolver.c",
    "src/platform/portable_resolver.h", "src/platform/portable_patterns.h",
    "src/platform/portable_storage_snapshot.h", "src/platform/arm926_ram_span.h",
    "src/platform/arm926_cpu_ram_span.h", "src/storage/usb/usb_host_session.c",
    "src/storage/usb/usb_host_session.h", "src/storage/usb/usb_phase_timing.h",
    "src/storage/usb/usb_registers.h", "src/storage/usb/usb_autoconnect.h",
    "src/storage/nand_page_reader.h", "src/storage/nand_address.h",
    "src/storage/cx_nand_reader.h", "src/storage/spi_nand_reader.h",
    "src/player/app_task_io.h", "src/player/native_file_io.h",
    "src/sram.h",
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
    for rel in NATIVE_FILES:
        src = nmedia / rel
        if not src.is_file(): raise SystemExit(f"Missing nMedia native USB source file: {src}")
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
    # Add a read-only FOTG210 register snapshot so an early root-port rejection
    # is distinguishable from a device-enumeration or streaming failure.
    declaration = "void usb_audio_debug_snapshot(char *text, size_t capacity);"
    if declaration not in header_text:
        linkage_close = "#ifdef __cplusplus" + chr(10) + "}" + chr(10) + "#endif"
        if linkage_close in header_text:
            header_text = header_text.replace(
                linkage_close, declaration + chr(10) + linkage_close, 1)
        else:
            final_guard = header_text.rfind("#endif")
            if final_guard < 0:
                raise SystemExit("Unexpected usb_audio.h: final include guard not found.")
            header_text = header_text[:final_guard] + declaration + chr(10) + header_text[final_guard:]
    usb_header.write_text(header_text)

    # Expose a foreground-only source-rate adjustment so streamed PCM follows
    # the actual NES frame production rate instead of assuming 60 FPS.
    rate_decl = "void usb_audio_set_source_rate(UsbAudio *u, uint32_t source_rate);"
    frame_decl = "uint32_t usb_audio_frame_count(const UsbAudio *u);"
    marker = "bool usb_audio_service(UsbAudio *u);"
    if marker not in header_text:
        raise SystemExit("Unexpected usb_audio.h: usb_audio_service declaration not found.")
    if rate_decl not in header_text:
        header_text = header_text.replace(marker, rate_decl + chr(10) + marker, 1)
    if frame_decl not in header_text:
        header_text = header_text.replace(marker, frame_decl + chr(10) + marker, 1)
    usb_header.write_text(header_text)
    schedule = out / "src/audio/usb_audio_schedule.inc"
    schedule_text = schedule.read_text()
    start_tail = """    if (!wait_status(u, PSS, PSS)) {
        usb_audio_pause(u);
        return false;
    }
    return true;
}"""
    rate_function = start_tail + """

/* Keep the PCM source clock locked to the rate at which the emulator produces
 * samples. The USB endpoint still runs at its negotiated hardware rate. */
void usb_audio_set_source_rate(UsbAudio *u, uint32_t source_rate)
{
    if (!u || !u->running || !source_rate || !u->format.sample_rate)
        return;
    u->step = ((uint64_t)source_rate << 32) / u->format.sample_rate;
}"""
    if "void usb_audio_set_source_rate(" not in schedule_text:
        if start_tail not in schedule_text:
            raise SystemExit("Unexpected usb_audio_schedule.inc: usb_audio_start tail not found.")
        schedule_text = schedule_text.replace(start_tail, rate_function, 1)
    frame_function = """

/* Number of elapsed 1 ms USB frames since stream start. */
uint32_t usb_audio_frame_count(const UsbAudio *u)
{
    return u ? u->frame : 0;
}"""
    if "uint32_t usb_audio_frame_count(" not in schedule_text:
        schedule_text += frame_function
    schedule.write_text(schedule_text)

    (out / "src/ndless/NesNspire_Audio.cpp").write_text(BRIDGE)
    (out / "src/ndless/NesNspire_Audio.h").write_text(HEADER)
    usb_source = out / "src/audio/usb_audio.c"
    usb_source_text = usb_source.read_text()
    if "#define LEAD 192U" not in usb_source_text:
        raise SystemExit("Unexpected usb_audio.c: expected 192-frame scheduling lead.")
    usb_source_text = usb_source_text.replace("#define LEAD 192U", "#define LEAD 64U", 1)
    usb_source.write_text(usb_source_text)
    connected_marker = """static bool connected(void)
{
    return (PORT & 5U) == 5U && !(PORT & (1U << 7));
}"""
    snapshot_function = connected_marker + """

/* Read-only diagnostics for the root-port/controller state at audio-open time. */
void usb_audio_debug_snapshot(char *text, size_t capacity)
{
    if (!text || !capacity) return;
    uint32_t port = PORT, command = CMD, otg = OTG, status = STS;
    unsigned connected_bit = (port & 1U) != 0;
    unsigned enabled_bit = (port & 4U) != 0;
    unsigned suspended_bit = (port & (1U << 7)) != 0;
    unsigned running_bit = (command & RUN) != 0;
    unsigned speed = (otg >> 22) & 3U;
    snprintf(text, capacity,
        "USB regs PORT=%08lx CMD=%08lx OTG=%08lx STS=%08lx CAP=%08lx; "
        "port_connected=%u port_enabled=%u port_suspended=%u controller_running=%u speed=%u",
        (unsigned long)port, (unsigned long)command, (unsigned long)otg,
        (unsigned long)status, (unsigned long)REG(0x00), connected_bit,
        enabled_bit, suspended_bit, running_bit, speed);
}"""
    if "void usb_audio_debug_snapshot(" not in usb_source_text:
        if connected_marker not in usb_source_text:
            raise SystemExit("Unexpected usb_audio.c: connected() implementation changed.")
        usb_source_text = usb_source_text.replace(connected_marker, snapshot_function, 1)
        usb_source.write_text(usb_source_text)

    makefile = out / "Makefile"
    m = makefile.read_text()
    m = m.replace("\tsrc/ndless/InfoNES_System_Ndless.o \\\n", "\tsrc/ndless/InfoNES_System_Ndless.o \\\n\tsrc/ndless/NesNspire_Audio.o \\\n\tsrc/audio/usb_audio.o \\\n\tsrc/audio/usb_audio_formats.o \\\n\tsrc/audio/audio_buffer.o \\\n\tsrc/player/native_usb_ui.o \\\n\tsrc/player/native_usb_ui_resolver.o \\\n\tsrc/player/native_interrupts.o \\\n\tsrc/player/storage_mutation.o \\\n\tsrc/platform/nspire_hardware.o \\\n\tsrc/platform/portable_reader_platform.o \\\n\tsrc/platform/portable_resolver.o \\\n\tsrc/storage/usb/usb_host_session.o \\\n")
    m = m.replace("CFLAGS := -O3 -marm -Isrc -Wall", "CFLAGS := -O3 -marm -Isrc -Isrc/audio -Isrc/player -Isrc/platform -Isrc/storage/usb -ffunction-sections -fdata-sections -Wall")
    makefile.write_text(m)
    apu_source = out / "src/InfoNES_pAPU.cpp"
    if not apu_source.is_file():
        raise SystemExit("Missing NES APU renderer: src/InfoNES_pAPU.cpp")
    apu_text = apu_source.read_text()
    # Drive the legacy APU envelope and sweep dividers at their real clock
    # rates. The upstream code subtracts a few units per PCM sample, but
    # checks an unsigned phase against zero; changing only the type to signed
    # makes it tick thousands of times too quickly. Use sample-rate-scaled
    # phase increments: 240 Hz for envelopes, 120 Hz for sweeps.
    if "#include <stdint.h>" not in apu_text:
        apu_text = "#include <stdint.h>\n" + apu_text
    for phase in ("ApuC1EnvPhase", "ApuC1SweepPhase",
                  "ApuC2EnvPhase", "ApuC2SweepPhase", "ApuC4EnvPhase"):
        apu_text, changed = re.subn(
            rf"(?m)^([ \t]*)DWORD([ \t]+{phase}[ \t]*;)",
            rf"\g<1>int32_t\g<2>",
            apu_text,
        )
        if changed != 1:
            raise SystemExit(f"Expected exactly one APU phase declaration for {phase}")
    phase_timers = (
        ("ApuC1EnvPhase", 4, "ApuC1EnvDelay", 240),
        ("ApuC2EnvPhase", 4, "ApuC2EnvDelay", 240),
        ("ApuC4EnvPhase", 4, "ApuC4EnvDelay", 240),
        ("ApuC1SweepPhase", 2, "ApuC1SweepDelay", 120),
        ("ApuC2SweepPhase", 2, "ApuC2SweepDelay", 120),
    )
    for phase, old_step, delay, tick_rate in phase_timers:
        decrement = rf"(?m)^([ \t]*){phase}[ \t]*-=[ \t]*{old_step};[ \t]*$"
        apu_text, changed = re.subn(
            decrement, rf"\g<1>{phase} -= {tick_rate};", apu_text)
        if changed != 1:
            raise SystemExit(f"Expected exactly one APU countdown for {phase}")
        divider = rf"(?m)^([ \t]*){phase}[ \t]*\+=[ \t]*{delay};[ \t]*$"
        apu_text, changed = re.subn(
            divider,
            rf"\g<1>{phase} += (int32_t)({delay} * ApuSampleRate);",
            apu_text,
        )
        if changed != 1:
            raise SystemExit(f"Expected exactly one APU divider reload for {phase}")
    apu_source.write_text(apu_text)
    apu_header = out / "src/InfoNES_pAPU.h"
    if not apu_header.is_file():
        raise SystemExit("Missing NES APU quality header: src/InfoNES_pAPU.h")
    apu_header_text = apu_header.read_text()
    apu_header_text, quality_changes = re.subn(
        r"(?m)^([ \t]*#define[ \t]+pAPU_QUALITY[ \t]+)3([ \t]*)$",
        r"\g<1>2\g<2>",
        apu_header_text,
    )
    if quality_changes != 1:
        raise SystemExit("Expected exactly one pAPU_QUALITY=3 setting in InfoNES_pAPU.h")
    apu_header.write_text(apu_header_text)
    apu_vsync_match = re.search(r"\bInfoNES_pAPUVsync\s*\([^)]*\)\s*\{\s*", apu_text)
    if not apu_vsync_match:
        raise SystemExit("Could not locate InfoNES_pAPUVsync() in the upstream APU renderer.")
    apu_vsync = apu_vsync_match.group(0)
    zero_channels = apu_vsync + "  /* Never reuse tail samples when a channel renderer exits early/disabled. */\n  for (unsigned int i = 0; i < ApuSamplesPerSync; ++i)\n    for (unsigned int channel = 0; channel < 5; ++channel)\n      wave_buffers[channel][i] = 0;\n"
    if "Never reuse tail samples" not in apu_text:
        apu_text = apu_text.replace(apu_vsync, zero_channels, 1)
        apu_source.write_text(apu_text)

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
  if (!ok) {
    char details[512];
    nes_audio_diagnostics(details, sizeof(details));
    log_message("USB audio open diagnostics: %s", details);
  }
  return ok;
}""", s)
    s = re.sub(r"void InfoNES_SoundClose\(void\)\s*\{\s*\}", """void InfoNES_SoundClose(void) {
  char details[768];
  nes_audio_diagnostics(details, sizeof(details));
  log_message("NES APU audio diagnostics: %s", details);
  nes_audio_close();
}""", s)
    s = re.sub(r"void InfoNES_SoundOutput\(int s, BYTE \*w1, BYTE \*w2, BYTE \*w3, BYTE \*w4, BYTE \*w5\)\s*\{[^}]*\}", """void InfoNES_SoundOutput(int s, BYTE *w1, BYTE *w2, BYTE *w3, BYTE *w4, BYTE *w5) {
  nes_audio_output(s, w1, w2, w3, w4, w5);
}""", s)
    if "nes_audio_output(s, w1" not in s or "nes_audio_service();" not in s:
        raise SystemExit("Failed to patch the NES sound hooks; upstream source has changed.")
    bridge_source = (out / "src/ndless/NesNspire_Audio.cpp").read_text()
    native_contract = (
        "native_usb_ui_clock(", "native_usb_ui_acquire(",
        "usb_host_session_prepare(", "usb_audio_set_route_lookup(",
        "usb_audio_set_state_lookup(", "native_usb_ui_release(",
        "monotonic_clock_now_ticks(",
    )
    missing_native = [symbol for symbol in native_contract if symbol not in bridge_source]
    if missing_native:
        raise SystemExit("Incomplete native USB audio integration: " + ", ".join(missing_native))
    s = s.replace('"%s/InfoNES.log"', '"%s/InfoNES-log.tns"')
    system.write_text(s)
    (out / "THIRD_PARTY_NOTICES.md").write_text(
        "The USB Audio Class transport is adapted from Nspire-MediaPlayer-Ndless. "
        "See src/audio/NMEDIA-GPL-3.0-LICENSE for its GPL-3.0 license.\\n")
    lic = nmedia / "LICENSE"
    if lic.exists(): shutil.copy2(lic, out / "src/audio/NMEDIA-GPL-3.0-LICENSE")
    print(f"Prepared source tree at {out}")
if __name__ == "__main__": main()
