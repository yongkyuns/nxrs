import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { copyFile, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { serve } from '../browser/tracker-demo/serve.mjs';

const [wasm, report, executablePath] = process.argv.slice(2);
assert(wasm && report, 'usage: node verify-web.mjs QUALIFICATION.wasm REPORT.json [CHROMIUM]');
const { chromium } = createRequire(import.meta.url)('playwright');
const root = await mkdtemp(join(tmpdir(), 'nxrs-log-opfs-'));
let server;
let browser;

function askWorker(page, command) {
    return page.evaluate(command => new Promise((resolve, reject) => {
        const worker = new Worker('./worker.mjs', { type: 'module' });
        const timeout = setTimeout(() => {
            worker.terminate();
            reject(new Error('Diagnostic worker timed out'));
        }, 10000);
        worker.onmessage = ({ data }) => {
            clearTimeout(timeout);
            worker.terminate();
            if (data.error) reject(new Error(data.error));
            else resolve(data);
        };
        worker.onerror = (error) => {
            clearTimeout(timeout);
            worker.terminate();
            reject(new Error(error.message));
        };
        worker.postMessage(command);
    }), command);
}

try {
    await Promise.all([
        copyFile(wasm, join(root, 'qualification.wasm')),
        copyFile(new URL('./worker.mjs', import.meta.url), join(root, 'worker.mjs')),
        copyFile(new URL('../../platform/logging/web/archive.mjs', import.meta.url),
                 join(root, 'archive.mjs')),
        writeFile(join(root, 'index.html'), '<!doctype html><title>NXRS logging qualification</title>'),
    ]);
    const site = await serve(root);
    server = site.server;
    browser = await chromium.launch({ executablePath });
    const page = await browser.newPage();
    await page.goto(site.url);
    const { dropped } = await askWorker(page, 'write');
    assert.equal(dropped, 8);

    // A fresh worker after reload reads the persisted files from the same origin.
    await page.reload();
    const { files } = await askWorker(page, 'read');
    assert.equal(files[0].size, 256);
    assert(files[1].size > 0 && files[1].size <= 256);
    const retained = files.map(file => file.text).join('');
    assert(retained.includes('[INFO navigation::observations] configured 1 IMU channels, GNSS disabled\n'));
    assert(retained.endsWith('[INFO navigation::observations] configured 1 IMU channels, GNSS enabled\n'));
    await writeFile(report, JSON.stringify({ chromium: browser.version(), dropped, files }, null, 2) + '\n');
    console.log('PASS: Rust service diagnostics persisted and rotated in Chromium OPFS across reload');
} finally {
    await browser?.close();
    server?.close();
    await rm(root, { recursive: true, force: true });
}
