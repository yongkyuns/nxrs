// Qualify the headless tracker, including the independent archive when configured.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const [image, reportPath, qemu = 'qemu-system-xtensa'] = process.argv.slice(2);
assert(image && reportPath, 'usage: node headless.mjs NUTTX.MERGED.BIN REPORT.json [QEMU]');
const nuttx = dirname(image);
const [config, receipt] = await Promise.all([
    readFile(join(nuttx, '.config'), 'utf8'),
    readFile(join(dirname(nuttx), 'provider-selection.json'), 'utf8').then(JSON.parse),
]);
assert.match(config, /^CONFIG_I2C_DRIVER=y$/m);
assert.doesNotMatch(config, /^CONFIG_(?:ESP32S3_UART[12]|INPUT|SPI|MTD|NET_BLUETOOTH)=y$/m);
assert.deepEqual(receipt.providers.map(provider => provider.capability), ['imu']);
const devices = Object.keys(receipt['resolved-composition'].graph.devices);
assert.equal(devices.length, 1);
const hasArchive = receipt.platform === 'esp32s3-qemu-tracker-logging';
if (hasArchive) {
    assert.match(config, /^CONFIG_RAMLOG_SYSLOG=y$/m);
    assert.match(config, /^# CONFIG_RAMLOG_NONBLOCKING is not set$/m);
    assert.match(config, /^CONFIG_FS_TMPFS=y$/m);
    assert.doesNotMatch(config, /^CONFIG_SYSLOG_FILE=y$/m);
}

const guest = spawn(qemu, [
    '-machine', 'esp32s3', '-smp', '2', '-display', 'none',
    '-serial', 'stdio', '-monitor', 'none', '-nic', 'none',
    '-no-reboot', '-snapshot', '-icount', 'shift=5,sleep=off',
    '-drive', `file=${image},if=mtd,format=raw`,
]);
const closed = once(guest, 'close');
closed.catch(() => {});
let output = '';
let failure;
guest.on('error', error => { failure = error; });
for (const stream of [guest.stdout, guest.stderr]) {
    stream.setEncoding('utf8');
    stream.on('data', data => { output += data; });
}

async function waitFor(text, offset = 0) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
        if (failure) throw failure;
        const position = output.indexOf(text, offset);
        if (position !== -1) return position;
        if (guest.exitCode !== null) break;
        await delay(20);
    }
    throw new Error(`missing ${text}\n${output.slice(-4000)}`);
}

async function runTracker() {
    const start = output.length;
    guest.stdin.write('motion_tracker --interval-ms 250 --frames 5 --stream-usb\n');
    const summary = await waitFor('motion-tracker: IMU 1 live', start);
    await waitFor('nsh>', summary);
    const samples = output.slice(start, summary).split(/\r?\n/)
        .filter(line => line.startsWith('{"v":2,')).map(line => JSON.parse(line));
    assert.equal(samples.length, 5);
    for (const sample of samples) {
        assert.equal(sample.imu.length, 1);
        assert.equal(Object.hasOwn(sample, 'gnss'), false);
    }
    const live = samples.map(sample => sample.imu[0]).filter(imu => imu.state === 'live');
    assert(live.length >= 3);
    assert(live.every((imu, i) => imu.accel_mps2[0] < -9 && (!i || imu.seq > live[i - 1].seq)));
    const status = output.length;
    guest.stdin.write('echo tracker-status:$?\n');
    const exited = await waitFor('tracker-status:0', status);
    await waitFor('nsh>', exited);
    return live;
}

async function shell(command) {
    const start = output.length;
    guest.stdin.write(command + '\n');
    const end = await waitFor('nsh>', start);
    return output.slice(start, end);
}

async function readArchive(path) {
    const dump = await shell(`hexdump ${path}`);
    const rows = [...dump.matchAll(/^[\da-f]{4}: ([\da-f ]{48})/gmi)];
    return Buffer.concat(rows.map(row => Buffer.from(row[1].replaceAll(' ', ''), 'hex')));
}

async function checkArchive() {
    await shell('usleep 10000');
    const sensorLog = await readArchive('/logs/system.log');
    assert.equal(sensorLog.toString().split('log witness: BMI270 configured').length - 1, 2);
    const serviceRecord = '[INFO navigation::observations] configured 1 IMU channels, GNSS disabled';
    assert.equal(sensorLog.toString().split(serviceRecord).length - 1, 2);

    const lines = Array.from({ length: 40 }, (_, index) => `route-${String(index).padStart(2, '0')}-${'x'.repeat(25)}`);
    for (const line of lines) await shell(`echo ${line} >/dev/log`);
    await shell('usleep 10000');
    const previous = await readArchive('/logs/system.log.1');
    const active = await readArchive('/logs/system.log');
    assert.equal(previous.length, 512);
    assert(active.length > 0 && active.length <= 512);
    const retained = Buffer.concat([previous, active]).toString().replaceAll('\r', '');
    assert(retained.endsWith(lines.slice(-12).join('\n') + '\n'), retained);
    assert(!retained.includes(lines[0]));
    return { bootMarker: true, sensorMarkers: 2, serviceMarkers: 2, emittedLines: lines.length,
             previousBytes: previous.length, activeBytes: active.length };
}

try {
    await waitFor('nsh>');
    if (hasArchive) {
        await shell('usleep 10000');
        const bootLog = await readArchive('/logs/system.log');
        assert(bootLog.includes('log witness: before tracker and archive task'), bootLog.toString());
    }
    const first = await runTracker();
    const restarted = await runTracker();
    const archive = hasArchive ? await checkArchive() : undefined;
    await writeFile(reportPath, JSON.stringify({ image, devices, first, restarted, archive }, null, 2) + '\n');
    console.log('PASS: one live IMU, no GNSS or display, and two clean session exits in one NuttX boot');
    if (archive) console.log('PASS: boot and driver syslog reached the archive, and repeated rotation retained the newest bytes');
} finally {
    guest.kill();
    await closed;
}
