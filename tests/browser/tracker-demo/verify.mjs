import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { serve } from './serve.mjs';

const [source, reportPath, executablePath, prefix] = process.argv.slice(2);
assert(source && reportPath, 'usage: node verify.mjs DIRECTORY_OR_HTTPS_SITE REPORT.json [CHROMIUM_BINARY [SITE_PREFIX]]');
const { chromium } = createRequire(import.meta.url)('playwright');
const published = source.startsWith('https://');
const tutorial = published || Boolean(prefix);
const { server, url } = published
    ? { url: new URL(source.endsWith('/') ? source : source + '/').href }
    : await serve(source, 0, prefix);
const demoUrl = tutorial ? `${url}demos/motion-tracker/` : url;
let browser;

async function launch({ wrongKey = false } = {}) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 960 } });
    const requests = [];
    const invalid = [];
    await context.route('**/*', async route => {
        const request = route.request();
        requests.push(request.url());
        if (request.method() !== 'GET' || !request.url().startsWith(url)) {
            invalid.push(request.url());
            await route.abort();
        } else if (wrongKey && request.url() === `${demoUrl}jsQR.js`) {
            // Keep the actual pixel decoder and corrupt only the decoded PSK.
            const source = await (await route.fetch()).text();
            await route.fulfill({ contentType: 'text/javascript', body: `${source}
                const decodeQr = globalThis.jsQR;
                globalThis.jsQR = (...args) => {
                    const code = decodeQr(...args);
                    if (code?.data.startsWith('NXRS1:')) {
                        const n = Number.parseInt(code.data[6], 16) ^ 1;
                        code.data = code.data.slice(0, 6) + n.toString(16).toUpperCase() + code.data.slice(7);
                    }
                    return code;
                };` });
        } else await route.continue();
    });
    const page = await context.newPage();
    page.on('websocket', socket => invalid.push(socket.url()));
    page.on('pageerror', error => invalid.push(error.message));
    const response = await page.goto(tutorial ? `${url}guide/motion-tracker/` : url);
    assert(response?.ok(), `site returned HTTP ${response?.status()}`);
    let demo = page;
    if (tutorial) {
        assert(!requests.some(request => request.startsWith(demoUrl)), 'demo loaded before activation');
        await page.locator('.tracker-demo > summary').click();
        const iframe = await page.locator('.tracker-demo iframe').elementHandle();
        demo = await iframe.contentFrame();
        assert(demo, 'missing tracker demo frame');
        await demo.waitForURL(demoUrl, { waitUntil: 'domcontentloaded' });
    }
    return { context, page, demo, requests, invalid };
}

async function waitPhase(page, expected) {
    await page.waitForFunction(phase => {
        const state = document.getElementById('status').dataset.phase;
        return state === phase || state === 'error';
    }, expected, { timeout: 30000 });
    const status = page.locator('#status');
    assert.equal(await status.getAttribute('data-phase'), expected, await status.textContent());
}

async function capture(page, last) {
    const handle = await page.waitForFunction(previous => {
        if (document.getElementById('status').dataset.phase === 'error') {
            throw new Error(document.getElementById('status').textContent);
        }
        const sequences = ['imu1', 'imu2', 'gnss'].map(id => Number(document.getElementById(id).dataset.sequence));
        if (!sequences.every((sequence, i) => Number.isFinite(sequence) && sequence > previous[i])) return null;
        const core = document.getElementById('cores').dataset;
        return {
            sequences, cores: Number(core.count), h4Rx: Number(core.h4Rx), h4Tx: Number(core.h4Tx),
            imu1: document.getElementById('imu1').textContent,
            imu2: document.getElementById('imu2').textContent,
            gnss: document.getElementById('gnss').textContent,
        };
    }, last, { timeout: 12000 });
    const sample = await handle.jsonValue();
    assert.equal(sample.cores, 2);
    assert(sample.h4Rx > 0 && sample.h4Tx > 0);
    assert.match(sample.imu1, /^-9\.81 \/ 0\.00 \/ 9\.81/);
    assert.match(sample.imu2, /^9\.81 \/ 0\.00 \/ 9\.81/);
    assert.equal(sample.gnss, '48.117300°, -11.516667°');
    return sample;
}

async function panelPoint(demo, x, y) {
    const canvas = demo.locator('#panel');
    await canvas.scrollIntoViewIfNeeded();
    const bounds = await canvas.boundingBox();
    const metrics = await canvas.evaluate(element => [element.clientLeft, element.clientTop,
                                                      element.clientWidth, element.clientHeight]);
    assert(bounds && metrics[2] && metrics[3], 'guest touchscreen not visible');
    return [bounds.x + metrics[0] + (x + 0.5) * metrics[2] / 320,
            bounds.y + metrics[1] + (y + 0.5) * metrics[3] / 240];
}

async function expectPixel(demo, x, y, rgb) {
    const color = [(rgb >> 11) * 255 / 31, ((rgb >> 5) & 63) * 255 / 63, (rgb & 31) * 255 / 31].map(Math.round);
    await demo.waitForFunction(({ x, y, color }) => {
        const panel = document.getElementById('panel');
        const pixel = panel.getContext('2d').getImageData(x, y, 1, 1).data;
        return color.every((component, index) => component === pixel[index]);
    }, { x, y, color }, { timeout: 6000 });
}

