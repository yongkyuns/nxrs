import init, { WasmEmulator } from './esp_emu.js';
import './jsQR.js';
import symbols from './symbols.mjs';
import { H4Controller } from './h4.mjs';
import { GnssReceiver } from './gnss.mjs';
import { width, height, readPanel, wordAt } from './panel.mjs';
import { streamTracker } from './secure-stream.mjs';
import { Touchscreen } from './touchscreen.mjs';

const encoder = new TextEncoder();
const h4 = new H4Controller();
let phase = 'booting';
let failed = false;

function report(state) {
    phase = state;
    postMessage({ type: 'state', phase });
}

function fail(error) {
    failed = true;
    postMessage({ type: 'error', message: error instanceof Error ? error.message : String(error) });
}

async function pair(psk) {
    report('pairing');
    try {
        const central = h4.connect();
        await central.discover();
        await streamTracker(central, psk, () => report('paired'), sample => {
            postMessage({ type: 'sample', sample });
        });
    } finally {
        psk.fill(0);
    }
}

async function boot() {
    await init({ module_or_path: new URL('./esp_emu_bg.wasm', import.meta.url) });
    const machine = new WasmEmulator('esp32s3');
    machine.load_default_rom();
    machine.set_boot_from_rom(true);
    const response = await fetch('./nuttx.merged.bin');
    if (!response.ok) throw new Error(`Firmware download failed (${response.status})`);
    machine.load_firmware(new Uint8Array(await response.arrayBuffer()));
    const gnss = new GnssReceiver(machine);
    const touch = new Touchscreen(machine, symbols);
    const schedule = new MessageChannel();
    const started = performance.now();
    let nextPanel = started;
    let nextStatus = started;
    let lastFrame = 0;
    let lastQrScan = 0;
    let bootOutput = '';
    let booted = false;

    function step() {
        if (failed) return;
        try {
            touch.service();
            const output = machine.run_until(1000000);
            const reply = h4.exchange(machine.uart2_output());
            if (reply.length) machine.uart2_input(reply);
            gnss.service();
            if (output) postMessage({ type: 'console', output });
            if (machine.faulted() || machine.needs_restart()) {
                throw new Error(machine.faulted() ? machine.fault_report() : 'The ESP32-S3 requested a restart');
            }

            const now = performance.now();
            if (!booted) {
                bootOutput = (bootOutput + output).slice(-4096);
                if (bootOutput.includes('nsh>')) {
                    if (machine.num_harts() !== 2) throw new Error('NuttX reached the shell without both cores');
                    booted = true;
                    report('tracker');
                }
            }

            if (booted && now >= nextPanel) {
                nextPanel = now + 50;
                const sequence = wordAt(machine, symbols.tracker_frame_sequence);
                if (wordAt(machine, symbols.tracker_model_error)) throw new Error('Guest LCD model reported an error');
                const newFrame = sequence > lastFrame;
                const scan = h4.trackerAdvertising && sequence > lastQrScan;
                if (sequence % 2 === 0 && (newFrame || (phase === 'ready' && scan))) {
                    const frame = readPanel(machine, symbols);
                    if (phase === 'tracker') report('ready');
                    // The guest may finish painting before its advertising
                    // command reaches H4. Scan that same frame once both are ready.
                    if (phase === 'ready' && scan) {
                        lastQrScan = sequence;
                        const code = globalThis.jsQR(frame, width, height, { inversionAttempts: 'dontInvert' });
                        if (code && /^NXRS1:[0-9A-F]{64}$/.test(code.data)) {
                            const key = Uint8Array.from(code.data.slice(6).match(/../g), byte => parseInt(byte, 16));
                            pair(key).catch(fail);
                        }
                    }
                    if (newFrame) {
                        lastFrame = sequence;
                        postMessage({ type: 'frame', buffer: frame.buffer }, [frame.buffer]);
                    }
                }
            }

            if (now >= nextStatus) {
                nextStatus = now + 100;
                postMessage({ type: 'device', cores: machine.num_harts(),
                              h4Tx: h4.txBytes, h4Rx: h4.rxBytes });
            }
            if ((phase === 'booting' || phase === 'tracker') && now - started > 20000) {
                throw new Error(booted ? 'The tracker did not draw a frame' : 'NuttX boot timed out');
            }
            const ahead = machine.cycles() / (1000 * machine.cycles_per_us()) - (now - started);
            if (ahead >= 4) setTimeout(step, ahead);
            else schedule.port2.postMessage(0);
        } catch (error) {
            fail(error);
        }
    }

    onmessage = ({ data }) => {
        if (failed || !booted) return;
        if (data.type === 'touch' && ['ready', 'pairing', 'paired'].includes(phase)) {
            touch.enqueue(data);
        } else if (data.type === 'command' && typeof data.command === 'string'
                   && /^[ -~]{1,62}$/.test(data.command)) {
            // CONFIG_LINE_MAX is 64, including the newline and terminating NUL.
            machine.uart_input(encoder.encode(data.command + '\n'));
        }
    };
    schedule.port1.onmessage = step;
    schedule.port2.postMessage(0);
}

boot().catch(fail);
