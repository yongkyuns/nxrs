# Switch an Nxrs LED from Home Assistant

Switch a simulated LED on and off from the Home Assistant dashboard. The
example runs on a Linux or macOS computer and appears as a Matter light.
It uses the same LED output and controller as the
[controller tutorial](../../guide/controller.md). A network command goes
through the controller's queue to that output. Home Assistant reads the
value confirmed by the controller.

| Piece | What it does |
| --- | --- |
| Home Assistant | Adds the light, sends On and Off, and displays its reported state. |
| Matter light example | Receives Matter commands over your home network and translates them into the controller's commands. |
| Nxrs controller and LED | The controller owns the LED output. This example uses the mock output, so the state changes without a physical board. |

## Start the light

You need an Nxrs checkout, Rust 1.98.1, and Home Assistant with its Matter
integration and Matter Server running. Use the same local network with IPv6
and multicast enabled for Home Assistant, your phone, and the computer
running the example. The example uses the host's existing IP connection.
[Home Assistant’s Matter setup instructions](https://www.home-assistant.io/integrations/matter/#prerequisites).

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

The first run builds the pinned Matter SDK. Leave the terminal open after it
prints the pairing information and `LED off`. The pairing window lasts three
minutes. If it expires before pairing, stop with Ctrl+C and repeat the
command to get a fresh code.

## Pair and switch the light

1. Open the Home Assistant Companion app. Go to **Settings → Connectivity →
   Matter → Add device** and choose **No. It's new**.
2. Scan the terminal's QR code or enter the manual code shown beside it. Use
   the test-device option if the app prompts about an uncertified accessory.
   Keep the setup code and state directory private.
3. Open **NXRS Simulated LED** on its device page. Toggle the light on,
   then off. The entity should follow the switch, and the terminal should
   show `LED on` and `LED off`. You can also add the light to a dashboard
   or call `light.turn_on` and `light.turn_off` in an automation.
   [Home Assistant pairing steps](https://www.home-assistant.io/integrations/matter/#adding-a-matter-device-to-home-assistant).

If the device is missing, check that the computer and controller share the
same LAN, that the firewall allows local Matter traffic on port 5540 and
multicast discovery, and that the example has a current pairing window.
Pause any other local Matter device already using port 5540, or run this
example on another computer. This example uses development attestation.
Use a controller that permits Matter test devices for this experiment.

To stop, press Ctrl+C. The example closes the network service and switches
the simulated output off before joining its worker. Start again with the
same directory and omit `--pair` to reuse the enrollment. The LED starts
off on each run. Give Home Assistant a moment to reconnect before switching it. To start with a new identity, remove the old light from
Home Assistant and choose a new state-directory path.

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
