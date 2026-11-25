// One simulated LE link. All traffic into the guest uses H4, ACL and ATT.
export const word = (bytes, offset = 0) => bytes[offset] | (bytes[offset + 1] << 8);
export const le = value => [value & 255, value >>> 8];
const same = (a, b) => a.length === b.length && a.every((value, i) => value === b[i]);
const trackerUuid = [0x00, 0x34, 0x3f, 0x2d, 0x57, 0xc4, 0x4f, 0xbe,
                     0xb7, 0x49, 0x55, 0xa9, 0x6e, 0xde, 0xb1, 0x3c];
const exportUuid = [0x20, ...trackerUuid.slice(1)];
const handle = 1;
const attCid = 4;
const maxH4Buffer = 8192;

const Command = {
    RESET: 0x0c03, SET_EVENT_MASK: 0x0c01, SET_CONTROLLER_FLOW: 0x0c31,
    HOST_BUFFER_SIZE: 0x0c33, HOST_COMPLETED_PACKETS: 0x0c35,
    LOCAL_VERSION: 0x1001, LOCAL_FEATURES: 0x1003, READ_ADDRESS: 0x1009,
    LE_EVENT_MASK: 0x2001, LE_BUFFER_SIZE: 0x2002, LE_FEATURES: 0x2003,
    ADV_PARAMETERS: 0x2006, ADV_DATA: 0x2008, SCAN_RESPONSE: 0x2009,
    ADV_ENABLE: 0x200a, DISCONNECT: 0x0406,
};

function hasTrackerService(data) {
    for (let offset = 0; offset < data.length;) {
        const size = data[offset];
        if (!size || offset + size >= data.length) break;
        if (data[offset + 1] === 6 || data[offset + 1] === 7) {
            for (let start = offset + 2; start + 16 <= offset + size + 1; start += 16) {
                const uuid = data.slice(start, start + 16);
                // The notification service is advertised when present. The
                // dashboard read service is still discoverable after connect.
                if (same(uuid, trackerUuid) || same(uuid, exportUuid)) return true;
            }
        }
        offset += size + 1;
    }
    return false;
}

export class H4Controller {
    #input = [];
    #output = [];
    #l2cap = [];
    #advertising = false;
    #advType = 0;
    #advData = [];
    #connected = false;
    onAtt = null;
    onDisconnect = null;
    txBytes = 0;
    rxBytes = 0;

    get trackerAdvertising() {
        return this.#advertising && this.#advType === 0 && hasTrackerService(this.#advData);
    }

    #queue(packet) {
        if (this.#output.length + packet.length > maxH4Buffer) throw new Error('H4 output overflow');
        this.#output.push(...packet);
    }

