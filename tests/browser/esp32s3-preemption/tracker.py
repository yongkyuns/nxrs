#!/usr/bin/env python3
"""Run the production NuttX tracker under the patched ESP-EMU browser module."""

import argparse
import asyncio
import base64
from contextlib import ExitStack
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from shutil import copyfile
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "nuttx-std" / "tracker-sim"))
from sensors import NeoM9n, check_live  # noqa: E402


class GnssWire:
    def __init__(self):
        self.pending = bytearray()

    def sendall(self, packet):
        self.pending.extend(packet)


def addresses(nm, elf):
    exports = {
        "tracker_pixels", "tracker_colors", "tracker_frame_sequence",
        "tracker_touch_reads", "tracker_touch_input", "tracker_model_error",
    }
    symbols = {}
    for line in subprocess.check_output([nm, str(elf)], text=True).splitlines():
        columns = line.split()
        if len(columns) == 3 and columns[2] in exports | {"tracker_panel_center", "tracker_center_colors"}:
            symbols[columns[2]] = int(columns[0], 16)
    if exports - symbols.keys():
        raise ValueError(f"missing dashboard fixture symbols: {exports - symbols.keys()}")
    return symbols


async def run(args, osm=None):
    symbols = addresses(args.nm, args.elf)
    if osm and not {"tracker_panel_center", "tracker_center_colors"} <= symbols.keys():
        raise ValueError("real tiles require the TRACKER_MAP_PANEL capture build")
    commands = args.command or [
        "motion_tracker --battery",
        "motion_tracker --interval-ms 250 --frames 8 --stream-usb",
    ]
    peer = None
    if args.ble:
        from tracker_ble import BlePeer

        peer = BlePeer(tiles=args.tiles, osm=osm, zoom=args.map_zoom or 18)
        if args.tiles:
            command = "motion_tracker --ble --frames 240 --stream-usb"
            if args.map_zoom is not None:
                command += f" --map-zoom {args.map_zoom}"
            commands = [command]
        else:
            commands = ["motion_tracker --ble --interval-ms 500 --frames 32 --stream-usb"]
    wire = GnssWire()
    receiver = NeoM9n(wire)
    fixes = 0

    def gnss_exchange(data):
        receiver.receive(bytes(data))
        reply = list(wire.pending)
        wire.pending.clear()
        return reply

    def gnss_fix():
        nonlocal fixes
        if receiver.rate != 1:
            return []
        fixes += 1
        return list(receiver.fix_packet(fixes * receiver.navigation[0], **(peer.fix if peer else {})))

    with TemporaryDirectory(prefix="esp-emu-tracker-") as directory:
        site = Path(directory)
        if args.patched_package:
            for file in ("esp_emu.js", "browser_devices.js"):
                copyfile(args.patched_package / file, site / file)
            wasm = args.patched_package / "esp_emu_bg.wasm"
        else:
            copyfile(args.bundle / "pkg" / "esp_emu.js", site / "esp_emu.js")
            wasm = args.wasm
        copyfile(args.bundle / "vendor" / "esp_cosim_pb.js", site / "esp_cosim_pb.js")
        copyfile(wasm, site / "emulator.wasm")
        copyfile(args.elf.with_name("nuttx.merged.bin"), site / "nuttx.bin")
        copyfile(HERE / "tracker.js", site / "tracker.js")
        for module in ("panel.mjs", "touchscreen.mjs"):
            copyfile(HERE.parent / "tracker-demo" / module, site / module)
        (site / "index.html").write_text("<!doctype html><title>Motion Tracker probe</title>\n")
        handler = partial(SimpleHTTPRequestHandler, directory=directory)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(executable_path=args.browser)
                    page = await browser.new_page()
                    if args.deny_entropy:
                        await page.add_init_script(
                            "Object.defineProperty(globalThis.crypto, 'getRandomValues', {value: undefined})"
                        )
                    origin = f"http://127.0.0.1:{server.server_port}/"
                    await page.route("**/*", lambda route: route.continue_()
                                     if route.request.url.startswith(origin) else route.abort())
                    await page.expose_function("gnssExchange", gnss_exchange)
                    await page.expose_function("gnssFix", gnss_fix)
                    ble_task = None
                    if peer:
                        await page.expose_function("h4Exchange", peer.exchange)
                        await page.expose_function("pairingPanel", peer.panel)
                        if args.tiles:
                            await page.expose_function("mapPanel", peer.map_panel)
                        ble_task = asyncio.create_task(peer.run(args.timeout))
                    try:
                        await page.goto(origin)
                        report = await page.evaluate(
                            "async config => (await import('./tracker.js')).probe(config)",
                            {"commands": commands, "symbols": symbols,
                             "timeoutMs": args.timeout * 1000, "ble": args.ble, "tiles": args.tiles},
                        )
                        if ble_task:
                            if not ble_task.done():
                                report["bleError"] = f"guest stopped while peer was {peer.phase}"
                            else:
                                try:
                                    report["ble"] = ble_task.result()
                                except Exception as error:
                                    report["bleError"] = f"{peer.phase}: {error!r}"
                    finally:
                        if ble_task:
                            ble_task.cancel()
                            await asyncio.gather(ble_task, return_exceptions=True)
                    report["chromium"] = browser.version
                    await browser.close()
            finally:
                server.shutdown()
                thread.join()

    image = report.pop("panelPng")
    if args.tiles:
        for name, png in peer.map_frames.items():
            args.report.with_name(f"{args.report.stem}-{name}.png").write_bytes(base64.b64decode(png))
        if "bleError" in report and peer.last_panel and peer.want_paint:
            name, samples = peer.want_paint
            report["pendingPanelPixels"] = samples
            args.report.with_name(f"{args.report.stem}-{name}-mismatch.png").write_bytes(
                base64.b64decode(peer.last_panel))
    report["gnssRateWrites"] = receiver.writes
    report["gnssFixesSent"] = fixes
    if image:
        args.report.with_suffix(".png").write_bytes(base64.b64decode(image))
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(report["boot"]["console"])
    for command in report["commands"]:
        print(command["console"])
    print(f"Chromium {report['chromium']}, {report['cores']} active guest cores, "
          f"UART1 TX {report['uartTx']['gnss']}, UART2 TX {report['uartTx']['hci']}, "
          f"panel sequence {report['panelSequence']}, touch polls {report['touchReads']}")
    if report["fault"] or report["restart"] or report["cores"] != 2 or not report["boot"]["prompt"]:
        raise RuntimeError(f"SMP boot failed. See {args.report}")
    if len(report["commands"]) != len(commands) or any(
        not command["prompt"] for command in report["commands"]
    ):
        raise RuntimeError(f"dashboard command did not return to NuttShell. See {args.report}")
    if args.ble:
        devices = report["devices"]
        if devices["uart2RxBytes"] == 0 or devices["uart2TxBytes"] == 0:
            raise RuntimeError(f"no bidirectional UART2 traffic. See {args.report}")
        if args.deny_entropy:
            if (devices["entropyAvailable"] or devices["entropyWords"] or "ble" in report
                    or "TLS: Unsupported" not in report["commands"][0]["console"]):
                raise RuntimeError(f"unexpected entropy denial result. See {args.report}")
            print("PASS: H4 booted and tracker pairing rejected unavailable entropy")
            return
        if "ble" not in report:
            raise RuntimeError(f"BLE qualification failed: {report['bleError']}. See {args.report}")
        if (not devices["entropyAvailable"] or devices["entropyWords"] <= report["bootEntropyWords"]
                or report["gnssRateWrites"] != [1, 7]):
            raise RuntimeError(f"missing peripheral evidence. See {args.report}")
        print("PASS: UART2 H4, dashboard QR, TLS-PSK and three advancing BLE telemetry snapshots")
        if args.tiles:
            assert report["modelError"] == 0 and report["ble"]["tileCrossing"]
            print("PASS: requested the next tile before crossing and painted both sides of the seam")
    elif not args.command:
        battery, tracker = report["commands"]
        snapshots = [json.loads(line[line.index('{"v":1,'):])
                     for line in tracker["console"].splitlines() if '{"v":1,' in line]
        if ("73%" not in battery["console"]
                or len(snapshots) != 8 or not image or report["modelError"] != 0
                or report["touchReads"] == 0 or report["uartPorts"]["gnss"] != [1]
                or report["gnssRateWrites"] != [1, 7]):
            raise RuntimeError(f"dashboard qualification incomplete. See {args.report}")
        check_live(snapshots[-1])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="ESP-EMU 1.2.0 www directory")
    package = parser.add_mutually_exclusive_group(required=True)
    package.add_argument("--wasm", type=Path, help="UART-clock-only ESP-EMU module")
    package.add_argument("--patched-package", type=Path, help="UART2 and entropy browser package")
    parser.add_argument("--elf", type=Path, required=True, help="NuttX ELF beside its merged image")
    parser.add_argument("--report", type=Path, required=True, help="JSON report path")
    parser.add_argument("--browser", help="local Chromium or Chrome executable")
    parser.add_argument("--nm", default="xtensa-esp32s3-elf-nm")
    parser.add_argument("--command", action="append", help="override the default NuttShell commands")
    parser.add_argument("--ble", action="store_true", help="verify QR and authenticated BLE telemetry")
    parser.add_argument("--tiles", action="store_true", help="verify motion-aware tile rendering over BLE")
    parser.add_argument("--map-zoom", type=int, choices=range(15, 19))
    parser.add_argument("--osm-tiles", action="store_true", help="download two real OpenStreetMap tiles")
    parser.add_argument("--deny-entropy", action="store_true", help="run the TLS entropy denial control")
    parser.add_argument("--timeout", type=int, default=25, help="wall seconds per guest command")
    args = parser.parse_args()
    if args.ble and (not args.patched_package or args.command):
        parser.error("--ble requires --patched-package and selects its own guest command")
    if args.tiles and (not args.ble or args.deny_entropy):
        parser.error("--tiles requires --ble with entropy enabled")
    if args.osm_tiles and not args.tiles:
        parser.error("--osm-tiles requires --tiles")
    if args.deny_entropy and not args.ble:
        parser.error("--deny-entropy requires --ble")
    with ExitStack() as resources:
        osm = None
        if args.osm_tiles:
            from requests_cache import CachedSession

            sys.path.insert(0, str(HERE.parents[2] / "tools" / "tracker-map"))
            from relay import USER_AGENT, fetcher

            cache = args.report.with_name("osm-tiles-cache.sqlite")
            session = resources.enter_context(CachedSession(cache, cache_control=True,
                                                           expire_after=7 * 86400, serializer="json"))
            session.headers["User-Agent"] = USER_AGENT
            osm = fetcher(session)
        asyncio.run(run(args, osm))
