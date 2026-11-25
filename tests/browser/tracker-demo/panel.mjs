export const width = 320;
export const height = 240;

export function wordAt(machine, address) {
    const bytes = machine.read_memory(address, 4);
    return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0, true);
}

function readBytes(machine, address, pixels) {
    const bytes = new Uint8Array(pixels);
    for (let offset = 0; offset < bytes.length; offset += 4096) {
        const size = Math.min(4096, bytes.length - offset);
        bytes.set(machine.read_memory(address + offset, size), offset);
    }
    return bytes;
}

function rgb565(color) {
    return [((color >> 11) * 255) / 31, (((color >> 5) & 63) * 255) / 63,
            ((color & 31) * 255) / 31, 255];
}

export function readPanel(machine, symbols) {
    const side = 170;
    const mapX = 75;
    const mapY = 38;
    const center = symbols.tracker_panel_center
        ? readBytes(machine, symbols.tracker_panel_center, side * side) : null;
    const pixels = readBytes(machine, symbols.tracker_pixels, (width * height - (center?.length ?? 0)) / 2);
    const readColors = (address, count) => {
        const words = machine.read_memory(address, count * 2);
        return Array.from({ length: count }, (_, index) => {
            return rgb565(words[index * 2] | (words[index * 2 + 1] << 8));
        });
    };
    const rgba = new Uint8ClampedArray(width * height * 4);
    const palette = readColors(symbols.tracker_colors, 16);
    const mapColors = center && readColors(symbols.tracker_center_colors, 64);
    let outer = 0;
    let inner = 0;
    for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
            const offset = (y * width + x) * 4;
            if (center && y >= mapY && y < mapY + side && x >= mapX && x < mapX + side) {
                rgba.set(mapColors[center[inner++]], offset);
            } else {
                const index = (pixels[outer >> 1] >> ((outer & 1) * 4)) & 15;
                rgba.set(palette[index], offset);
                outer++;
            }
        }
    }
    return rgba;
}
