"""Strict gateway configuration and an independent RTSP receiver for the Matter trial."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import sys
import time


def private_file(path, contents):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as file:
        file.write(contents)


def request(port, method, path, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Authorization": "Bearer " + token} if token else {}
        body = None
        if method == "POST":
            body = "{}"
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.read(65536)
    finally:
        connection.close()


class StrictMedia:
    def __init__(self, root, api, control, internal):
        self.api, self.control, self.internal = api, control, internal
        self.management = secrets.token_hex(32)
        self.publisher = secrets.token_hex(32)
        self.credentials = {}
        cameras = ["cam"]
        viewers = []
        for name in ("reader", "independent"):
            secret = secrets.token_hex(32)
            self.credentials[name] = root / (name + ".token")
            private_file(self.credentials[name], secret)
            viewers.append(dict(viewer_id=name, scope="camera_source", site_id="lab", camera_ids=cameras,
                                token_sha256=hashlib.sha256(secret.encode()).hexdigest()))
        config = dict(listen=f"127.0.0.1:{control}", auth_listen=f"127.0.0.1:{internal}",
            grant_ttl_seconds=20, max_grants=8, media_origins=["https://media.test"],
            cameras=[dict(camera_id="cam", gateway_id="gw", site_id="lab", name="Test camera",
                          source="synthetic", stream_path="cam", whep_endpoint="https://media.test/cam/whep")],
            viewers=viewers, publishers=[dict(username="publisher", camera_id="cam",
                token_sha256=hashlib.sha256(self.publisher.encode()).hexdigest())])
        self.config, self.security = root / "gateway.json", root / "security.json"
        revocations = root / "revocations.json"
        private_file(self.config, json.dumps(config))
        private_file(revocations, json.dumps(dict(version=1, revoked=[])))
        private_file(self.security, json.dumps(dict(revocations_file=str(revocations),
                     media_api=f"127.0.0.1:{api}", media_api_token=self.management)))

    def path(self, name="cam"):
        try:
            status, body = request(self.api, "GET", f"/v3/paths/get/{name}", self.management)
        except ConnectionRefusedError:
            return {}
        return json.loads(body) if status == 200 else {}

    def ready(self, process, revoked=False):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and process.poll() is None:
            try:
                status, body = request(self.internal, "GET", "/internal/viewers/reader/revocation")
                if status == 200:
                    state = json.loads(body)
                    if state["media_control_ready"] and state["persistence_healthy"] and state["revoked"] == revoked:
                        return
            except ConnectionRefusedError:
                pass
            time.sleep(.1)
        raise RuntimeError("strict RTSP management readiness")

    def revoke(self, viewer="reader"):
        assert request(self.internal, "POST", f"/internal/viewers/{viewer}/revoke")[0] == 202


def receive(port, user, secret_file, counter):
    # The synthetic receiver authenticates once. Its exit on revocation proves
    # server-side enforcement independently of the Matter reader's periodic check.
    import av
    av.logging.set_level(av.logging.PANIC)
    try:
        url = f"rtsp://{user}:{Path(secret_file).read_text()}@127.0.0.1:{int(port)}/cam"
        with av.open(url, options={"rtsp_transport": "tcp"}, timeout=(3, 5)) as stream:
            with open(counter, "xb", buffering=0) as count:
                for frame in stream.decode(video=0):
                    if frame.width and count.tell() < 2048:
                        count.write(b".")
    except av.error.HTTPUnauthorizedError:
        return 77
    except Exception:
        # FFmpeg errors may contain the private RTSP URI.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(receive(*sys.argv[1:]))