async function revealTabs(page, demo, selected) {
    const start = await panelPoint(demo, 160, 6);
    const end = await panelPoint(demo, 160, 65);
    await page.mouse.move(...start);
    await page.mouse.down();
    await page.mouse.move(...end, { steps: 9 });
    await page.mouse.up();
    await expectPixel(demo, selected, 33, 0x57f4);
}

async function tap(page, demo, x, y) {
    await page.mouse.click(...await panelPoint(demo, x, y));
}

async function startGuestPairing(page, demo) {
    await waitPhase(demo, 'ready');
    assert.equal(await demo.locator('#count').getAttribute('data-samples'), '0');
    assert.equal(await demo.locator('#cores').getAttribute('data-count'), '2');
    assert.equal(await demo.locator('#status').getAttribute('data-qr-scanned'), null);

    await revealTabs(page, demo, 40);
    await tap(page, demo, 118, 20);
    await expectPixel(demo, 0, 0, 0x0883);
    await revealTabs(page, demo, 118);
    await tap(page, demo, 195, 20);
    await expectPixel(demo, 0, 0, 0x0883);
    await revealTabs(page, demo, 195);
    await tap(page, demo, 275, 20);
}

async function checkConsole(demo, submit) {
    await demo.getByText('Serial console', { exact: true }).click();
    const command = demo.getByLabel('NSH command');
    await command.fill('echo NXRS_UART0_OK');
    if (submit === 'Enter') await command.press('Enter');
    else await demo.getByRole('button', { name: 'Send' }).click();
    await demo.waitForFunction(() => document.getElementById('serial').textContent.includes('\nNXRS_UART0_OK\nnsh> '),
                               null, { timeout: 6000 });
    assert(!/\x1b/.test(await demo.locator('#serial').textContent()));
    await demo.getByText('Serial console', { exact: true }).click();
}

async function checkPairing(page, demo, submit) {
    await startGuestPairing(page, demo);
    await waitPhase(demo, 'paired');
    assert.equal(await demo.locator('#status').getAttribute('data-qr-scanned'), 'true');
    const samples = [];
    let sequences = [-1, -1, -1];
    for (let i = 0; i < 3; i++) {
        if (i === 2) await checkConsole(demo, submit);
        const sample = await capture(demo, sequences);
        sequences = sample.sequences;
        samples.push(sample);
    }
    assert.equal(await demo.locator('#transport').textContent(), 'Authenticated · TLS 1.2 PSK over GATT');
    return samples;
}

try {
    browser = await chromium.launch({ executablePath });
    const { context, page, demo, requests, invalid } = await launch();
    const samples = await checkPairing(page, demo, 'Enter');
    assert.deepEqual(invalid, []);
    assert(requests.some(request => request.endsWith('/tls-client.wasm')));
    await page.screenshot({ path: reportPath.replace(/\.json$/, '') + '.png' });
    if (tutorial) await page.setViewportSize({ width: 390, height: 844 });
    await demo.getByRole('button', { name: 'Restart' }).click();
    const restarted = await checkPairing(page, demo, 'Send');
    assert.deepEqual(invalid, []);
    if (tutorial) {
        assert(await demo.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.locator('.tracker-demo > summary').click();
        await demo.waitForURL('about:blank');
        assert.equal(await page.locator('.tracker-demo iframe').getAttribute('src'), null);
    }
    await context.close();

    let wrongKey;
    if (!published) {
        const negative = await launch({ wrongKey: true });
        await startGuestPairing(negative.page, negative.demo);
        await waitPhase(negative.demo, 'error');
        wrongKey = await negative.demo.locator('#status').textContent();
        assert.match(wrongKey, /TLS handshake failed|BLE disconnected|ATT request (10|18) failed with status 14/);
        assert.equal(await negative.demo.locator('#count').getAttribute('data-samples'), '0');
        assert.deepEqual(negative.invalid, []);
        await negative.context.close();
    }

    const assets = [...new Set(requests)].map(request => new URL(request).pathname).sort();
    const report = { chromium: browser.version(), tutorial, site: published ? url : undefined,
                     tabs: ['IMU 1', 'IMU 2', 'GNSS'],
                     samples, restarted, nsh: { command: 'echo NXRS_UART0_OK', output: 'NXRS_UART0_OK' },
                     wrongKey, assets };
    await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
    console.log(`Chromium ${report.chromium}, both guest cores active`);
    console.log('PASS: guest touch drags and taps switched tabs and started QR pairing');
    console.log('PASS: three advancing TLS-authenticated BLE snapshots before and after restart');
    console.log('PASS: NSH executed browser commands and returned a fresh prompt during live telemetry');
    if (wrongKey) console.log(`PASS: corrupted QR key rejected (${wrongKey}), zero samples`);
    console.log(`PASS: ${assets.length} same-origin static assets, no external requests or WebSockets`);
} finally {
    await browser?.close();
    server?.close();
}
