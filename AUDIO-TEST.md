# USB audio test

The GitHub build publishes two TI-Nspire files:

- `NesNspire-CX2-USB-Audio.tns`: the emulator.
- `USB-Audio-Tone-Test.nes.tns`: a small mapper-0 NES ROM that enables pulse channel 1 and generates a steady tone near 440 Hz.

## Calculator test

1. Copy both files to the calculator's Documents folder.
2. Launch `NesNspire-CX2-USB-Audio.tns`.
3. In the emulator's ROM browser, open `USB-Audio-Tone-Test.nes.tns`.
4. Listen through the USB OTG audio adapter. The test ROM intentionally has no graphics; a continuous tone is the expected result.
5. To test an actual game, exit the test ROM and load a game with known music.

Use a USB audio device that accepts standard USB Audio Class playback. A passive USB-C/USB-A-to-3.5 mm cable is not necessarily a USB sound card; the adapter must contain a USB audio DAC.

## Interpreting the log

The emulator's `InfoNES-log.tns` log can distinguish opening the device from maintaining playback:

- `USB audio open: ready` means enumeration/open/start completed, not that audible sound was confirmed.
- `USB audio stopped: ...` indicates a runtime service error. Keep the complete log when reporting this.
- A persistent 440 Hz tone is the end-to-end audible pass condition.

## What CI verifies

Every GitHub build checks the generated sound hooks, the hardware timer code, required nMedia source files, linked ARM audio symbols, the Ndless loader prefix in the final TNS package, and the test ROM's iNES header/reset vector/APU register writes.

CI cannot physically connect to a TI-Nspire or USB audio adapter. The on-calculator tone test is therefore still required to confirm actual device compatibility and audible playback.
