import pb from './esp_cosim_pb.js';
import { word, le } from './h4.mjs';

const { EmulatorMessage, ProtoVersion } = pb.esp_cosim.v1;
const text = new TextDecoder();
const key = [7, 0, 0x91, 0x20];
const measurementKey = [1, 0, 0x21, 0x30];
const cyclesKey = [2, 0, 0x21, 0x30];

function frame(kind, id, payload) {
    const body = [kind, id, ...le(payload.length), ...payload];
    let a = 0;
    let b = 0;
    for (const byte of body) {
        a = (a + byte) & 255;
        b = (b + a) & 255;
    }
    return Uint8Array.of(0xb5, 0x62, ...body, a, b);
}

export class GnssReceiver {
    #machine;
    #pending = [];
    #rate = 7;
    #measurementMs = 1000;
    #navigationCycles = 1;
    #tow = 0;
    #registered = false;
    #nextFix = 0;

    constructor(machine) {
        this.#machine = machine;
        machine.cosim_attach(1000);
        this.#send({ register: { name: 'gnss', protoVersion: ProtoVersion.PROTO_VERSION_CURRENT,
                                claims: [{ uart: { txPin: 17, rxPin: 18 } }] } });
        machine.cosim_service();
        this.service();
        if (!this.#registered) throw new Error('GNSS co-simulation registration failed');
    }

    #send(message) {
        this.#machine.cosim_push_inbound('gnss', EmulatorMessage.encode(message).finish());
    }

    #reply(data) {
        this.#send({ uartRx: { data } });
    }

    #receive(bytes) {
        this.#pending.push(...bytes);
        while (this.#pending.length >= 6) {
            const length = word(this.#pending, 4);
            if (this.#pending[0] !== 0xb5 || this.#pending[1] !== 0x62 || this.#pending[2] !== 6
                || ![8, 9, 12, 16].includes(length)) throw new Error('Unexpected GNSS command');
            if (this.#pending.length < length + 8) return;
            const message = this.#pending.splice(0, length + 8);
            const id = message[3];
            const payload = message.slice(6, -2);
            if (!frame(6, id, payload).every((value, i) => value === message[i])) {
                throw new Error('Invalid UBX packet');
            }
            if (length >= 12) {
                this.#navigation(id, payload);
                continue;
            }
            if (!key.every((byte, i) => payload[4 + i] === byte)) throw new Error('Invalid UBX key');
            if (id === 0x8a && length === 9) {
                this.#rate = payload[8];
                this.#reply(frame(5, 1, [6, 0x8a]));
            } else if (id === 0x8b && length === 8) {
                this.#reply(frame(6, 0x8b, [1, 0, 0, 0, ...key, this.#rate]));
            } else throw new Error('Unsupported UBX configuration');
        }
    }

    #navigation(id, payload) {
        const set = id === 0x8a && payload.length === 16;
        const get = id === 0x8b && payload.length === 12;
        const cyclesOffset = set ? 10 : 8;
        if (!(set || get) || !measurementKey.every((byte, i) => payload[4 + i] === byte)
            || !cyclesKey.every((byte, i) => payload[cyclesOffset + i] === byte)) {
            throw new Error('Unsupported UBX navigation configuration');
        }
        if (set) {
            this.#measurementMs = word(payload, 8);
            this.#navigationCycles = word(payload, 14);
            this.#reply(frame(5, 1, [6, 0x8a]));
        } else {
            this.#reply(frame(6, 0x8b, [1, 0, 0, 0, ...measurementKey, ...le(this.#measurementMs),
                                      ...cyclesKey, ...le(this.#navigationCycles)]));
        }
    }

    service() {
        for (let data; (data = this.#machine.cosim_take_outbound()).length;) {
            const size = word(data);
            if (data[2] !== 64 || text.decode(data.subarray(3, 2 + size)) !== 'gnss') continue;
            const msg = EmulatorMessage.decode(data.subarray(2 + size));
            if (msg.registerAck) this.#registered = msg.registerAck.accepted;
            if (msg.uartTx?.port === 1) this.#receive(msg.uartTx.data);
        }
        const cycles = this.#machine.cycles();
        if (this.#rate === 1 && cycles >= this.#nextFix) {
            const periodMs = this.#measurementMs * this.#navigationCycles;
            // Delay the first fix until the guest finishes configuring NAV-PVT.
            if (this.#nextFix) {
                const payload = new Uint8Array(92);
                const view = new DataView(payload.buffer);
                this.#tow += periodMs;
                view.setUint32(0, this.#tow, true);
                payload.set([3, 1], 20);
                view.setInt32(24, -115166667, true);
                view.setInt32(28, 481173000, true);
                this.#reply(frame(1, 7, payload));
            }
            this.#nextFix = cycles + periodMs * 1000 * this.#machine.cycles_per_us();
        } else if (this.#rate !== 1) this.#nextFix = 0;
    }
}
