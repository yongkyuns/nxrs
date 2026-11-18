// An independent, commissionable Matter clock for the bridge time-client qualification.
"use strict";

const { rename, writeFile } = require("node:fs/promises");
const { createRequire } = require("node:module");
const { join } = require("node:path");

async function main() {
    const [install, storage, iface, port, onboarding] = process.argv.slice(2);
    const sdk = createRequire(join(install, "package.json"));
    const { Environment, ServerNode } = sdk("@matter/main");
    const { TimeSynchronizationServer } = sdk("@matter/main/behaviors/time-synchronization");
    class Clock extends TimeSynchronizationServer {
        setUtcTime({ utcTime, granularity }) {
            this.state.utcTime = utcTime;
            this.state.granularity = granularity;
        }
    }

    const environment = Environment.default;
    environment.vars.set("storage.path", storage);
    environment.vars.set("mdns.networkInterface", iface);
    const node = await ServerNode.create(ServerNode.RootEndpoint.with(Clock), {
        id: "time-source",
        environment,
        network: { port: Number(port), ble: false, tcp: true },
        dcl: { offline: true },
        productDescription: { deviceType: 0x0016 },
        timeSynchronization: { utcTime: null, granularity: 0 },
    });
    const stopped = new Promise(resolve => {
        process.once("SIGTERM", resolve);
        process.once("SIGINT", resolve);
    });
    try {
        await node.start();
        const temporary = onboarding + ".next";
        await writeFile(temporary, JSON.stringify({
            code: node.state.commissioning.pairingCodes.qrPairingCode,
        }), { mode: 0o600, flag: "wx" });
        await rename(temporary, onboarding);
        await stopped;
    } finally {
        await node.close();
    }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
