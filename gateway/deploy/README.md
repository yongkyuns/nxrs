# VPN-only Linux gateway deployment

For #20 / draft #21. This is an **opt-in single-host IPv4 deployment profile**, not
an installer, cloud service, or completed surveillance product. Nothing here
changes camera acquisition, flashes hardware, or modifies the iPhone trust policy.
The generator writes a NEW private directory only. It never installs packages,
starts services, changes routes/firewalls, or installs certificates.

## Topology and trust

The phone runs the WireGuard app separately from Nxrs Monitor. One Linux gateway
hosts the WireGuard endpoint, nginx, Rust control service and MediaMTX:

| Boundary | Default binding / policy |
| --- | --- |
| Internet | UDP 51820 WireGuard only; home router forwards only this port |
| Tunnel | Gateway `10.77.0.1/32`, phone `10.77.0.2/32`; choose unused RFC1918 addresses |
| HTTPS API / WHEP | nginx on gateway tunnel address, TCP 8443, valid trusted TLS |
| WebRTC media | MediaMTX on gateway tunnel address, UDP 8189; no LAN interface discovery or public STUN |
| Control / internal authorization | Loopback TCP 18080 / 18081; never proxied wholesale |
| WHEP backend / RTSP publisher | Loopback TCP 8889 / 8554; never publicly forwarded |

The generated nftables table rejects unencrypted traffic addressed to the tunnel
IP, restricts incoming tunnel traffic to the two intended ports, prevents tunnel
forwarding to/from the LAN, and prevents traffic with the tunnel source address
leaking onto another interface. Binding to a VPN IP alone would not enforce this.
It atomically replaces only `inet nxrs_monitor`, never the host's other rules.
Existing default-deny firewall chains still need to allow the intended UDP 51820
and tunnel TCP 8443/UDP 8189 traffic: an accept here does not override their drops.
No NAT, default route, DNS override, or global IP-forwarding change is installed.
The fixed `PreUp` firewall command fails tunnel startup when its config is absent.
There is deliberately no firewall deletion on shutdown.

WireGuard membership is NOT viewer authorization. Viewer, publisher and media
credentials remain separate. The gateway still has development-only in-memory
identity/revocation: **do not deploy this profile as a production security claim**.
Established media is not yet terminated by application-token revocation. Physical
ingest security, persistent revocation, resource/soak qualification and dependency
review remain gates. The four-reader MediaMTX path cap is not a per-user quota.

## Generate (no host changes)

Prerequisites for generation: Python 3 and `wg` from wireguard-tools. Target profile:
Linux with systemd credentials support (Ubuntu 24.04 is the CI target), nginx,
nftables, the locked Rust service and checksum-verified MediaMTX 1.21.2. OS packages
must receive security updates; the workflow records versions, not a reproducible
OS image. The pinned Linux MediaMTX asset currently covers amd64 only.

Create the phone tunnel in WireGuard, keeping its generated PRIVATE key on the
phone. Export only its PUBLIC key. From the repository root:

```sh
umask 077
python3 gateway/deploy/vpn.py /tmp/nxrs-vpn-enrollment \
  --host monitor.your-domain.example \
  --gateway 10.77.0.1 --peer 10.77.0.2 \
  --endpoint home.your-domain.example \
  --peer-public-key 'PUBLIC_KEY_FROM_THE_PHONE'
```

Replace the example DNS names and public key. The `--host` DNS name must resolve to
the gateway's tunnel address on the phone and match the TLS certificate. The
public `--endpoint` is the reachable HOME address used for WireGuard, not the app's
HTTPS address. A public, automatically renewed certificate obtained through
DNS-01 can avoid opening port 80. Certificate issuance/renewal is deliberately
not automated here; do not distribute a private CA key or disable validation.
A privately managed CA requires explicit iOS trust enrollment and separate tests.
Using an IP as `--host` requires that exact IP in the trusted certificate's SAN.

The generator refuses overwrite and invalid inputs. It reuses the existing
reference enrollment generator, preserving stable camera IDs and synthetic labels.
Every output is mode 0600 under a 0700 directory. `viewer.token` goes only to app
enrollment; `publisher.token` goes only to the trusted local ingest process.
`wg-nxrs.conf` contains the GATEWAY private key. `phone-settings.txt` contains the
server public key and the narrow `/32` phone route; no phone private key is created
or collected. Never paste these private files into issues or CI artifacts.
A failed/partial generation must not be deployed; start again in a new directory.

## Explicit installation and startup

Review generated configuration and route conflicts first. These are manual,
privileged deployment actions on a host you administer, not performed by the
generator or chat. Build the Rust binary with `cargo build --release --locked`
inside `gateway/`. Reproduce MediaMTX with the frozen patched Go graph and pass
the source/binary advisory gates using the [media build procedure](media-engine.md).
Verify the executable and all three public sidecars before installation. Install
those binaries at `/usr/local/libexec/nxrs-gateway-control` and
`/usr/local/libexec/nxrs-mediamtx`, root-owned and executable.

