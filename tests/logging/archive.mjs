const capacity = 32;

function writeAll(file, bytes, offset) {
    while (bytes.length) {
        const written = file.write(bytes, { at: offset });
        if (!written) throw new Error('Diagnostic file write made no progress');
        bytes = bytes.subarray(written);
        offset += written;
    }
}

// Run in a dedicated worker. Enqueue copies the borrowed Wasm bytes and
// schedules a drain. File operations run after control has returned to JS.
export async function openArchive(directory, limit) {
    if (!Number.isSafeInteger(limit) || limit <= 0) throw new RangeError('Invalid log file limit');
    const handles = [];
    try {
        for (const name of ['system.log', 'system.log.1']) {
            const file = await directory.getFileHandle(name, { create: true });
            const access = await file.createSyncAccessHandle();
            handles.push(access);
            if (access.getSize() > limit) throw new RangeError('Existing log exceeds its limit');
        }
    } catch (error) {
        for (const handle of handles) handle.close();
        throw error;
    }
    const [active, previous] = handles;
    const scratch = new Uint8Array(256);
    const pending = [];
    let size = active.getSize();
    let dropped = 0;
    let scheduled = false;
    let closed = false;
    let failure;

    function rotate() {
        previous.truncate(0);
        for (let offset = 0; offset < size;) {
            const chunk = scratch.subarray(0, Math.min(scratch.length, size - offset));
            const count = active.read(chunk, { at: offset });
            if (!count) throw new Error('Diagnostic file ended during rotation');
            writeAll(previous, chunk.subarray(0, count), offset);
            offset += count;
        }
        previous.flush();
        active.truncate(0);
        size = 0;
    }

    function drain() {
        for (let line of pending.splice(0)) {
            while (line.length) {
                if (size === limit) rotate();
                const count = Math.min(line.length, limit - size);
                writeAll(active, line.subarray(0, count), size);
                size += count;
                line = line.subarray(count);
            }
        }
        active.flush();
    }

    return {
        enqueue(bytes) {
            if (closed || failure || pending.length === capacity || bytes.length > 256) {
                dropped++;
                return;
            }
            pending.push(bytes.slice());
            if (!scheduled) {
                scheduled = true;
                queueMicrotask(() => {
                    scheduled = false;
                    if (closed || failure) return;
                    try {
                        drain();
                    } catch (error) {
                        failure = error;
                    }
                });
            }
        },
        close() {
            if (closed) throw new Error('Diagnostic archive is closed');
            closed = true;
            try {
                if (failure) throw failure;
                drain();
                return dropped;
            } finally {
                for (const handle of handles) handle.close();
            }
        },
    };
}
