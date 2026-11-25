// Generate independent Matter onboarding material for the pinned test device.
// Keep onboarding.json with the operator. The responder needs only factory.json.
"use strict";

const { createRequire } = require("node:module");
const { mkdirSync, writeFileSync } = require("node:fs");
const { randomBytes, randomInt } = require("node:crypto");
const { isAbsolute, join } = require("node:path");

async function main() {
    const [install, output] = process.argv.slice(2);
    if (process.argv.length !== 4 || !isAbsolute(install) || !isAbsolute(output)) {
        throw Error("Expected absolute controller install and new output directory");
    }
    const fromController = createRequire(join(install, "package.json"));
    const { NodeJsCrypto } = fromController("@matter/nodejs");
    const { PaseClient } = fromController("@matter/protocol");
    const { CommissioningFlowType, DiscoveryCapabilitiesSchema, QrPairingCodeCodec, ManualPairingCodeCodec } =
        fromController("@matter/types");
    const crypto = new NodeJsCrypto();
    const passcode = PaseClient.generateRandomPasscode(crypto);
    const discriminator = randomInt(4096);
    const salt = randomBytes(32);
    const iterations = 10000;
    const verifier = await PaseClient.generatePakePasscodeVerifier(crypto, passcode, { salt, iterations });
    // These VID/PID match the SDK's development attestation chain and Basic Information.
    const identity = { vendorId: 0xfff1, productId: 0x8001, flowType: CommissioningFlowType.UserIntent,
        discriminator, passcode };
    const qrCode = QrPairingCodeCodec.encode([{ ...identity, version: 0,
        discoveryCapabilities: DiscoveryCapabilitiesSchema.encode({ onIpNetwork: true }) }]);
    const manualCode = ManualPairingCodeCodec.encode(identity);

    mkdirSync(output, { mode: 0o700 });
    const write = (name, data) => writeFileSync(join(output, name), JSON.stringify(data) + "\n",
        { flag: "wx", mode: 0o600 });
    write("factory.json", { version: 1, discriminator, iterations, salt: salt.toString("base64"),
        verifier: Buffer.from(verifier).toString("base64") });
    write("onboarding.json", { qrCode, manualCode });
}

main().catch(() => {
    process.stderr.write("Could not provision the requested new private directory.\n");
    process.exitCode = 1;
});
