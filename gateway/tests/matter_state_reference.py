#!/usr/bin/env python3
"""Real SDK commissioning, crash/restart and fail-closed state qualification.

Requires unused Matter/controller ports and the explicitly supplied generated
H.264 fixture. Creates its own private state. Never uses live gateway enrollment.
"""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from types import SimpleNamespace

import aiohttp
from matter_reference import Controller, run as playback


class Trial:
    def __init__(self, args):
        self.args = args
        self.root = args.runtime.resolve()
        self.root.mkdir(mode=0o700)  # Refuse existing state, even an empty directory.
        self.children = []
        self.device = None
        self.results = []
        self.env = dict(os.environ, TMPDIR=str(self.root), RUST_LOG="error",
                        WEBRTC_CAMERA_STATE=str(self.root / "state"),
                        WEBRTC_CAMERA_H264=str(args.video.resolve()), WEBRTC_CAMERA_FPS="30")

    def start(self, name, command, env=None):
        with (self.root / f"{name}.log").open("ab") as log:
            child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
        self.children.append(child)
        return child

    def start_device(self, initialize=False):
        self.device = self.start("device", [str(self.args.device.resolve())] +
                                 (["--initialize-state"] if initialize else []), self.env)

    async def stop_device(self, kill=False):
        if self.device.poll() is None:
            self.device.kill() if kill else self.device.terminate()
        await asyncio.to_thread(self.device.wait, 5)

    async def view(self, label, flow="receiver-offer", commission=False, recovering=False):
        start = time.monotonic()
        deadline = start + (120 if commission else 30)
        failures = 0
        while True:
            if self.device.poll() is not None:
                raise RuntimeError("fixture device exited before playback")
            try:
                result = await asyncio.wait_for(playback(SimpleNamespace(
                    server=f"http://127.0.0.1:{self.args.port}/ws", node_id=None,
                    endpoint_id=1, frames=60, flow=flow,
                    test_commission_ip=self.args.ip if commission else None,
                )), timeout=max(0, deadline - time.monotonic()))
                result.update(case=label, seconds=round(time.monotonic() - start, 2),
                              transient_failures=failures)
                self.results.append(result)
                print(json.dumps(result), flush=True)
                return
            except (RuntimeError, TimeoutError, aiohttp.ClientError):
                # Recovery includes the independent controller's stale TCP channel.
                # Report retries and enforce one total 30-second recovery budget.
                if not recovering or time.monotonic() + 1 >= deadline:
                    raise
                failures += 1
                await asyncio.sleep(1)

    async def write_label(self):
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{self.args.port}/ws") as ws:
                await ws.receive_json()
                controller = Controller(ws)
                reader = asyncio.create_task(controller.read())
                try:
                    nodes = await controller.request("start_listening")
                    assert len(nodes) == 1
                    await asyncio.wait_for(controller.request(
                        "write_attribute", node_id=nodes[0]["node_id"],
                        attribute_path="0/40/5", value="Nxrs persistence qualification"), 10)
                finally:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader

    async def execute(self):
        controller = self.start("controller", [str(self.args.controller.resolve()),
            "--storage-path", str(self.root / "controller"), "--primary-interface", self.args.interface,
            "--port", str(self.args.port), "--listen-address", "127.0.0.1",
            "--enable-test-net-dcl", "--dcl-offline", "--disable-ota",
            "--disable-thread-diagnostics", "--log-level", "warning"])
        for _ in range(50):
            if controller.poll() is not None:
                raise RuntimeError("isolated controller failed to start")
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.ws_connect(f"http://127.0.0.1:{self.args.port}/ws"):
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(.1)
        else:
            raise TimeoutError("isolated controller startup deadline")
        self.start_device(initialize=True)
        await self.view("commission", commission=True)
        await self.stop_device(kill=True)
        self.start_device()
        await self.view("after-process-kill", "camera-offer", recovering=True)
        await self.stop_device()

        # Model an interrupted staging write without replacing committed identity.
        staged = self.root / "state/state.next"
        with staged.open("xb") as file:
            os.chmod(staged, 0o600)
            file.write(b"incomplete test snapshot")
            file.flush()
            os.fsync(file.fileno())
        self.start_device()
        await self.view("abandoned-stage", recovering=True)
        assert not staged.exists(), "orphan staging file was not retired"
        await self.stop_device()

        state = self.root / "state/state.bin"
        backup = state.read_bytes()
        corrupt = bytearray(backup)
        corrupt[-1] ^= 1
        state.write_bytes(corrupt)
        self.start_device()
        code = await asyncio.to_thread(self.device.wait, 5)
        assert code != 0, "corrupt identity was accepted"
        self.results.append(dict(case="corrupt-state-refused", exit_code=code))
        state.write_bytes(backup)  # Restore only this fixture's private checkpoint.
        self.start_device()
        await self.view("restored-checkpoint", "camera-offer", recovering=True)

        # Force a real SDK persistence request to fail while the node is serving.
        state.rename(self.root / "state/checkpoint.bin")
        try:
            await self.write_label()
        except (RuntimeError, TimeoutError, aiohttp.ClientError):
            pass
        code = await asyncio.to_thread(self.device.wait, 5)
        assert code != 0, "node kept serving after a storage failure"
        self.results.append(dict(case="runtime-storage-failure-stops-node", exit_code=code))
        (self.root / "results.json").write_text(json.dumps(self.results, indent=2) + "\n")
        print(json.dumps(dict(passed=True, cases=len(self.results))), flush=True)

    async def close(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, 5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, 5)


async def run(args):
    # Do not stop or reconfigure an existing service to obtain these ports.
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as tcp, \
         socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as udp, \
         socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ws:
        # Permit retired TCP connections in TIME_WAIT, but require exclusive
        # listeners. No SO_REUSEPORT and no modification of existing services.
        for listener, address in ((tcp, ("::", 5540)), (ws, ("127.0.0.1", args.port))):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen(1)
        udp.bind(("::", 5540))
    trial = Trial(args)
    try:
        await trial.execute()
    finally:
        await trial.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("device", "controller", "video", "runtime"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--ip", required=True)
    parser.add_argument("--port", type=int, default=15580)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be an unprivileged TCP port")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
