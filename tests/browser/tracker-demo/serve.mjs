import { createServer } from 'node:http';
import { createReadStream } from 'node:fs';
import { realpath, stat } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const types = {
    '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.wasm': 'application/wasm', '.bin': 'application/octet-stream', '.txt': 'text/plain; charset=utf-8',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json',
};

export async function serve(directory, port = 0, prefix = '/') {
    if (!/^\/(?:[a-zA-Z0-9_-]+\/)*$/.test(prefix)) throw new Error('Invalid static site prefix');
    const root = await realpath(directory);
    const server = createServer(async (request, response) => {
        if (request.method !== 'GET' && request.method !== 'HEAD') {
            response.writeHead(405, { Allow: 'GET, HEAD' }).end();
            return;
        }
        try {
            const { pathname } = new URL(request.url, 'http://127.0.0.1');
            if (!pathname.startsWith(prefix)) {
                response.writeHead(404).end();
                return;
            }
            const relative = decodeURIComponent(pathname.slice(prefix.length));
            const path = await realpath(resolve(root, `./${relative}${pathname.endsWith('/') ? 'index.html' : ''}`));
            const type = types[extname(path)];
            if (!path.startsWith(root + sep) || !type || !(await stat(path)).isFile()) {
                response.writeHead(404).end();
                return;
            }
            response.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
            if (request.method === 'HEAD') response.end();
            else createReadStream(path).on('error', error => response.destroy(error)).pipe(response);
        } catch {
            response.writeHead(404).end();
        }
    });
    await new Promise((accept, reject) => {
        server.once('error', reject);
        server.listen(port, '127.0.0.1', () => {
            server.removeListener('error', reject);
            accept();
        });
    });
    return { server, url: `http://127.0.0.1:${server.address().port}${prefix}` };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    if (!process.argv[2]) throw new Error('usage: node serve.mjs DIRECTORY [PORT [PREFIX]]');
    const { url } = await serve(process.argv[2], Number(process.argv[3] ?? 8080), process.argv[4]);
    console.log(`Motion Tracker demo at ${url}`);
}