Place the reviewed `config.json`, `mediamtx.json`, `nginx.conf`, and `firewall.nft`
under `/etc/nxrs-monitor` (0700 directory / 0600 files), and the certificate chain
and private key at `/etc/nxrs-monitor/tls/fullchain.pem` and `privkey.pem` (0600).
Place `wg-nxrs.conf` at `/etc/wireguard/wg-nxrs.conf` (0600), and the four generated
`.service` files under `/etc/systemd/system` (0644). Keep token files out of
service configuration directories unless explicitly needed by the ingest owner.
Do not copy the generated output directory wholesale to a public web directory.

```sh
sudo nft -c -f /etc/nxrs-monitor/firewall.nft
sudo systemctl daemon-reload
sudo systemctl enable --now nxrs-firewall.service
sudo systemctl enable --now wg-quick@wg-nxrs.service
sudo systemctl enable --now nxrs-control.service nxrs-media.service nxrs-proxy.service
```

The three application services run as distinct unprivileged dynamic users and use
systemd `LoadCredential` for root-owned config/TLS files. They require the tunnel
and firewall; stopping the WireGuard unit stops them. The proxy has its own
private runtime directory, bounded sockets/requests/body sizes, no request logs,
and only exact catalog/grant/WHEP routes. Unknown routes, WHIP publishing,
query credentials, noncanonical URLs, method mismatches and extra trust headers
are rejected or stripped. WHEP session Locations remain relative and therefore
on the external HTTPS origin. No browser UI, CORS API or trickle PATCH is exposed.
MediaMTX logs are suppressed because upstream errors may contain capability URLs.
Use service status and the bounded health/catalog/media checks for diagnosis.
After certificate renewal restart `nxrs-proxy` so systemd reloads its credentials.

Configure the phone using `phone-settings.txt`; its Allowed IPs include only the
gateway `/32`, not the entire LAN or Internet. In Nxrs Monitor enter the trusted
HTTPS origin and viewer token. The app remains unchanged and VPN-agnostic.
No physical source is provisioned: the catalog entry remains SYNTHETIC until the
camera effort's qualified ingest adapter is integrated. The CI synthetic publisher
is a test fixture, not a daemon silently enabled on a home gateway.

### Reachability limits and revocation

This profile requires a reachable home UDP endpoint. For carrier-grade NAT, use
an explicitly designed public hub/outbound-home deployment; this generator does
not silently create one. Hosting/relay bandwidth may cost money. Routing, ICE
advertisement and the firewall must be qualified again for that topology. Stable
API identities and the player need not change for future hosted reachability.

For a lost phone, remove its peer from the persistent WireGuard config AND from
the running interface (`wg set wg-nxrs peer PUBLIC_KEY remove`); remove the viewer
credential from the control config and restart control/MediaMTX as documented in
[the gateway README](../README.md). Deleting the app's local Keychain record alone
is not server-side revocation. Keep application identity separate from VPN keys.

## Evidence and reproduction

```sh
python3 -m unittest discover -s gateway/tests -p test_vpn_deployment.py -v
```

The unit suite launches real nginx with private temporary TLS and an instrumented
loopback backend. It checks trusted/untrusted TLS, hostname validation, allowlisted
routes/methods, header filtering, body bounds, permissions and generated policy.
nginx/OpenSSL tests are skipped when those tools are absent; CI installs them.

The dedicated `monitoring-vpn.yml` job additionally executes `vpn_reference.py`
on an isolated hosted Linux runner as root. It creates TWO private network
namespaces linked by a veth pair, installs real kernel WireGuard and the generated
firewall, and runs the actual nginx/Rust/MediaMTX services. An aiortc receiver
inside the peer namespace must decode five advancing synthetic H.264 frames and
DELETE its session through the tunnel. It also checks denied unencrypted access,
denied private ports (including a deliberately listening test service), rejection
of VPN-only unauthenticated API access, preserved unrelated firewall tables, and
loss of reachability after removing the VPN peer. It changes no host routes or
system certificate trust. Namespace processes, keys and private logs are removed;
no raw artifacts are uploaded. Test FFmpeg credentials are visible to local root
in process arguments, so the harness refuses personal/non-hosted machines.

This is NOT evidence of physical iPhone rendering/cellular transitions, a router
port-forward, CGNAT traversal, service-manager boot/hardening enforcement, hardware
capture, sustained operation, or active application-session revocation. The
namespace test runs the same generated network/proxy/media policy but injects
private test certificate/runtime paths rather than starting the systemd units.
Check #21 for commit-specific executed results; adding a test is not a pass.

Primary references: [WireGuard](https://www.wireguard.com/quickstart/),
[nginx proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[nftables](https://netfilter.org/projects/nftables/manpage.html),
[systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html),
[MediaMTX 1.21.2 WHEP session locations](https://github.com/bluenviron/mediamtx/blob/v1.21.2/internal/servers/webrtc/http_server.go).
