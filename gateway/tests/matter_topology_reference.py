#!/usr/bin/env python3
"""Observe a real Home Assistant state automation while driving the isolated bridge."""
import argparse
import json
import socket
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright

SERVER = "http://127.0.0.1:18123"


def observe(browser, args):
    page = browser.new_page(storage_state=str(args.storage_state))
    page.goto(SERVER, wait_until="networkidle", timeout=30000)
    page.wait_for_function("() => document.querySelector('home-assistant')?.hass?.connected")
    page.evaluate("""async sensor => {
        window.transitions = [];
        await document.querySelector('home-assistant').hass.connection.subscribeEvents(
            event => { if (event.data.sensor === sensor)
                window.transitions.push(event.data.observed); }, 'nxrs_fixture_occupancy');
    }""", args.sensor)
    def state(entity):
        return page.evaluate("""id =>
            document.querySelector('home-assistant').hass.states[id]?.state""", entity)
    return page, state


def playback(args):
    with TemporaryDirectory() as directory:
        output = Path(directory) / "playback.json"
        subprocess.run([sys.executable, str(Path(__file__).with_name("matter_browser_reference.py")),
                        "--entity", args.camera, "--storage-state", str(args.storage_state),
                        "--output", str(output)], timeout=120, check=True)
        result = json.loads(output.read_text())
    return result


def run(args):
    with sync_playwright() as pw, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
        control.connect(str(args.control))
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            page, state = observe(browser, args)
            assert state(args.sensor) == "unavailable"
            assert state(args.camera) not in (None, "unknown", "unavailable")
            expected = []
            for command, observed in [
                ("person", "on"), ("unavailable", "unavailable"), ("clear", "off"),
                ("unavailable", "unavailable"), ("person", "on"), ("clear", "off"),
                ("person", "on"), ("unavailable", "unavailable"),
            ]:
                expected.append(observed)
                control.send(command.encode())
                page.wait_for_function("n => window.transitions.length >= n", arg=len(expected),
                                       timeout=20000)
                assert page.evaluate("window.transitions") == expected, "unexpected automation transition"
                assert state(args.camera) not in (None, "unknown", "unavailable")
                assert state(args.sensor) == observed
            # Leave the subscription open while the other browser receives video.
            result = playback(args)
            assert state(args.sensor) == "unavailable" and page.evaluate("window.transitions") == expected
            return {"automation_transitions": expected, "video_during_sensor_unavailable": result}
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sensor", required=True)
    parser.add_argument("--camera", required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--storage-state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args)
    except Exception as error:
        raise SystemExit("Home Assistant topology witness failed: " + type(error).__name__) from None
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print("Occupancy automation and video during detector unavailability passed")


if __name__ == "__main__":
    main()
