# Switch an Nxrs LED from Home Assistant

Run a Matter light on your computer and switch it from Home Assistant. The
light uses the simulated LED from [Build a controller](../../guide/controller.md),
so you can follow the example without a board.

| Piece | What it does |
| --- | --- |
| Home Assistant | Adds the light, sends On and Off commands, and displays the reported state. |
| Matter light example | Receives commands over the local network and calls the controller. |
| NXRS controller | Switches the simulated LED and reports the result to the Matter light. |

## Start the light

You’ll need Home Assistant with its Matter integration and Matter Server
running, the Companion app on your phone, and an NXRS checkout on Linux or
macOS. Connect all three to the same local network with IPv6 and multicast
enabled. [Home Assistant’s Matter setup instructions](https://www.home-assistant.io/integrations/matter/#prerequisites).

On Debian or Ubuntu, install `build-essential`, `pkg-config`,
`avahi-daemon`, and `libavahi-compat-libdnssd-dev`. Make sure the Avahi
service is running.
macOS uses its built-in Bonjour service.

Run these commands from the root of the Nxrs checkout.

```sh
rustup toolchain install 1.98.1 --profile minimal
led_state="$HOME/.nxrs-matter-led"
NXRS_PLATFORM=mock cargo +1.98.1 run --release --locked \
  --manifest-path website/examples/matter-led/Cargo.toml -- "$led_state" --pair
```

The first run builds the pinned Matter SDK. The example prints the QR and
manual codes in the terminal even when Rust logging is off. Leave the
terminal open while you pair. The pairing window lasts three minutes. If it
expires, stop with Ctrl+C and rerun with `--pair` to get a fresh code.

## Pair and switch the light

1. Open the Home Assistant Companion app. Go to **Settings → Connectivity →
   Matter → Add device** and choose **No. It's new**.
2. Scan the terminal's QR code or enter the manual code shown beside it. Use
   the test-device option if the app prompts about an uncertified accessory.
   Keep the setup code and state directory private.
3. Once the terminal shows `Ready to switch the light.`, open
   **NXRS Simulated LED** and toggle it on, then off. The entity should
   follow the switch, and the terminal should show `LED on` and `LED off`.
   You can also add the light to a dashboard or call `light.turn_on` and
   `light.turn_off` in an automation.
   [Home Assistant pairing steps](https://www.home-assistant.io/integrations/matter/#adding-a-matter-device-to-home-assistant).

If the light doesn’t appear, check for an active pairing window and make
sure the host firewall allows local Matter traffic on port 5540 and
multicast discovery. If another local Matter device already uses port 5540,
run this example on a different computer. The controller must accept
development attestation to pair this light.

## Stop and restart

Press Ctrl+C to close the Matter service, switch the simulated LED off,
and join the worker. To reuse the pairing, run the same command with the
same state directory and omit `--pair`. The light starts off and waits
for Home Assistant to receive its first Matter state report. When the
terminal prints `Ready to switch the light.`, use the same device page
to switch it again.

To pair again with a fresh identity, remove the old light from Home
Assistant and choose a new state-directory path.

## Adapt it to your own project

1. Start with the mock output. Give the example your project's device name
   in the `DEVICE` settings. Pair with a new state directory when you want
   Home Assistant to discover it as a separate test device.
2. Replace the output at `Controller::start`. For a binary output, provide
   a type that implements the [LED capability](../../../hal/led/src/lib.rs).
   Its `set` operation should report whether it succeeded, and `supported`
   identifies the available outputs. If the output lives on a NuttX board,
   build and test that board with its [hardware profile](../../guide/profiles.md).
   Keep this Matter process on the host. Have your host output type send a
   command to the board and return success after the board confirms it.
3. Keep the [Matter adapter](src/main.rs) at the network boundary. It sends
   the controller's typed `Set` command for On or Off and `Status` for reads.
   A failed write returns a Matter error. The handler waits for completion
   because the mock output is immediate. If your output can take a long
   time, use asynchronous completion so it does not stall the Matter loop.
4. Repeat the same test through Home Assistant. Confirm On, confirm Off,
   and exercise a failed command and a host restart. For a different kind
   of device, choose the Matter device type and its required clusters
   before testing it with your intended controller.

See [Build a controller](../../guide/controller.md) for queue admission and
shutdown, and the [Matter integration guide](../../../gateway/matter/integration.md)
for how this pattern fits into a larger system.
