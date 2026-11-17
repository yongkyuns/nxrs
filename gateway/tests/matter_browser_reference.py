#!/usr/bin/env python3
"""Exercise Home Assistant's actual camera dialog in an isolated Chrome browser.

Use an authenticated Playwright storage state from the private HA fixture.
Print only media counters and errors with fixed labels, never SDP or credentials.
"""
import argparse
import json
import traceback
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def run(args):
    with sync_playwright() as playwright:
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
            result = video.evaluate("""video => new Promise((resolve, reject) => {
                const canvas = document.createElement('canvas');
                canvas.width = 128; canvas.height = 72;
                const ctx = canvas.getContext('2d', {willReadFrequently: true});
                const hashes = new Set(), times = [];
                const timeout = setTimeout(() => reject(new Error('frame deadline')), 15000);
                function frame(now, metadata) {
                    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                    let hash = 2166136261;
                    for (const byte of ctx.getImageData(0, 0, canvas.width, canvas.height).data)
                        hash = Math.imul(hash ^ byte, 16777619) >>> 0;
                    hashes.add(hash); times.push({now, media: metadata.mediaTime});
                    if (times.length < 60) { video.requestVideoFrameCallback(frame); return; }
                    clearTimeout(timeout);
                    resolve({presented_frames: times.length, distinct_sampled_images: hashes.size,
                        dimensions: [video.videoWidth, video.videoHeight],
                        advancing_media_time: times.every((t, i) => !i || t.media > times[i-1].media),
                        presented_fps: (times.length-1)*1000/(times.at(-1).now-times[0].now),
                        playback_quality: {
                            total: video.getVideoPlaybackQuality().totalVideoFrames,
                            dropped: video.getVideoPlaybackQuality().droppedVideoFrames}});
                }
                video.requestVideoFrameCallback(frame);
            })""")
            assert result["advancing_media_time"] and result["distinct_sampled_images"] > 1
            result["presented_fps"] = round(result["presented_fps"], 2)
            result.update(kind="generated-source Home Assistant dialog; headless Chrome",
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
            if args.screenshot:
                page.screenshot(path=str(args.screenshot))
            page.get_by_role("button", name="Close", exact=True).click()
            page.wait_for_function("window.nxrsPeers.every(peer => peer.connectionState === 'closed')",
                                   timeout=10000)
            result["browser_peers_closed"] = True
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
    parser.add_argument("--entity", required=True)
    parser.add_argument("--storage-state", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
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
