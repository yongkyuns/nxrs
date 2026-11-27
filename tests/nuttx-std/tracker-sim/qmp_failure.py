"""Temporary, failure-only register capture for the frozen QEMU diagnostic run."""
import asyncio
import hashlib
import json
from pathlib import Path


TIMEOUT_SECONDS = 5
# Exact esp32s3.dram RAM mapping in the pinned emulator, not a guessed board range:
# espressif/qemu@7411393a2f61e6f0b05af00276d30780769e0839, hw/xtensa/esp32s3.c.
DRAM_BASE = 0x3FC80000
DRAM_BYTES = 0x170000


async def retain(qmp, image, destination, original_error):
    """Stop the failed guest, retain raw per-CPU registers, and never replace its error."""
    report = {
        'schema': 'nxrs-qemu-failure-registers-v1',
        'original_error': {'type': type(original_error).__name__, 'message': str(original_error)},
        'commands': [], 'events': [], 'registers': {}, 'images': {},
    }
    writer = None
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            reader, writer = await asyncio.open_unix_connection(qmp)
            report['greeting'] = json.loads(await reader.readline())
            if 'QMP' not in report['greeting']:
                raise ValueError('missing QMP greeting')

            async def execute(command, **arguments):
                request = {'execute': command, 'id': len(report['commands'])}
                if arguments:
                    request['arguments'] = arguments
                item = {'request': request}
                report['commands'].append(item)
                writer.write(json.dumps(request).encode() + b'\n')
                await writer.drain()
                while True:
                    response = json.loads(await reader.readline())
                    if 'event' in response:
                        report['events'].append(response)
                        if len(report['events']) > 64:
                            raise ValueError('excessive QMP events')
                        continue
                    item['response'] = response
                    if response.get('id') != request['id'] or 'return' not in response:
                        raise ValueError(f'QMP {command} failed: {response}')
                    return response['return']

            await execute('qmp_capabilities')
            await execute('stop')
            report['stopped_status'] = await execute('query-status')
            if report['stopped_status'].get('running') is not False:
                raise ValueError('QEMU did not stop before the register snapshot')
            report['cpus'] = await execute('query-cpus-fast')
            for cpu in report['cpus']:
                index = cpu['cpu-index']
                report['registers'][str(index)] = await execute(
                    'human-monitor-command', **{'command-line': 'info registers', 'cpu-index': index})
            dump = Path(destination).with_suffix('.dram.bin').resolve()
            dump.parent.mkdir(parents=True, exist_ok=True)
            report['dram'] = {'address': DRAM_BASE, 'bytes': DRAM_BYTES, 'path': str(dump)}
            await execute('pmemsave', val=DRAM_BASE, size=DRAM_BYTES, filename=str(dump))
            if dump.stat().st_size != DRAM_BYTES:
                raise ValueError('incomplete QEMU DRAM snapshot')
            report['dram']['sha256'] = hashlib.sha256(dump.read_bytes()).hexdigest()
            # Do not resume: the existing failure cleanup immediately kills this guest.
    except Exception as error:
        report['diagnostic_error'] = {'type': type(error).__name__, 'message': str(error)}
        original_error.add_note(f'QMP register capture incomplete: {error}')
    finally:
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 0.25)
            except Exception as error:
                report['close_error'] = str(error)
        try:
            for path in (image, image.with_name('nuttx'), image.with_name('nuttx.map'),
                         image.with_name('.config')):
                if path.is_file():
                    report['images'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2) + '\n')
        except Exception as error:
            original_error.add_note(f'QMP diagnostic evidence could not be saved: {error}')
