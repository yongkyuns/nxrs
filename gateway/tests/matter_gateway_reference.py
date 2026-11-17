#!/usr/bin/env python3
"""Publish through an isolated Nxrs gateway and observe the resulting HA automation."""
import argparse
from contextlib import ExitStack
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time


ROOT = Path(__file__).resolve().parents[2]
CAMERA = "cam_one"


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def start(stack, root, name, command, env=None, *, cwd=None):
    with (root / (name + ".log")).open("ab") as log:
        child = subprocess.Popen(command, stdout=log, stderr=log, env=env, cwd=cwd)
    stack.callback(stop, child)
    return child


class Gateway:
    def __init__(self, root):
        # Reserve both ports together so they are distinct.
        with socket.socket() as a, socket.socket() as b:
            a.bind(("127.0.0.1", 0))
            b.bind(("127.0.0.1", 0))
            self.api, self.internal = a.getsockname()[1], b.getsockname()[1]
        self.publisher = secrets.token_hex(32)
        token = secrets.token_hex(32)
        self.token_file = root / "subscriber.token"
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(token)
        def digest(value):
            return hashlib.sha256(value.encode()).hexdigest()
        self.config = root / "control.json"
        self.config.write_text(json.dumps(dict(
            listen=f"127.0.0.1:{self.api}", auth_listen=f"127.0.0.1:{self.internal}",
            grant_ttl_seconds=20, max_grants=4, media_origins=["https://media.test"],
            cameras=[dict(camera_id=CAMERA, gateway_id="gw", site_id="site", name="Test camera",
                          source="synthetic", stream_path="one", whep_endpoint="https://media.test/one/whep")],
            viewers=[dict(viewer_id="matter", scope="detections", site_id="site", camera_ids=[CAMERA], token_sha256=digest(token))],
            publishers=[dict(username="publisher", camera_id=CAMERA, token_sha256=digest(self.publisher))])))
        self.source = json.loads((ROOT / "clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json")
                                 .read_text())["source"]
        self.sequence = 20

    def post(self, endpoint, data):
        conn = http.client.HTTPConnection("127.0.0.1", self.internal, timeout=3)
        try:
            conn.request("POST", f"/internal/cameras/{CAMERA}/{endpoint}", json.dumps(data),
                         {"Authorization": "Bearer " + self.publisher, "x-nxrs-publisher": "publisher",
                          "Content-Type": "application/json"})
            res = conn.getresponse()
            return res.status, res.read(1024)
        finally:
            conn.close()

    def viewer_status(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.api, timeout=3)
        try:
            conn.request(method, path, "{}" if method == "POST" else None,
                         {"Authorization": "Bearer " + self.token_file.read_text(),
                          "Content-Type": "application/json"})
            return conn.getresponse().status
        finally:
            conn.close()

    def register(self, instance):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and instance.poll() is None:
            try:
                status, body = self.post("detection-sessions", {})
                assert status == 200
                return json.loads(body)["source_session_id"]
            except ConnectionRefusedError:
                time.sleep(0.05)
        raise RuntimeError("gateway startup")

    def publish(self, session, occupied):
        self.sequence += 1
        now = self.sequence * 400
        source = self.source.copy()
        source.update(publication_id=self.sequence, emitted_ms=now,
                      presence="person" if occupied else "clear")
        source["latest"] = dict(source["latest"], frame_sequence=self.sequence, captured_ms=now-100)
        if not occupied:
            source["latest"].update(objects=[], person_score_permille=0)
            source.update(event_count=0, events=[])
        status, _ = self.post("detections", dict(source_session_id=session, frame_sequence=self.sequence,
                                                frame_capture_ms=now, source=source))
        return status


def run(args):
    from playwright.sync_api import sync_playwright
    from matter_topology_reference import observe, playback
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with ExitStack() as stack:
        gateway = Gateway(root)
        def start_control():
            return start(stack, root, "gateway", [args.gateway, gateway.config])
        instance = start_control()
        session = gateway.register(instance)
        assert gateway.viewer_status("GET", "/v1/cameras") == 403
        assert gateway.viewer_status("POST", f"/v1/cameras/{CAMERA}/playback-grants") == 403
        env = dict(os.environ, TMPDIR=str(root), WEBRTC_CAMERA_CONTROL=f"127.0.0.1:{gateway.api}",
                   WEBRTC_CAMERA_TOKEN_FILE=str(gateway.token_file), WEBRTC_CAMERA_ID=CAMERA,
                   WEBRTC_CAMERA_STATE=str(args.state.resolve()), WEBRTC_CAMERA_H264=str(args.video.resolve()),
                   WEBRTC_CAMERA_FPS="30", RUST_LOG="error")
        env.pop("WEBRTC_CAMERA_TRIAL_DIR", None)
        device = start(stack, root, "device", [args.device], env)
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch(channel="chrome", headless=True)
        stack.callback(browser.close)
        page, state = observe(browser, args)
        page.wait_for_function("""id => { const entity = document.querySelector('home-assistant').hass.states[id];
            return entity && !['unknown','unavailable'].includes(entity.state); }""", arg=args.camera, timeout=90000)
        assert state(args.sensor) == "unavailable"
        expected = []
        def observe_level(level, publish=None):
            expected.append(level)
            until = time.monotonic() + 20
            while time.monotonic() < until:
                assert device.poll() is None
                if publish is not None:
                    assert gateway.publish(session, publish) == 204
                if len(page.evaluate("window.transitions")) >= len(expected):
                    break
                page.wait_for_timeout(300)
            assert page.evaluate("window.transitions") == expected, "unexpected automation transition"
            assert state(args.sensor) == level

        observe_level("on", True)
        # Stop publishing. No explicit unavailable event is sent by the test.
        observe_level("unavailable")
        old = session
        session = gateway.register(instance)
        assert gateway.publish(old, True) == 409
        observe_level("off", False)
        stop(instance)
        observe_level("unavailable")
        instance = start_control()
        session = gateway.register(instance)
        observe_level("on", True)
        conn = http.client.HTTPConnection("127.0.0.1", gateway.internal, timeout=3)
        try:
            conn.request("POST", "/internal/viewers/matter/revoke", "{}", {"Content-Type": "application/json"})
            assert conn.getresponse().status == 204
        finally:
            conn.close()
        # The publisher stays authorized. Only the bridge subscriber loses access.
        observe_level("unavailable", True)
        assert gateway.viewer_status("GET", f"/v1/cameras/{CAMERA}/detections") == 403
        # Keep a fresh positive observation through multiple reconnect attempts.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            assert gateway.publish(session, True) == 204
            page.wait_for_timeout(300)
        assert state(args.sensor) == "unavailable" and page.evaluate("window.transitions") == expected
        video = playback(args)
        assert state(args.sensor) == "unavailable"
        assert page.evaluate("window.transitions") == expected
        return dict(automation_transitions=expected, retired_source_http_status=409,
                    gateway_restart_recovered=True, subscriber_revoked=True,
                    catalogue_and_playback_denied=True, video_during_sensor_unavailable=video)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("gateway", "device", "state", "video", "storage-state", "runtime"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument("--camera", required=True)
    parser.add_argument("--sensor", required=True)
    args = parser.parse_args()
    result = run(args)
    (args.runtime / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Authenticated occupancy, expiry, source replacement, gateway restart and browser video passed")
