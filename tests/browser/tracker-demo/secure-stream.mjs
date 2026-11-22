import createTls from './tls-client.mjs';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

export async function streamTracker(att, key, onAuthenticated, onSample) {
    let incoming = [];
    let outgoing = [];
    const tls = await createTls({
        writeEncrypted(bytes) {
            if (outgoing.length + bytes.length > 16384) throw new Error('TLS send buffer exhausted');
            outgoing.push(...bytes);
            return bytes.length;
        },
        readEncrypted(out) {
            const count = Math.min(out.length, incoming.length);
            out.set(incoming.splice(0, count));
            return count;
        },
    });
    const result = (value, operation) => {
        if (value < 0) throw new Error(`TLS ${operation} failed (-0x${(-value).toString(16)})`);
        return value;
    };
    try {
        tls.HEAPU8.set(key, tls._peer_key());
        result(tls._peer_start(), 'setup');
        key.fill(0);
        const flush = async () => {
            if (!outgoing.length) return;
            const bytes = Uint8Array.from(outgoing);
            outgoing = [];
            await att.write(bytes);
        };
        const receive = async () => {
            const data = await att.read();
            if (incoming.length + data.length > 16384) throw new Error('TLS receive buffer exhausted');
            incoming.push(...data);
            if (!data.length) await delay(12);
        };

        const deadline = performance.now() + 10000;
        for (;;) {
            const done = result(tls._peer_handshake(), 'handshake');
            await flush();
            if (done === 1) break;
            if (performance.now() >= deadline) throw new Error('TLS handshake timed out');
            await receive();
        }

        onAuthenticated();
        const decoder = new TextDecoder('utf-8', { fatal: true });
        let line = '';
        for (;;) {
            const size = result(tls._peer_read(), 'read');
            await flush();
            if (!size) {
                await receive();
                continue;
            }
            const start = tls._peer_plaintext();
            line += decoder.decode(tls.HEAPU8.subarray(start, start + size), { stream: true });
            let end;
            while ((end = line.indexOf('\n')) !== -1) {
                const sample = JSON.parse(line.slice(0, end));
                line = line.slice(end + 1);
                if (sample.v !== 1 || !Array.isArray(sample.imu) || sample.imu.length !== 2 || !sample.gnss) {
                    throw new Error('Invalid tracker telemetry');
                }
                onSample(sample);
            }
            if (line.length > 768) throw new Error('Tracker telemetry line exceeded its limit');
        }
    } finally {
        key.fill(0);
        tls._peer_close();
        incoming = outgoing = [];
    }
}
