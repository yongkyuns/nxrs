import init, { WasmEmulator } from './esp_emu.js';
import pb from './esp_cosim_pb.js';
import { width, height, readPanel, wordAt } from './panel.mjs';

const { EmulatorMessage, ProtoVersion } = pb.esp_cosim.v1;
const text = new TextDecoder();
const encoder = new TextEncoder();
function panelPng(machine, symbols) {
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    context.putImageData(new ImageData(readPanel(machine, symbols), width, height), 0, 0);
    return canvas.toDataURL().split(',')[1];
}

export async function probe({ commands, symbols, timeoutMs, ble }) {
    await init({ module_or_path: new URL('./emulator.wasm', location.href) });
    const machine = new WasmEmulator('esp32s3');
    machine.load_default_rom();
    machine.set_boot_from_rom(true);
    const response = await fetch('./nuttx.bin');
    if (!response.ok) throw new Error(`Cannot read NuttX image (${response.status})`);
    machine.load_firmware(new Uint8Array(await response.arrayBuffer()));

    const registration = {};
    const uartTx = { gnss: 0, hci: 0 };
    const uartPorts = { gnss: [], hci: [] };
    const send = (peer, message) => {
        machine.cosim_push_inbound(peer, EmulatorMessage.encode(message).finish());
    };
    machine.cosim_attach(1000);
    const serialParts = ble ? [['gnss', 17, 18]] : [['gnss', 17, 18], ['hci', 13, 7]];
    for (const [name, txPin, rxPin] of serialParts) {
        send(name, { register: { name, protoVersion: ProtoVersion.PROTO_VERSION_CURRENT,
                                claims: [{ uart: { txPin, rxPin } }] } });
    }
    machine.cosim_service();

    async function drain() {
        for (let frame; (frame = machine.cosim_take_outbound()).length;) {
            const size = frame[0] | (frame[1] << 8);
            if (frame[2] !== 64) continue;
            const peer = text.decode(frame.subarray(3, 2 + size));
            const msg = EmulatorMessage.decode(frame.subarray(2 + size));
            if (msg.registerAck) {
                registration[peer] = { accepted: msg.registerAck.accepted,
                                       reason: msg.registerAck.rejectReason };
            }
            if (msg.uartTx) {
                uartTx[peer] += msg.uartTx.data.length;
                if (!uartPorts[peer].includes(msg.uartTx.port)) uartPorts[peer].push(msg.uartTx.port);
                if (peer === 'gnss') {
                    const data = await window.gnssExchange(Array.from(msg.uartTx.data));
                    if (data.length) send(peer, { uartRx: { data: Uint8Array.from(data) } });
                }
            }
        }
    }
    await drain();

    function word(name) {
        return wordAt(machine, symbols[name]);
    }

    let image = '';
    let panelSequence = 0;
    let qrDecoded = false;
    async function untilPrompt(command) {
        const start = machine.cycles();
        const time = performance.now();
        const cyclesPerMs = 1000 * machine.cycles_per_us();
        const fixPeriod = 250 * cyclesPerMs;
        const cycleBudget = ble ? timeoutMs * cyclesPerMs : 2400000000;
        let nextFix = start + fixPeriod;
        let nextH4 = start;
        let console = '';
        if (command) machine.uart_input(encoder.encode(command + '\n'));
        while (performance.now() - time < timeoutMs && machine.cycles() - start < cycleBudget) {
            console += machine.run_until(ble ? 1000000 : 250000);
            const cycles = machine.cycles();
            if (ble) {
                const bytes = machine.uart2_output();
                if (bytes.length || cycles >= nextH4) {
                    const reply = await window.h4Exchange(Array.from(bytes));
                    uartTx.hci += bytes.length;
                    if (bytes.length && !uartPorts.hci.length) uartPorts.hci.push(2);
                    if (reply.length) machine.uart2_input(Uint8Array.from(reply));
                    nextH4 = cycles + 5 * cyclesPerMs;
                }
            }
            await drain();
            if (command?.includes('--stream-usb') && cycles >= nextFix) {
                const data = await window.gnssFix();
                if (data.length) send('gnss', { uartRx: { data: Uint8Array.from(data) } });
                nextFix += fixPeriod;
            }
            if (command?.includes('--frames') && (!ble || !qrDecoded)) {
                const sequence = word('tracker_frame_sequence');
                if (sequence > panelSequence && sequence % 2 === 0) {
                    const candidate = panelPng(machine, symbols);
                    if (ble) qrDecoded = await window.pairingPanel(candidate);
                    else image = candidate;
                    panelSequence = sequence;
                }
            }
            if (console.includes('nsh>') || machine.faulted() || machine.needs_restart()) break;
            if (ble && command) {
                const ahead = (cycles - start) / cyclesPerMs - (performance.now() - time);
                if (ahead >= 4) await new Promise(resolve => setTimeout(resolve, ahead));
            }
        }
        return { command, prompt: console.includes('nsh>'), console: console.replaceAll('\r', ''),
                 cycles: machine.cycles() - start,
                 harts: Array.from({ length: machine.num_harts() }, (_, i) => machine.hart_pc(i)) };
    }

    const boot = await untilPrompt(null);
    const bootEntropyWords = machine.browser_device_stats?.().entropyWords ?? 0;
    const runs = [];
    if (boot.prompt) {
        for (const command of commands) {
            const run = await untilPrompt(command);
            runs.push(run);
            if (!run.prompt) break;
        }
    }
    return { boot, commands: runs, cores: machine.num_harts(), registration, uartTx, uartPorts,
             fault: machine.faulted() ? machine.fault_report() : '', restart: machine.needs_restart(),
             devices: machine.browser_device_stats?.(), bootEntropyWords,
             panelSequence, panelPng: image, touchReads: word('tracker_touch_reads'),
             modelError: word('tracker_model_error') };
}
