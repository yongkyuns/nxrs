// Numerical reference only. Load the official public deployment's Node module;
// Rust production HALs do not depend on this process or its wire format.
const path = require('node:path');
const readline = require('node:readline');
const M = require(path.resolve(process.argv[2], 'node/edge-impulse-standalone.js'));
function plain(value, prototype) {
    const result = {};
    for (const key of Object.getOwnPropertyNames(prototype)) {
        if (typeof Object.getOwnPropertyDescriptor(prototype, key)?.get === 'function') result[key] = value[key];
    }
    return result;
}
M.onRuntimeInitialized = () => {
    try {
        const status = M.init();
        if (typeof status === 'number' && status !== 0) throw Error('WASM initialization');
        const properties = plain(M.get_properties(), M.emcc_classification_properties_t.prototype);
        if (properties.model_type !== 'constrained_object_detection' || properties.label_count !== 1 || properties.labels[0] !== 'hum') throw Error('unexpected model contract');
        for (const threshold of properties.thresholds) {
            const reply = M.set_threshold({ id: threshold.id, min_score: 0.5 });
            if (!reply.success) throw Error('threshold rejected');
        }
        console.log(JSON.stringify({ type: 'ready', project: plain(M.get_project(), M.emcc_classification_project_t.prototype), properties, node: process.version }));
        const input = readline.createInterface({ input: process.stdin });
        input.on('line', line => {
            let pointer, result;
            try {
                if (line.length > 65536) throw Error('input limit');
                const bytes = Buffer.from(JSON.parse(line).pixels, 'base64');
                if (bytes.length !== properties.input_features_count * 4) throw Error('input shape');
                const values = Float32Array.from(new Uint32Array(bytes.buffer, bytes.byteOffset, bytes.length / 4));
                pointer = M._malloc(values.byteLength);
                if (!pointer) throw Error('allocation');
                M.HEAPF32.set(values, pointer / 4);
                const start = process.hrtime.bigint();
                result = M.run_classifier(pointer, values.length, false);
                const engine_us = Number((process.hrtime.bigint() - start) / 1000n);
                if (result.result !== 0 || result.size() > 256) throw Error('inference result');
                const objects = [];
                for (let i = 0; i < result.size(); i++) {
                    const obj = result.get(i);
                    if (obj.value >= 0.5) objects.push({ label: obj.label, value: obj.value, x: obj.x, y: obj.y, width: obj.width, height: obj.height });
                    obj.delete();
                }
                console.log(JSON.stringify({ type: 'result', engine_us: Math.max(engine_us, 1), objects }));
            } catch (error) {
                console.log(JSON.stringify({ type: 'error', error: error.message }));
                process.exitCode = 1; input.close();
            } finally {
                if (result) result.delete();
                if (pointer) M._free(pointer);
            }
        });
    } catch (error) { console.log(JSON.stringify({ type: 'error', error: error.message })); process.exitCode = 1; }
};
