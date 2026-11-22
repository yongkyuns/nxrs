export const width = 320;
export const height = 240;

export function wordAt(machine, address) {
    const bytes = machine.read_memory(address, 4);
    return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0, true);
}

export function readPanel(machine, symbols) {
    const pixels = new Uint8Array(width * height / 2);
    for (let offset = 0; offset < pixels.length; offset += 4096) {
        const size = Math.min(4096, pixels.length - offset);
        pixels.set(machine.read_memory(symbols.tracker_pixels + offset, size), offset);
    }
    const colors = machine.read_memory(symbols.tracker_colors, 32);
    const rgba = new Uint8ClampedArray(width * height * 4);
    const palette = Array.from({ length: 16 }, (_, index) => {
        const color = colors[index * 2] | (colors[index * 2 + 1] << 8);
        return [((color >> 11) * 255) / 31, (((color >> 5) & 63) * 255) / 63,
                ((color & 31) * 255) / 31, 255];
    });
    for (let i = 0; i < width * height; i++) {
        const index = (pixels[i >> 1] >> ((i & 1) * 4)) & 15;
        rgba.set(palette[index], i * 4);
    }
    return rgba;
}
