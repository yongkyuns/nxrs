// A commissionable Matter client. The test administrator enrolls it onto the camera's
// fabric like any other node. Its operational key stays in its own SDK storage.
"use strict";

const { createRequire } = require("node:module");
const { join } = require("node:path");
const { createInterface } = require("node:readline");

async function main() {
    const [install, storage, iface, port] = process.argv.slice(2);
    const sdk = createRequire(join(install, "package.json"));
    const { Environment, ServerNode, Endpoint, NodeId } = sdk("@matter/main");
    const { FabricManager, Invoke, Read } = sdk("@matter/main/protocol");
    const { WebRtcTransportProvider } = sdk("@matter/main/clusters/web-rtc-transport-provider");
    const { WebRtcTransportRequestorServer } = sdk("@matter/main/behaviors/web-rtc-transport-requestor");
    const { CameraControllerDevice } = sdk("@matter/main/devices/camera-controller");
    const env = Environment.default;
    env.vars.set("storage.path", storage);
    env.vars.set("mdns.networkInterface", iface);
    const node = await ServerNode.create({ id: "viewer", environment: env,
        network: { port: Number(port), ble: false, tcp: true, transportPreference: "tcp" },
        dcl: { offline: true } });
    // stdout belongs exclusively to the private parent/child protocol.
    const reply = value => process.stdout.write(JSON.stringify(value) + "\n");
    try {
        let offer;
        // Answer and ICE can arrive on separate exchanges before ProvideOffer returns.
        // Register only the session confirmed by the provider's response, on its exact CASE peer.
        class ViewerRequestor extends WebRtcTransportRequestorServer {
            async register(request) {
                const peer = this.context.session.peerAddress;
                if (!offer || offer.camera !== peer.nodeId || offer.fabric !== peer.fabricIndex) return;
                const session = await offer.confirmed;
                if (session && session.id === request.webRtcSessionId) this.upsertSession(session);
            }
            async offer(request) {
                await this.register(request);
                return super.offer(request);
            }
            async answer(request) {
                await this.register(request);
                return super.answer(request);
            }
            async iceCandidates(request) {
                await this.register(request);
                return super.iceCandidates(request);
            }
        }
        const endpoint = await node.add(new Endpoint(CameraControllerDevice.with(ViewerRequestor),
            { id: "viewer" }));
        let lastEnd;
        endpoint.eventsOf(WebRtcTransportRequestorServer).end.on((session, reason) => {
            lastEnd = { id: session.id, reason };
        });
        await node.start();
        reply({ code: node.state.commissioning.pairingCodes.qrPairingCode, endpoint: endpoint.number });
        for await (const line of createInterface({ input: process.stdin })) {
            const request = JSON.parse(line);
            const [fabric] = node.env.get(FabricManager);
            if (!fabric) throw Error("Viewer has no operational fabric");
            if (request.op === "identity") {
                reply({ node: String(fabric.nodeId), fabric: String(fabric.fabricId) });
                continue;
            }
            const camera = NodeId(BigInt(request.camera));
            const peer = await node.peers.forAddress({ fabricIndex: fabric.fabricIndex, nodeId: camera });
            async function invoke(command, fields) {
                const responses = peer.interaction.invoke(Invoke({ endpoint: 1, cluster: WebRtcTransportProvider,
                    command, fields }));
                for await (const chunk of responses) for (const report of chunk) {
                    if (report.kind === "cmd-response") return report.data;
                    if (report.kind === "cmd-status" && report.status === 0) return;
                    throw Error("Provider command failed with status " + report.status);
                }
                throw Error("Missing provider response");
            }
            if (request.op === "sessions") {
                const events = peer.interaction.read(Read({ attributes: [
                    { endpointId: 1, clusterId: 0x553, attributeId: 0 },
                ] }));
                let result;
                for await (const chunk of events) for (const report of chunk) {
                    if (report.kind === "attr-status") result = { status: report.status };
                    if (report.kind === "attr-value") result = { sessions: report.value.length };
                }
                if (!result) throw Error("Missing CurrentSessions report");
                reply(result);
            } else if (request.op === "offer" || request.op === "hold") {
                const deferred = request.op === "hold";
                const events = endpoint.eventsOf(WebRtcTransportRequestorServer);
                const signal = new Promise(resolve => {
                    if (deferred) events.offer.once(session => resolve({ id: session.id }));
                    else events.answer.once((session, sdp) => resolve({ id: session.id, sdp }));
                });
                const confirmation = Promise.withResolvers();
                offer = { camera, fabric: fabric.fabricIndex, confirmed: confirmation.promise };
                lastEnd = undefined;
                try {
                    const fields = { streamUsage: 3, originatingEndpointId: endpoint.number,
                        videoStreams: [request.video] };
                    const outcome = deferred ? await invoke("solicitOffer", fields) :
                        await invoke("provideOffer", { ...fields, webRtcSessionId: null, sdp: request.sdp });
                    const id = outcome.webRtcSessionId;
                    if (!Number.isInteger(id)) throw Error("Provider did not return a session ID");
                    confirmation.resolve({ id, fabricIndex: fabric.fabricIndex, peerNodeId: camera, peerEndpointId: 1,
                        streamUsage: 3, videoStreams: [request.video], metadataEnabled: false });
                    const remote = await signal;
                    if (remote.id !== id) throw Error("Signal belongs to another session");
                    reply(remote);
                } finally {
                    confirmation.resolve(null);
                    offer = undefined;
                }
            } else if (request.op === "lastEnd") {
                reply(lastEnd ?? {});
            } else if (request.op === "end") {
                await invoke("endSession", { webRtcSessionId: request.id, reason: 2 });
                await endpoint.act(agent => agent.get(WebRtcTransportRequestorServer).removeSession({
                    id: request.id, fabricIndex: fabric.fabricIndex, peerNodeId: camera }));
                reply({ ended: true });
            } else throw Error("Unknown viewer operation");
        }
    } finally {
        await node.close();
    }
}

// matter.js normally logs to stdout. Route it to the fixture's private diagnostic file.
console.log = console.info = console.debug = console.warn = console.error.bind(console);
main().catch(error => { console.error(error); process.exitCode = 1; });
