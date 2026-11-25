const element = id => document.getElementById(id);
const panel = element('panel');
const paint = panel.getContext('2d');
const status = element('status');
const transport = element('transport');
const serial = element('serial');
const command = element('serial-command');
const send = element('serial-send');
const readings = ['imu1', 'imu2', 'gnss'];
let worker;
let freshness;
let samples;
let pointerId = null;
let lastPoint;
let serialOutput = '';

document.body.classList.toggle('embedded', window.self !== window.top);

function state(phase, message) {
    status.dataset.phase = phase;
    status.textContent = message;
    panel.dataset.interactive = ['ready', 'pairing', 'paired'].includes(phase);
    command.disabled = send.disabled = phase === 'booting' || phase === 'error';
    if (phase === 'pairing') status.dataset.qrScanned = 'true';
}

function renderSerial(output) {
    const follow = serial.scrollHeight - serial.clientHeight - serial.scrollTop <= 2;
    serialOutput = (serialOutput + output).slice(-24576);
    // Keep the raw tail so a VT100 sequence split across batches is still removed.
    serial.textContent = serialOutput.replace(/\x1b\[[0-?]*[ -/]*[@-~]|\x1b(?:\[[0-?]*[ -/]*)?$|\r/g, '');
    if (follow) serial.scrollTop = serial.scrollHeight;
}

function sample(source, id, value) {
    element(`${id}-state`).textContent = source.state === 'live' ? 'Live' : source.state;
    element(id).textContent = source.state === 'live' ? value(source) : '—';
    if (source.state === 'live') element(id).dataset.sequence = source.seq;
    else delete element(id).dataset.sequence;
}

function renderTelemetry(data) {
    samples++;
    element('count').textContent = `${samples} ${samples === 1 ? 'sample' : 'samples'}`;
    element('count').dataset.samples = samples;
    const acceleration = source => `${source.accel_mps2.map(number => number.toFixed(2)).join(' / ')} m/s²`;
    sample(data.imu[0], 'imu1', acceleration);
    sample(data.imu[1], 'imu2', acceleration);
    sample(data.gnss, 'gnss', source => `${source.lat_deg.toFixed(6)}°, ${source.lon_deg.toFixed(6)}°`);
    transport.textContent = 'Authenticated · TLS 1.2 PSK over GATT';
    clearTimeout(freshness);
    freshness = setTimeout(() => {
        for (const id of readings) sample({ state: 'Stale' }, id);
        transport.textContent = 'Waiting for fresh BLE telemetry';
    }, 2500);
}

function failed(message) {
    worker?.terminate();
    clearTimeout(freshness);
    for (const id of readings) sample({ state: '' }, id);
    transport.textContent = 'Link unavailable';
    state('error', message);
}

function start() {
    worker?.terminate();
    clearTimeout(freshness);
    pointerId = null;
    delete status.dataset.qrScanned;
    paint.clearRect(0, 0, panel.width, panel.height);
    serialOutput = '';
    serial.textContent = '';
    command.value = '';
    samples = 0;
    element('count').textContent = '0 samples';
    element('count').dataset.samples = 0;
    element('cores').textContent = 'Starting';
    element('cores').dataset.count = 0;
    for (const id of readings) sample({ state: '' }, id);
    transport.textContent = 'Waiting for pairing';
    state('booting', 'Booting NuttX…');

    worker = new Worker(new URL('./worker.mjs', import.meta.url), { type: 'module' });
    worker.onerror = event => failed(event.message);
    worker.onmessage = ({ data }) => {
        switch (data.type) {
        case 'state': {
            const message = {
                tracker: 'Starting Motion Tracker…', ready: 'Touchscreen ready',
                pairing: 'Guest QR scanned. Pairing over BLE…', paired: 'Paired over BLE',
            }[data.phase];
            state(data.phase, message);
            break;
        }
        case 'frame':
            paint.putImageData(new ImageData(new Uint8ClampedArray(data.buffer), panel.width, panel.height), 0, 0);
            break;
        case 'device': {
            const core = element('cores');
            core.textContent = `${data.cores} ${data.cores === 1 ? 'core' : 'cores'} active`;
            core.dataset.count = data.cores;
            core.dataset.h4Tx = data.h4Tx;
            core.dataset.h4Rx = data.h4Rx;
            break;
        }
        case 'console': renderSerial(data.output); break;
        case 'sample': renderTelemetry(data.sample); break;
        case 'error': failed(data.message); break;
        }
    };
}

function locationOnPanel(event) {
    const bounds = panel.getBoundingClientRect();
    return {
        x: Math.max(0, Math.min(panel.width - 1, Math.floor(
            (event.clientX - bounds.left - panel.clientLeft) * panel.width / panel.clientWidth))),
        y: Math.max(0, Math.min(panel.height - 1, Math.floor(
            (event.clientY - bounds.top - panel.clientTop) * panel.height / panel.clientHeight))),
    };
}

function contact(kind, point) {
    worker.postMessage({ type: 'touch', contact: kind, ...point });
}

panel.addEventListener('pointerdown', event => {
    if (pointerId !== null || !event.isPrimary || event.button !== 0 || panel.dataset.interactive !== 'true') return;
    event.preventDefault();
    pointerId = event.pointerId;
    panel.setPointerCapture(pointerId);
    lastPoint = locationOnPanel(event);
    contact(0, lastPoint);
});
panel.addEventListener('pointermove', event => {
    if (event.pointerId !== pointerId) return;
    lastPoint = locationOnPanel(event);
    contact(2, lastPoint);
});
function release(event, point) {
    if (event.pointerId !== pointerId) return;
    pointerId = null;
    contact(1, point);
}
panel.addEventListener('pointerup', event => release(event, locationOnPanel(event)));
panel.addEventListener('pointercancel', event => release(event, lastPoint));
panel.addEventListener('lostpointercapture', event => release(event, lastPoint));

element('restart').addEventListener('click', start);
serial.closest('details').addEventListener('toggle', event => {
    if (event.currentTarget.open) serial.scrollTop = serial.scrollHeight;
});
element('serial-form').addEventListener('submit', event => {
    event.preventDefault();
    if (command.disabled || !command.value.trim()) return;
    serial.scrollTop = serial.scrollHeight;
    worker.postMessage({ type: 'command', command: command.value });
    command.value = '';
    command.focus();
});
start();
