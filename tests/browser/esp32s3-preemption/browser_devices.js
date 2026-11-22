// ESP32-S3 UART2 register offsets and interrupt bits used by the NuttX port.
const FIFO = 0x00;
const INT_RAW = 0x04;
const INT_STATUS = 0x08;
const INT_ENABLE = 0x0c;
const INT_CLEAR = 0x10;
const STATUS = 0x1c;
const CONF0 = 0x20;
const CONF1 = 0x24;
const CLK_CONF = 0x78;
const RX_FULL = 1 << 0;
const TX_EMPTY = 1 << 1;
const RX_TIMEOUT = 1 << 8;
const TX_DONE = 1 << 14;
const RX_RESET = 1 << 17;
const TX_RESET = 1 << 18;
const RX_TIMEOUT_ENABLE = 1 << 23;
const FIFO_SIZE = 128;
const HOST_QUEUE_LIMIT = 8192;

// One guest per Wasm instance. The H4 peer exchanges ordered bytes with the
// guest's UART. Transfer timing is at the browser batch boundary.
class BrowserDevices {
    #regs = new Int32Array(32);
    #rx = [];
    #tx = [];
    #entropy = new Uint32Array(1);
    #rxBytes = 0;
    #txBytes = 0;
    #randomWords = 0;

    reset() {
        this.#regs.fill(0);
        this.#regs[CLK_CONF / 4] = 0x03701000;
        this.#rx = [];
        this.#tx = [];
        this.#entropy.fill(0);
        this.#rxBytes = 0;
        this.#txBytes = 0;
        this.#randomWords = 0;
    }

    receive(data) {
        if (data.length > HOST_QUEUE_LIMIT - this.#rx.length) {
            throw new Error('UART2 receive queue exhausted');
        }
        for (const byte of data) this.#rx.push(byte);
        this.#rxBytes += data.length;
    }

    transmit() {
        const data = Uint8Array.from(this.#tx);
        this.#tx = [];
        return data;
    }

    #interrupts() {
        const config = this.#regs[CONF1 / 4];
        const count = Math.min(FIFO_SIZE, this.#rx.length);
        const threshold = Math.max(1, config & 0x3ff);
        let events = TX_EMPTY | TX_DONE;
        if (count >= threshold) events |= RX_FULL;
        if (count > 0 && (config & RX_TIMEOUT_ENABLE)) events |= RX_TIMEOUT;
        return events;
    }

    #read(offset) {
        switch (offset) {
        case FIFO: return this.#rx.shift() ?? 0;
        case STATUS: return Math.min(FIFO_SIZE, this.#rx.length);
        case INT_RAW: return this.#interrupts();
        case INT_STATUS: return this.#interrupts() & this.#regs[INT_ENABLE / 4];
        case INT_CLEAR: return 0;
        default: return this.#regs[offset >>> 2];
        }
    }

    #write(offset, word) {
        switch (offset) {
        case FIFO:
            if (this.#tx.length === HOST_QUEUE_LIMIT) throw new Error('UART2 transmit queue exhausted');
            this.#tx.push(word & 255);
            this.#txBytes++;
            break;
        case CONF0:
            if (word & RX_RESET) this.#rx = [];
            if (word & TX_RESET) this.#tx = [];
            this.#regs[CONF0 / 4] = word;
            break;
        case INT_RAW:
        case INT_STATUS:
        case INT_CLEAR:
        case STATUS:
            // FIFO conditions are evaluated live, including after an ISR
            // acknowledges them. An empty RX FIFO drops its interrupt level.
            break;
        default:
            this.#regs[offset >>> 2] = word;
        }
    }

    #entropyAvailable() {
        return globalThis.isSecureContext && typeof globalThis.crypto?.getRandomValues === 'function';
    }

    stats() {
        return { uart2RxBytes: this.#rxBytes, uart2TxBytes: this.#txBytes,
                 entropyAvailable: this.#entropyAvailable(), entropyWords: this.#randomWords };
    }

    imports = {
        uart2_read: offset => this.#read(offset),
        uart2_write: (offset, word) => this.#write(offset, word),
        uart2_irq: () => Number((this.#interrupts() & this.#regs[INT_ENABLE / 4]) !== 0),
        entropy_available: () => Number(this.#entropyAvailable()),
        random_word: () => {
            globalThis.crypto.getRandomValues(this.#entropy);
            this.#randomWords++;
            return this.#entropy[0];
        },
    };
}

export const browserDevices = new BrowserDevices();
