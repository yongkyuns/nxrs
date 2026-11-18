#!/usr/bin/env python3
"""Exercise Home Assistant's actual camera dialog in an isolated browser.

Use an authenticated Playwright storage state from the private HA fixture.
Print only media counters and errors with fixed labels, never SDP or credentials.
"""
import argparse
from collections import Counter
import json
import traceback
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def observe_candidate_responses(page):
    accepted, rejected = Counter(), Counter()

    def observe(socket):
        if urlparse(socket.url).path != "/api/websocket":
            return
        pending = {}

        def messages(payload):
            values = json.loads(payload)
            return values if isinstance(values, list) else [values]

        def sent(payload):
            for value in messages(payload):
                if value.get("type") != "camera/webrtc/candidate":
                    continue
                parts = value["candidate"]["candidate"].split()
                kind = "mdns" if len(parts) > 4 and parts[4].casefold().endswith(".local") else "other"
                pending[value["id"]] = kind

        def received(payload):
            for value in messages(payload):
                if value.get("type") != "result":
                    continue
                kind = pending.pop(value.get("id"), None)
                if kind:
                    (accepted if value.get("success") else rejected)[kind] += 1

        socket.on("framesent", sent)
        socket.on("framereceived", received)

    page.on("websocket", observe)
    return accepted, rejected


