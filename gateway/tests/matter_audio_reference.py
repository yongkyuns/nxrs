#!/usr/bin/env python3
"""Qualify timestamped H.264/Opus RTSP publication with generated optical and acoustic cues."""
import argparse
from array import array
from contextlib import ExitStack
import json
from pathlib import Path
import statistics
import subprocess
import time

import av

from matter_rtsp_reference import publish_encoded, start_media, wait_path


def check_admission(args, port, credential):
    scenarios = {
        "missing audio": (["-map", "0:v"], [], ["mpegts-av"]),
        "stereo": (["-map", "0:v", "-map", "1:a"], ["-c:a", "libopus", "-ac", "2"], ["mpegts-av"]),
        "AAC": (["-map", "0:v", "-map", "1:a"], ["-c:a", "aac"], ["mpegts-av"]),
        "extra track": (["-map", "0:v", "-map", "1:a", "-map", "1:a"], ["-c:a", "libopus"], ["mpegts-av"]),
        "paired input in video-only mode": (["-map", "0:v", "-map", "1:a"], ["-c:a", "libopus"], []),
    }
    for label, (mapping, encoding, mode) in scenarios.items():
        ts = subprocess.check_output(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15", "-f", "lavfi", "-i", "sine=sample_rate=16000",
            "-t", "0.5", *mapping, "-c:v", "libx264", "-bf", "0", *encoding, "-f", "mpegts", "pipe:1"], timeout=8)
        rejected = subprocess.run([args.publisher, f"127.0.0.1:{port}", "publisher", "cam", *mode],
                                  input=credential.encode() + ts, capture_output=True, timeout=8)
        assert rejected.returncode and not rejected.stdout and rejected.stderr == b"nxrs-rtsp-publisher: Media\n", label
    return list(scenarios)


def observe(streams):
    video, = streams.streams.video
    audio, = streams.streams.audio
    assert (video.codec_context.name, audio.codec_context.name) == ("h264", "opus")
    assert len(streams.streams) == 2
    mono = av.AudioResampler(format="s16", layout="mono", rate=16_000)
    stamps = {"video": [], "audio": []}
    edges = {"video": [], "audio": []}
    last_level = {}
    frequencies = []
    deadline = time.monotonic() + 14
    for frame in streams.decode(video=0, audio=0):
        assert time.monotonic() < deadline, "timed out observing both tracks"
        if isinstance(frame, av.VideoFrame):
            kind = "video"
            assert (frame.width, frame.height) == (320, 240)
            active = bytes(frame.planes[0])[0] > 160
        else:
            kind = "audio"
            pcm = array("h")
            for chunk in mono.resample(frame):
                pcm.frombytes(bytes(chunk.planes[0])[:chunk.samples * pcm.itemsize])
            if not pcm:
                continue
            active = sum(abs(x) for x in pcm) / len(pcm) > 3000
            if active:
                crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:]))
                frequencies.append(crossings * 16_000 / len(pcm))
        timestamp = frame.pts * frame.time_base
        if stamps[kind]:
            assert timestamp > stamps[kind][-1], "decoded clock regression"
        stamps[kind].append(timestamp)
        if active and last_level.get(kind) is False:
            edges[kind].append(float(timestamp))
        last_level[kind] = active
        if min(map(len, edges.values())) >= 6:
            break
    assert min(map(len, edges.values())) >= 6
    for kind, rate in (("video", 15), ("audio", 50)):
        ticks = [float(b - a) for a, b in zip(stamps[kind], stamps[kind][1:])]
        assert abs(statistics.median(ticks) - 1 / rate) < .001
        periods = [b - a for a, b in zip(edges[kind], edges[kind][1:])]
        expected = (.6, 1.0, 1.4)
        phase = min(range(3), key=lambda i: abs(periods[0] - expected[i]))
        assert all(abs(period - expected[(i + phase) % 3]) < .07 for i, period in enumerate(periods)), "lost or repeated cue"
    hz = statistics.median(frequencies)
    assert 950 <= hz <= 1050, "unexpected decoded tone"
    # Match adjacent pulses by timestamp, independently of which track joined
    # first. Omit the outer pulses where the other track may have no sample.
    skews = [min(abs(v - a) for a in edges["audio"]) for v in edges["video"][1:-1]]
    assert max(skews) < .1, "generated audio/video cues separated by 100 ms or more"
    return dict(video_frames=len(stamps["video"]), audio_frames=len(stamps["audio"]),
                median_tone_hz=round(hz, 1), maximum_cue_difference_ms=round(max(skews) * 1000, 1),
                video_pulses=len(edges["video"]), audio_pulses=len(edges["audio"]))


def publish_av(stack, root, args, port, token):
    # Deliberately unequal pulse intervals make a missing cue observable.
    pulse = "lt(mod(t,3),0.22)+between(mod(t,3),0.6,0.82)+between(mod(t,3),1.6,1.82)"
    # Audio first exercises per-track rescaling when video isn't stream zero.
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-re", "-f", "lavfi", "-i", f"color=black:s=320x240:r=15,drawbox=color=white:t=fill:enable='{pulse}'",
               "-re", "-f", "lavfi", "-i", f"aevalsrc='if({pulse},0.6*sin(2*PI*1000*t),0)':s=16000",
               "-map", "1:a", "-map", "0:v", "-c:a", "libopus", "-ar", "16000", "-ac", "1",
               "-b:a", "64k", "-frame_duration", "20", "-c:v", "libx264", "-bf", "0",
               "-g", "15", "-threads", "2", "-preset", "ultrafast", "-tune", "zerolatency",
               "-f", "mpegts", "-muxdelay", "0", "pipe:1"]
    return publish_encoded(stack, root, args, port, token, command, ("mpegts-av",))


def run(args):
    root = args.runtime.resolve()
    root.mkdir(mode=0o700)
    with ExitStack() as stack:
        gateway, _, _, port = start_media(stack, root, args)
        rejected = check_admission(args, port, gateway.publisher)
        children = publish_av(stack, root, args, port, gateway.publisher)
        wait_path(gateway, lambda p: p.get("ready"), children)
        token = gateway.credentials["reader"].read_text()
        uri = f"rtsp://reader:{token}@127.0.0.1:{port}/cam"
        av.logging.set_level(av.logging.PANIC)
        try:
            with av.open(uri, options={"rtsp_transport": "tcp"}, timeout=(4, 4)) as stream:
                result = observe(stream)
                result["rejected_inputs"] = rejected
        except av.FFmpegError:
            # SDK exceptions can include the RTSP credential.
            raise RuntimeError("RTSP receive or decode failed") from None
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "gateway", "publisher", "mediamtx"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(parser.parse_args())
