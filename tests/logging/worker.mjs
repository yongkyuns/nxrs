import { openArchive } from './archive.mjs';

onmessage = async ({ data }) => {
    try {
        const root = await navigator.storage.getDirectory();
        const directory = await root.getDirectoryHandle('diagnostics', { create: true });
        if (data === 'read') {
            const files = [];
            for (const name of ['system.log.1', 'system.log']) {
                const handle = await directory.getFileHandle(name);
                const file = await handle.getFile();
                files.push({ size: file.size, text: await file.text() });
            }
            postMessage({ files });
            return;
        }

        const archive = await openArchive(directory, 256);
        let dropped;
        try {
            let exports;
            const response = await fetch('./qualification.wasm');
            const { instance } = await WebAssembly.instantiateStreaming(response, {
                nxrs_logging: {
                    emit(ptr, len) {
                        archive.enqueue(new Uint8Array(exports.memory.buffer, ptr, len));
                    },
                },
            });
            exports = instance.exports;
            if (!exports.enable_diagnostics()) throw new Error('Rust logger installation failed');
            // Deliberately fill the bounded queue in one Wasm burst.
            for (let i = 0; i < 40; i++) exports.configure_observations(0);
            await Promise.resolve();
            exports.configure_observations(1);
        } finally {
            dropped = archive.close();
        }
        postMessage({ dropped });
    } catch (error) {
        postMessage({ error: String(error) });
    }
};