    #event(code, parameters) {
        this.#queue([4, code, parameters.length, ...parameters]);
    }

    #complete(op, status = 0, data = []) {
        this.#event(0x0e, [1, ...le(op), status, ...data]);
    }

    connect() {
        if (!this.trackerAdvertising || this.#connected) throw new Error('Tracker is not advertising');
        this.#advertising = false;
        this.#connected = true;
        // LE Connection Complete, peripheral role, static random central
        // address, 10 ms interval, zero latency and 2 s supervision timeout.
        this.#event(0x3e, [1, 0, ...le(handle), 1, 1, 0xa2, 0xf4, 0xf3, 0xf2, 0xf1, 0xf0,
                           ...le(8), ...le(0), ...le(200), 0]);
        return new AttClient(this);
    }

    sendAtt(pdu) {
        if (!this.#connected) throw new Error('BLE disconnected');
        if (pdu.length > 23) throw new Error('ATT MTU unavailable');
        this.#queue([2, ...le(handle | 0x2000), ...le(pdu.length + 4),
                     ...le(pdu.length), ...le(attCid), ...pdu]);
    }

    #command(op, data) {
        switch (op) {
        case Command.RESET:
            this.#advertising = this.#connected = false;
            this.#advData = [];
            this.#l2cap = [];
            break;
        case Command.LOCAL_VERSION: return this.#complete(op, 0, [9, 0, 0, 9, 0xff, 0xff, 0, 0]);
        case Command.LOCAL_FEATURES: return this.#complete(op, 0, [0, 0, 0, 0, 0x60, 0, 0, 0]);
        case Command.READ_ADDRESS: return this.#complete(op, 0, [1, 0xf4, 0xf3, 0xf2, 0xf1, 0xf0]);
        case Command.LE_FEATURES: return this.#complete(op, 0, Array(8).fill(0));
        case Command.LE_BUFFER_SIZE: return this.#complete(op, 0, [...le(27), 8]);
        case Command.ADV_PARAMETERS: this.#advType = data[4]; break;
        case Command.ADV_DATA: this.#advData = data.slice(1, 1 + data[0]); break;
        case Command.ADV_ENABLE: this.#advertising = data[0] === 1; break;
        case Command.SET_EVENT_MASK:
        case Command.LE_EVENT_MASK:
        case Command.SET_CONTROLLER_FLOW:
        case Command.HOST_BUFFER_SIZE:
        case Command.SCAN_RESPONSE:
            break;
        case Command.HOST_COMPLETED_PACKETS:
            return;
        case Command.DISCONNECT:
            this.#event(0x0f, [0, 1, ...le(op)]);
            this.#event(0x05, [0, ...le(handle), data[2]]);
            this.#connected = false;
            this.onDisconnect?.();
            return;
        default:
            return this.#complete(op, 0x01);
        }
        this.#complete(op);
    }

    #acl(header, data) {
        if (!this.#connected || (header & 0x0fff) !== handle) throw new Error('Unexpected ACL handle');
        this.#event(0x13, [1, ...le(handle), ...le(1)]);
        if (((header >>> 12) & 3) !== 1) this.#l2cap = [];
        this.#l2cap.push(...data);
        if (this.#l2cap.length < 4) return;
        const length = word(this.#l2cap) + 4;
        if (length > maxH4Buffer || this.#l2cap.length > length) throw new Error('Invalid L2CAP length');
        if (this.#l2cap.length < length) return;
        const packet = this.#l2cap;
        this.#l2cap = [];
        if (word(packet, 2) === attCid) this.onAtt?.(packet.slice(4));
    }

    exchange(bytes) {
        this.txBytes += bytes.length;
        if (this.#input.length + bytes.length > maxH4Buffer) throw new Error('H4 input overflow');
        this.#input.push(...bytes);
        while (this.#input.length >= 4) {
            const acl = this.#input[0] === 2;
            if (!acl && this.#input[0] !== 1) throw new Error('Invalid guest H4 packet type');
            const header = acl ? 5 : 4;
            if (this.#input.length < header) break;
            const length = header + (acl ? word(this.#input, 3) : this.#input[3]);
            if (length > maxH4Buffer) throw new Error('Invalid H4 packet size');
            if (this.#input.length < length) break;
            const packet = this.#input.splice(0, length);
            if (acl) this.#acl(word(packet, 1), packet.slice(header));
            else this.#command(word(packet, 1), packet.slice(header));
        }
        const reply = Uint8Array.from(this.#output);
        this.#output = [];
        this.rxBytes += reply.length;
        return reply;
    }
}

class AttClient {
    #pending = null;
    #controller;
    #rx;
    #tx;

    constructor(controller) {
        this.#controller = controller;
        controller.onAtt = pdu => this.#respond(pdu);
        controller.onDisconnect = () => this.#pending?.reject(new Error('BLE disconnected'));
    }

    #respond(pdu) {
        const pending = this.#pending;
        if (!pending) return;
        if (pdu[0] === pending.response) pending.resolve(pdu.slice(1));
        else if (pdu[0] === 1 && pdu[1] === pending.request) {
            pending.reject(new Error(`ATT request ${pdu[1]} failed with status ${pdu[4]}`));
        } else pending.reject(new Error(`Unexpected ATT response ${pdu[0]}`));
    }

    async #request(pdu, response) {
        if (this.#pending) throw new Error('Concurrent ATT requests');
        let timer;
        try {
            return await new Promise((resolve, reject) => {
                this.#pending = { request: pdu[0], response, resolve, reject };
                timer = setTimeout(() => reject(new Error(`ATT request ${pdu[0]} timed out`)), 5000);
                this.#controller.sendAtt(pdu);
            });
        } finally {
            clearTimeout(timer);
            this.#pending = null;
        }
    }

    async discover() {
        const range = await this.#request([6, ...le(1), ...le(0xffff), ...le(0x2800), ...trackerUuid], 7);
        if (range.length !== 4) throw new Error('Unexpected tracker service range');
        const end = word(range, 2);
        for (let start = word(range); start <= end && (!this.#rx || !this.#tx);) {
            const chars = await this.#request([8, ...le(start), ...le(end), ...le(0x2803)], 9);
            const size = chars[0];
            if (size !== 21 || (chars.length - 1) % size || chars.length < 22) {
                throw new Error('Invalid tracker characteristic discovery');
            }
            for (let offset = 1; offset < chars.length; offset += size) {
                const declaration = word(chars, offset);
                const properties = chars[offset + 2];
                const value = word(chars, offset + 3);
                const uuid = chars.slice(offset + 5, offset + size);
                if (declaration < start || value <= declaration || value > end) throw new Error('Invalid GATT handle');
                if (same(uuid, [1, ...trackerUuid.slice(1)]) && (properties & 8)) this.#rx = value;
                if (same(uuid, [2, ...trackerUuid.slice(1)]) && (properties & 2)) this.#tx = value;
                start = declaration + 1;
            }
        }
        if (!this.#rx || !this.#tx) throw new Error('Tracker RX/TX characteristics missing');
    }

    async write(bytes) {
        for (let i = 0; i < bytes.length; i += 20) {
            await this.#request([0x12, ...le(this.#rx), ...bytes.subarray(i, i + 20)], 0x13);
        }
    }

    async read() {
        return this.#request([0x0a, ...le(this.#tx)], 0x0b);
    }
}