def run(args):
    with sync_playwright() as playwright:
        if args.browser_engine == "webkit":
            browser = playwright.webkit.launch(headless=True)
        else:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(storage_state=str(args.storage_state),
                                          viewport={"width": 1280, "height": 900})
            # Observe negotiation without rewriting the browser's offer.
            context.add_init_script("""
                window.nxrsPeers = [];
                window.nxrsOffers = [];
                const original = RTCPeerConnection;
                window.RTCPeerConnection = new Proxy(original, {
                    construct(target, args) {
                        const peer = Reflect.construct(target, args);
                        window.nxrsPeers.push(peer);
                        const create = peer.createOffer.bind(peer);
                        peer.createOffer = async (...args) => {
                            const offer = await create(...args);
                            window.nxrsOffers.push({bytes: offer.sdp.length,
                                media: offer.sdp.split(/\\r?\\n/)
                                    .filter(line => line.startsWith('m='))
                                    .map(line => line.split(' ')[0].slice(2))});
                            return offer;
                        };
                        return peer;
                    }
                });
            """)
            page = context.new_page()
            accepted, rejected = observe_candidate_responses(page)
            page.goto(args.server, wait_until="networkidle", timeout=30000)
            page.wait_for_function("""entity => {
                const camera = document.querySelector('home-assistant')?.hass?.states?.[entity];
                return camera && camera.state !== 'unavailable';
            }""", arg=args.entity, timeout=60000)
            page.evaluate("""entity => document.querySelector('home-assistant')
                .dispatchEvent(new CustomEvent('hass-more-info', {
                    detail: {entityId: entity}, bubbles: true, composed: true
                }))""", args.entity)
            video = page.locator("video")
            video.wait_for(state="visible", timeout=30000)
            result = video.evaluate("""(video, {marker, rate}) => new Promise((resolve, reject) => {
                const canvas = document.createElement('canvas');
                canvas.width = 128; canvas.height = 72;
                const ctx = canvas.getContext('2d', {willReadFrequently: true});
                const hashes = new Set(), times = [];
                const timeout = setTimeout(() => reject(new Error('frame deadline')), 15000);
                function frame(now, metadata) {
                    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                    if (marker) {
                        const [red, green, blue] = ctx.getImageData(8, 4, 1, 1).data;
                        if (!(marker === 'red' ? red > 185 && blue < 70 && green < 70 :
                                                 blue > 185 && red < 70 && green < 70)) {
                            clearTimeout(timeout); reject(new Error('incorrect fixture marker')); return;
                        }
                    }
                    let hash = 2166136261;
                    for (const byte of ctx.getImageData(0, 0, canvas.width, canvas.height).data)
                        hash = Math.imul(hash ^ byte, 16777619) >>> 0;
                    hashes.add(hash);
                    times.push({now, media: metadata.mediaTime, rtp: metadata.rtpTimestamp,
                        frames: metadata.presentedFrames, presentation: metadata.presentationTime});
                    if (times.length < 60) { video.requestVideoFrameCallback(frame); return; }
                    clearTimeout(timeout);
                    const steps = times.slice(1).map((t,i) => (t.rtp-times[i].rtp) >>> 0);
                    const mediaSteps = times.slice(1).map((t,i) => Math.round((t.media-times[i].media)*90000));
                    const rtpAdvances = times.every(t => Number.isInteger(t.rtp)) &&
                        steps.every(step => step > 0 && step < 0x80000000);
                    const compositorAdvances = times.every((t, i) => Number.isInteger(t.frames) &&
                        Number.isFinite(t.presentation) && (!i || t.frames > times[i-1].frames &&
                            t.presentation > times[i-1].presentation));
                    resolve({...(rate ? {rtp_clock_valid: rtpAdvances &&
                        steps.every(t => t % (90000/rate) === 0),
                        min_step_90khz: Math.min(...steps), max_step_90khz: Math.max(...steps),
                        media_time_min_step_90khz: Math.min(...mediaSteps)} : {}),
                        ...(marker ? {fixture_marker: marker, first_presented_ms:
                        performance.timeOrigin + times[0].now, last_presented_ms:
                        performance.timeOrigin + times.at(-1).now} : {}), presented_frames: times.length, distinct_sampled_images: hashes.size,
                        dimensions: [video.videoWidth, video.videoHeight],
                        advancing_media_time: times.every((t, i) => !i || t.media > times[i-1].media),
                        advancing_rtp_clock: rtpAdvances,
                        advancing_compositor: compositorAdvances,
                        presented_fps: (times.length-1)*1000/(times.at(-1).now-times[0].now),
                        playback_quality: {
                            total: video.getVideoPlaybackQuality().totalVideoFrames,
                            dropped: video.getVideoPlaybackQuality().droppedVideoFrames}});
                }
                video.requestVideoFrameCallback(frame);
            })""", {"marker": args.fixture_marker, "rate": args.fixture_rate})
            if args.fixture_rate and not result["rtp_clock_valid"]:
                print("Unexpected fixture RTP steps", result["min_step_90khz"], result["max_step_90khz"])
                raise AssertionError("encoded RTP clock")
            assert result["distinct_sampled_images"] > 1
            if args.browser_engine == "webkit":
                # WebKit's MediaStream player can leave callback mediaTime at zero.
                assert result["advancing_rtp_clock"] and result["advancing_compositor"]
            else:
                assert result["advancing_media_time"]
            result["presented_fps"] = round(result["presented_fps"], 2)
            engine = "WebKit" if args.browser_engine == "webkit" else "Chrome"
            result.update(kind=f"generated-source Home Assistant dialog; headless {engine}",
                          browser=browser.version, offered_media=page.evaluate("window.nxrsOffers"))
            result["peers"] = page.evaluate("""async () => {
                const result = [];
                for (const peer of window.nxrsPeers) {
                    const stats = await peer.getStats();
                    for (const value of stats.values()) {
                        if (value.type !== 'inbound-rtp' || value.kind !== 'video') continue;
                        result.push({state: peer.connectionState,
                            codec: stats.get(value.codecId)?.mimeType,
                            decoded_frames: value.framesDecoded,
                            dropped_frames: value.framesDropped,
                            packets_lost: value.packetsLost});
                    }
                }
                return result;
            }""")
            assert len(result["peers"]) == 1
            assert result["peers"][0]["state"] == "connected"
            assert result["peers"][0]["codec"] == "video/H264"
            assert result["peers"][0]["decoded_frames"] >= result["presented_frames"]
            if args.audio_cue:
                # Unlock Web Audio with a user gesture. Read only the incoming track.
                page.keyboard.press("Shift")
                result["audio_cue"] = video.evaluate((Path(__file__).parent / "matter/audio-cue.js").read_text())
            if args.await_source_loss:
                args.await_source_loss.touch(exist_ok=False)
                page.wait_for_function("['closed','disconnected','failed'].includes(window.nxrsPeers[0].connectionState)",
                                       timeout=15000)
                result["peer_ended_before_dialog_closed"] = True
            if args.screenshot:
                page.screenshot(path=str(args.screenshot))
            page.get_by_role("button", name="Close", exact=True).click()
            page.wait_for_function("window.nxrsPeers.every(peer => peer.connectionState === 'closed')",
                                   timeout=10000)
            result["browser_peers_closed"] = True
            result["candidate_responses"] = {"accepted": dict(accepted), "rejected": dict(rejected)}
            return result
        except Exception:
            if args.screenshot and "page" in locals():
                page.screenshot(path=str(args.screenshot))
            raise
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:18123")
    parser.add_argument("--browser-engine", choices=("chrome", "webkit"), default="chrome")
    parser.add_argument("--entity", required=True)
    parser.add_argument("--storage-state", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--await-source-loss", type=Path)
    parser.add_argument("--fixture-rate", type=int, choices=(10, 15))
    parser.add_argument("--fixture-marker", choices=["red", "blue"])
    parser.add_argument("--audio-cue", action="store_true", help="measure the paired generated light and microphone tone")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = urlparse(args.server)
    if url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password:
        parser.error("use an isolated loopback Home Assistant fixture")
    try:
        result = run(args)
    except Exception as error:
        # Playwright error strings can contain browser inputs or raw page data.
        stage = next((f.lineno for f in reversed(traceback.extract_tb(error.__traceback__))
                          if f.filename == __file__), 0)
        raise SystemExit(f"Home Assistant browser witness failed at line {stage}: {type(error).__name__}") from None
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
