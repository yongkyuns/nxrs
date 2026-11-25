#!/usr/bin/env python3
"""Boot the two-core NuttX preemption witness in local Chromium."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from shutil import copyfile
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import sync_playwright


BOOT = """async () => {
    const { default: init, WasmEmulator } = await import('./esp_emu.js');
    await init({ module_or_path: new URL('./emulator.wasm', location.href) });
    const machine = new WasmEmulator('esp32s3');
    machine.load_default_rom();
    machine.set_boot_from_rom(true);
    const reply = await fetch('./nuttx.bin');
    if (!reply.ok) throw new Error(`Cannot read NuttX image (${reply.status})`);
    machine.load_firmware(new Uint8Array(await reply.arrayBuffer()));
    let output = '';
    const deadline = performance.now() + 20000;
    for (let batch = 0; batch < 4000 && performance.now() < deadline; batch++) {
        output += machine.run_until(250000);
        if (machine.faulted() || machine.needs_restart() || output.includes('nsh>') ||
            output.includes('Assertion failed')) break;
    }
    return { output, cores: machine.num_harts(), cycles: machine.cycles(),
             fault: machine.faulted() ? machine.fault_report() : '',
             restart: machine.needs_restart() };
}"""


def run(args):
    with TemporaryDirectory(prefix="esp-emu-preemption-") as directory:
        site = Path(directory)
        copyfile(args.glue, site / "esp_emu.js")
        browser_devices = args.glue.with_name("browser_devices.js")
        if browser_devices.is_file():
            copyfile(browser_devices, site / browser_devices.name)
        copyfile(args.wasm, site / "emulator.wasm")
        copyfile(args.firmware, site / "nuttx.bin")
        (site / "index.html").write_text("<!doctype html><title>NuttX SMP</title>\n")
        handler = partial(SimpleHTTPRequestHandler, directory=directory)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=args.browser)
                    page = browser.new_page()
                    origin = f"http://127.0.0.1:{server.server_port}/"
                    page.route("**/*", lambda route: route.continue_()
                               if route.request.url.startswith(origin) else route.abort())
                    page.goto(origin)
                    result = page.evaluate(BOOT)
                    print(f"Chromium {browser.version}, {result['cores']} guest cores, "
                          f"{result['cycles']} guest cycles", flush=True)
                    browser.close()
            finally:
                server.shutdown()
                thread.join()

    output = result["output"].replace("\r", "")
    print(output, end="\n")
    if result["fault"] or result["restart"]:
        raise RuntimeError(result["fault"] or "ESP32-S3 requested a restart")
    if result["cores"] != 2 or "nsh>" not in output:
        raise RuntimeError("NuttX SMP did not reach NuttShell with both guest cores active")
    for kind, cpu in [("TIMER", 0), ("IPI", 1)]:
        expected = rf"^PREEMPT_{kind} PASS cpu={cpu} irq_cpu={cpu} progress=[1-9]\d* irq=1 status=0$"
        if len(re.findall(expected, output, re.M)) != 1:
            raise RuntimeError(f"missing guest {kind} preemption proof")
    if re.findall(r"^PREEMPT_SMP (\w+)$", output, re.M) != ["PASS"]:
        raise RuntimeError("missing guest SMP completion")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glue", type=Path, required=True, help="released esp_emu.js")
    parser.add_argument("--wasm", type=Path, required=True, help="patched ESP-EMU module")
    parser.add_argument("--firmware", type=Path, required=True, help="NuttX merged flash image")
    parser.add_argument("--browser", help="local Chromium or Chrome executable")
    run(parser.parse_args())
