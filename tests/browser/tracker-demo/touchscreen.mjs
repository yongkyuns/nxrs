import { width, height, wordAt } from './panel.mjs';

const touchDown = 0;
const touchUp = 1;
const touchMove = 2;
const queueLimit = 32;

// Send one FT6336 contact state at a time. The guest polls its own model and
// gets both the press and the release even when the browser delivers them in
// the same emulation batch.
export class Touchscreen {
    #machine;
    #symbols;
    #queue = [];
    #active = false;
    #postedAt = null;

    constructor(machine, symbols) {
        this.#machine = machine;
        this.#symbols = symbols;
    }

    enqueue({ contact, x, y }) {
        if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || x >= width || y < 0 || y >= height) return;
        if (contact === touchDown) {
            if (this.#active || this.#queue.length > queueLimit - 2) return;
            this.#active = true;
        } else if (!this.#active || (contact !== touchMove && contact !== touchUp)) return;

        const event = { contact, word: (x | (y << 16) | (contact << 30)) >>> 0 };
        if (contact === touchMove && this.#queue.at(-1)?.contact === touchMove) {
            this.#queue[this.#queue.length - 1] = event;
        } else if (contact !== touchMove || this.#queue.length < queueLimit - 1) {
            this.#queue.push(event);
        }
        if (contact === touchUp) this.#active = false;
    }

    service() {
        if (!this.#queue.length) return;
        const reads = wordAt(this.#machine, this.#symbols.tracker_touch_reads);
        if (this.#postedAt !== null && ((reads - this.#postedAt) >>> 0) < 2) return;
        const { word } = this.#queue.shift();
        if (!this.#machine.write_touch_word(this.#symbols.tracker_touch_input, word)) {
            throw new Error('The emulated touch mailbox is unavailable');
        }
        this.#postedAt = reads;
    }
}
