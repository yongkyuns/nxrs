// Regenerate: node docs/assets/render-architecture.mjs
// Generate matching light and dark SVGs with editable labels and accessible descriptions.
import { readFileSync, writeFileSync } from 'node:fs';

const palettes = {
  light: {
    canvas: '#FCFDFE', surface: '#FFFFFF', group: '#F4F7FB', border: '#D7E1EB',
    groupBorder: '#CCD9E6', ink: '#203042', secondary: '#596B7D', line: '#8A9CAF',
    blue: '#73B9E2', orange: '#F08A4B', shadow: '#24374A',
  },
  dark: {
    canvas: '#10161F', surface: '#1A2533', group: '#141E2B', border: '#34465A',
    groupBorder: '#3B5066', ink: '#E5EDF5', secondary: '#ACBDCE', line: '#8199B1',
    blue: '#83C5EA', orange: '#F2A06A', shadow: '#000000',
  },
};

const edge = (from, to, d, dashed = false) =>
  `<path id="edge-${from}-${to}" data-from="${from}" data-to="${to}" d="${d}" class="edge"${dashed ? ' stroke-dasharray="4 6"' : ''}/>`;
const card = ({ id, x, y, w, h, title, subtitle, accent, size = 24 }) =>
  `<g id="node-${id}">
      <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="15" class="card" filter="url(#shadow)"/>
      ${accent ? `<rect x="${x + 20}" y="${y + 24}" width="4" height="${h - 48}" rx="2" fill="${accent}"/>` : ''}
      <text x="${x + w / 2 + (accent ? 8 : 0)}" y="${y + (subtitle ? 35 : h / 2 + 8)}" text-anchor="middle" class="label" font-size="${size}">${title}</text>
      ${subtitle ? `<text x="${x + w / 2 + (accent ? 8 : 0)}" y="${y + 65}" text-anchor="middle" class="subtitle">${subtitle}</text>` : ''}
    </g>`;

function definitions(p, subtitleSize = 19) {
  return `  <defs>
    <marker id="arrow" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto">
      <path d="M1 1 L6 4 L1 7" fill="none" stroke="${p.line}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </marker>
    <filter id="shadow" x="-10%" y="-20%" width="120%" height="145%">
      <feGaussianBlur in="SourceAlpha" stdDeviation="3" result="blur"/>
      <feOffset in="blur" dx="0" dy="3" result="offset-blur"/>
      <feFlood flood-color="${p.shadow}" flood-opacity="0.055" result="shadow-color"/>
      <feComposite in="shadow-color" in2="offset-blur" operator="in" result="card-shadow"/>
      <feMerge><feMergeNode in="card-shadow"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <style>
      text { font-family: Inter, "Helvetica Neue", Arial, sans-serif; }
      .card { fill: ${p.surface}; stroke: ${p.border}; stroke-width: 1.2; }
      .label { fill: ${p.ink}; font-weight: 500; letter-spacing: -0.4px; }
      .subtitle { fill: ${p.secondary}; font-size: ${subtitleSize}px; }
      .edge { fill: none; stroke: ${p.line}; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; marker-end: url(#arrow); }
      .edge-label { fill: ${p.secondary}; font-size: 16px; }
    </style>
  </defs>`;
}

function diagram(p) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="810" viewBox="0 0 1200 810" role="img" aria-labelledby="title description">
  <title id="title">NXRS architecture</title>
  <desc id="description">Platform profiles configure the reusable Rust firmware group at build time. Within that group, applications connect to services, capability HALs and chip drivers. The firmware group has two separate paths: ordinary Rust APIs through a standard library compiled for NuttX, and device access through Rust peripheral wrappers. Both paths lead to NuttX, which provides the POSIX C environment, RTOS and BSP on the embedded MCU.</desc>
${definitions(p)}

  <rect width="1200" height="810" rx="24" fill="${p.canvas}"/>

  <!-- Configuration is a dashed connection to the whole firmware group. -->
  ${card({ id: 'P', x: 352, y: 32, w: 496, h: 80, title: 'Platform profiles', subtitle: 'Devices · resources · roles' })}
  ${edge('P', 'F', 'M600 112 V170', true)}
  <text x="616" y="148" class="edge-label">build-time composition</text>

  <!-- Preserve the original application → service → HAL → driver topology. -->
  <g id="node-F">
    <rect x="48" y="172" width="1104" height="162" rx="21" fill="${p.group}" stroke="${p.groupBorder}" stroke-width="1.2"/>
    <text x="80" y="210" class="label" font-size="22">Reusable and composable Rust firmware</text>
    ${card({ id: 'A', x: 80, y: 244, w: 220, h: 60, title: 'Applications', size: 22 })}
    ${card({ id: 'S', x: 353, y: 244, w: 220, h: 60, title: 'Services', size: 22 })}
    ${card({ id: 'H', x: 626, y: 244, w: 220, h: 60, title: 'Capability HALs', size: 22 })}
    ${card({ id: 'D', x: 899, y: 244, w: 220, h: 60, title: 'Chip drivers', size: 22 })}
    ${edge('A', 'S', 'M300 274 H350')}
    ${edge('S', 'H', 'M573 274 H623')}
    ${edge('H', 'D', 'M846 274 H896')}
  </g>

  <!-- Two parallel paths from the firmware group, never one mandatory HAL path. -->
  ${edge('F', 'R', 'M340 334 V400')}
  <text x="356" y="373" class="edge-label">ordinary Rust APIs</text>
  ${edge('F', 'W', 'M860 334 V400')}
  <text x="876" y="373" class="edge-label">device access</text>
  ${card({ id: 'R', x: 150, y: 402, w: 380, h: 88, title: 'Rust std', subtitle: 'Compiled for NuttX', accent: p.blue, size: 25 })}
  ${card({ id: 'W', x: 670, y: 402, w: 380, h: 88, title: 'Rust peripheral wrappers', accent: p.orange, size: 24 })}

  <!-- Both Rust interfaces lead to the same operating-system foundation. -->
  ${edge('R', 'N', 'M340 490 V558')}
  ${edge('W', 'N', 'M860 490 V558')}
  ${card({ id: 'N', x: 280, y: 560, w: 640, h: 88, title: 'NuttX', subtitle: 'POSIX / C · RTOS · BSP', size: 26 })}
  ${edge('N', 'M', 'M600 648 V708')}
  ${card({ id: 'M', x: 450, y: 710, w: 300, h: 60, title: 'Embedded MCU', size: 22 })}
</svg>
`;
}

function eventCommunication(p) {
  // The navigation demo uses FusionConfig::default(), with a 16-slot inbox.
  const capacity = 16;
  const slots = Array.from({ length: capacity }, (_, i) =>
    `<rect x="${110 + i * 29}" y="221" width="25" height="23" rx="4" fill="${i < 4 ? p.blue : p.group}" stroke="${p.groupBorder}"/>`,
  ).join('\n    ');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="680" height="644" viewBox="0 0 680 644" role="img" aria-labelledby="title description">
  <title id="title">Event-driven communication in the navigation demo</title>
  <desc id="description">The IMU service, GNSS service and application control send typed events to a bounded inbox with sixteen slots reserved at setup. While running, the Fusion owner blocks at one receive point, handles one event to completion with its private state, and returns to the same wait.</desc>
${definitions(p, 22)}

  <rect width="680" height="644" rx="24" fill="${p.canvas}"/>
  ${card({ id: 'imu', x: 24, y: 28, w: 192, h: 86, title: 'IMU service', subtitle: 'IMU samples', size: 27 })}
  ${card({ id: 'gnss', x: 244, y: 28, w: 192, h: 86, title: 'GNSS service', subtitle: 'GNSS fixes', size: 27 })}
  ${card({ id: 'control', x: 464, y: 28, w: 192, h: 86, title: 'App control', subtitle: 'Stop request', size: 27 })}
  ${edge('imu', 'inbox', 'M120 114 V140 H214 V164')}
  ${edge('gnss', 'inbox', 'M340 114 V164')}
  ${edge('control', 'inbox', 'M560 114 V140 H466 V164')}

  <g id="node-inbox">
    <rect x="96" y="166" width="488" height="124" rx="15" class="card" filter="url(#shadow)"/>
    <text x="340" y="205" text-anchor="middle" class="label" font-size="29">Bounded inbox</text>
    ${slots}
    <text x="340" y="272" text-anchor="middle" class="subtitle">${capacity} slots reserved at setup</text>
  </g>

  <g id="node-owner">
    <rect x="96" y="332" width="488" height="288" rx="21" fill="${p.group}" stroke="${p.groupBorder}" stroke-width="1.2"/>
    <text x="122" y="369" class="label" font-size="28">Fusion owner</text>
    <text x="558" y="369" text-anchor="end" class="subtitle">Private state</text>
    ${card({ id: 'wait', x: 166, y: 390, w: 348, h: 80, title: 'One wait point', subtitle: 'Block in recv()', accent: p.blue, size: 28 })}
    ${card({ id: 'handle', x: 166, y: 514, w: 348, h: 80, title: 'Handle one event', subtitle: 'Run to completion', accent: p.orange, size: 28 })}
    ${edge('wait', 'handle', 'M340 470 V512')}
    ${edge('handle', 'wait', 'M514 554 H549 Q557 554 557 546 V438 Q557 430 549 430 H516')}
  </g>
  ${edge('inbox', 'wait', 'M340 290 V388')}
</svg>
`;
}

// Reader-guide figures use the overview's palette, rounded cards and connectors.
// The nominal width is the guide's reading width, so labels stay readable inline.
const xml = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
function panel({ id, x, y, w, h = 88, title, lines = [], accent }) {
  const labelY = lines.length ? y + 32 : y + h / 2 + 7;
  return `<g id="node-${id}" data-panel="true">
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="15" class="card" filter="url(#shadow)"/>
    ${accent ? `<rect x="${x + 15}" y="${y + 20}" width="4" height="${h - 40}" rx="2" fill="${accent}"/>` : ''}
    <text x="${x + w / 2}" y="${labelY}" text-anchor="middle" class="label" font-size="22">${xml(title)}</text>
    ${lines.map((line, i) => `<text x="${x + w / 2}" y="${y + 59 + i * 25}" text-anchor="middle" class="subtitle">${xml(line)}</text>`).join('\n    ')}
  </g>`;
}
function guideFigure(p, title, description, height, body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="800" height="${height}" viewBox="0 0 800 ${height}" role="img" aria-labelledby="title description">
  <title id="title">${xml(title)}</title>
  <desc id="description">${xml(description)}</desc>
${definitions(p, 18)}
  <rect width="800" height="${height}" rx="24" fill="${p.canvas}"/>
${body}
</svg>\n`;
}
const label = (x, y, text, anchor = 'middle') => `<text x="${x}" y="${y}" text-anchor="${anchor}" class="edge-label">${xml(text)}</text>`;
const group = (p, id, x, y, w, h, title) => `<g id="group-${id}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="21" fill="${p.group}" stroke="${p.groupBorder}" stroke-width="1.2"/><text x="${x + 24}" y="${y + 35}" class="label" font-size="21">${xml(title)}</text></g>`;

function buildWorkflow(p) {
  return guideFigure(p, 'From source inputs to a NuttX firmware image',
    'An application manifest, a profile with ordered overlays, and pinned native sources enter the host build controller. It resolves configuration, prepares the compiler and native sources, asks Cargo for selected dependencies, prepares generated instances and native helpers, compiles and links, then checks the actual image and writes build records. Application behavior remains in handwritten Rust.', 756, `
  ${panel({ id: 'app', x: 24, y: 24, w: 236, title: 'Application', lines: ['Manifest · Rust source'], accent: p.blue })}
  ${panel({ id: 'profile', x: 282, y: 24, w: 236, title: 'Profile + overlays', lines: ['Board · resources'], accent: p.orange })}
  ${panel({ id: 'sources', x: 540, y: 24, w: 236, title: 'Pinned sources', lines: ['NuttX · Rust · patches'] })}
  ${edge('app', 'resolve', 'M142 112 V134 H276 V156', true)}
  ${edge('profile', 'resolve', 'M400 112 V156', true)}
  ${edge('sources', 'resolve', 'M658 112 V134 H524 V156', true)}
  ${panel({ id: 'resolve', x: 120, y: 158, w: 560, title: 'Resolve the build', lines: ['Entry settings · capability checks · configuration'] })}
  ${edge('resolve', 'prepare', 'M400 246 V284')}
  ${panel({ id: 'prepare', x: 120, y: 286, w: 560, h: 112, title: 'Prepare selected inputs', lines: ['Compiler and sources → Cargo dependency graph', 'Instances · native headers and helpers'], accent: p.blue })}
  ${edge('prepare', 'compile', 'M400 398 V436')}
  ${panel({ id: 'compile', x: 120, y: 438, w: 560, title: 'Compile and link', lines: ['Cargo builds Rust · NuttX builds the native image'] })}
  ${edge('compile', 'inspect', 'M400 526 V564')}
  ${panel({ id: 'inspect', x: 120, y: 566, w: 560, title: 'Inspect the actual outputs', lines: ['Effective configuration · ABI · symbols · image hashes'], accent: p.orange })}
  ${edge('inspect', 'image', 'M276 654 V676 H210 V694')}
  ${edge('inspect', 'records', 'M524 654 V676 H590 V694')}
  ${panel({ id: 'image', x: 24, y: 696, w: 372, h: 40, title: 'Firmware image' })}
  ${panel({ id: 'records', x: 420, y: 696, w: 356, h: 40, title: 'Build records + receipts' })}
`);
}

function profileComposition(p) {
  return guideFigure(p, 'Profiles bind resources, devices and logical names',
    'A base profile and ordered overlays resolve into resources, physical devices, and aliases. Shared tooling derives native configuration from resource bindings and generates canonical device instances and alias accessors. NuttX registers the resources. Multiple aliases borrow the same device identity instead of allocating another device. The figure describes composition; application behavior is separate.', 664, `
  ${panel({ id: 'profile', x: 160, y: 24, w: 480, title: 'Base profile + ordered overlays', lines: ['Resolve and validate at build time'] })}
  ${group(p, 'declarations', 24, 160, 752, 182, 'Resolved composition')}
  ${panel({ id: 'resource', x: 44, y: 220, w: 218, title: 'Resource', lines: ['Bus · endpoint · pins'] })}
  ${panel({ id: 'device', x: 290, y: 220, w: 218, title: 'Device', lines: ['Part · bus · address'], accent: p.blue })}
  ${panel({ id: 'alias', x: 536, y: 220, w: 218, title: 'Alias', lines: ['Logical product name'] })}
  ${edge('profile', 'declarations', 'M400 112 V158', true)}
  ${edge('device', 'resource', 'M290 264 H264')}
  ${edge('alias', 'device', 'M536 264 H510')}
  ${edge('resource', 'bsp', 'M153 308 V426', true)}
  ${edge('device', 'instance', 'M399 308 V426', true)}
  ${edge('alias', 'accessor', 'M645 308 V426', true)}
  ${label(169, 388, 'Native settings', 'start')}
  ${label(415, 388, 'Generated identity', 'start')}
  ${label(661, 388, 'Generated name', 'start')}
  ${panel({ id: 'bsp', x: 44, y: 428, w: 218, h: 112, title: 'NuttX BSP', lines: ['Initializes hardware', 'Registers resources'] })}
  ${panel({ id: 'instance', x: 290, y: 428, w: 218, h: 112, title: 'Device instance', lines: ['One opaque object', 'Exclusive acquisition'], accent: p.blue })}
  ${panel({ id: 'accessor', x: 536, y: 428, w: 218, h: 112, title: 'Alias accessor', lines: ['Borrows the same', 'device instance'] })}
  ${edge('accessor', 'instance', 'M536 484 H510')}
  ${label(400, 586, 'Dashed: build-time configuration. Solid: refers to.')}
  ${label(400, 616, 'One driver can serve multiple independently configured devices.')}
`);
}

function interfaceBoundaries(p) {
  return guideFigure(p, 'Application-facing interfaces and implementation boundaries',
    'The application constructs reusable services. Application and service code may call a capability HAL or use ordinary Rust standard-library APIs directly. A capability selects its provider below the portable interface. The provider may use a chip driver, a resource adapter, or standard-library operations. On an MCU these paths reach the NuttX operating system and BSP. Platform profiles select configuration without becoming a runtime layer.', 688, `
  ${group(p, 'portable', 24, 24, 752, 166, 'Portable application code')}
  ${panel({ id: 'app', x: 54, y: 90, w: 300, h: 68, title: 'Application main()' })}
  ${panel({ id: 'service', x: 444, y: 90, w: 300, h: 68, title: 'Reusable services' })}
  ${edge('app', 'service', 'M354 124 H442')}
  ${edge('portable', 'hal', 'M214 190 V252')}
  ${edge('portable', 'std', 'M600 190 V252')}
  ${label(232, 226, 'Subsystem behavior', 'start')}
  ${label(618, 226, 'Ordinary Rust APIs', 'start')}
  ${panel({ id: 'hal', x: 44, y: 254, w: 340, title: 'Capability HAL', lines: ['Types · errors · ownership contract'], accent: p.blue })}
  ${panel({ id: 'std', x: 444, y: 254, w: 312, title: 'Rust std', lines: ['Files · sockets · threads'], accent: p.orange })}
  ${edge('hal', 'provider', 'M214 342 V404')}
  ${panel({ id: 'provider', x: 44, y: 406, w: 340, title: 'Selected provider', lines: ['Acquisition · cleanup · native I/O'] })}
  ${panel({ id: 'driver', x: 444, y: 406, w: 312, title: 'Driver / resource adapter', lines: ['Device protocol · bus operations'] })}
  ${edge('provider', 'driver', 'M384 450 H442')}
  ${edge('provider', 'os', 'M214 494 V576')}
  ${edge('driver', 'os', 'M600 494 V576')}
  ${edge('std', 'os', 'M756 298 H778 V548 H692 V576')}
  ${panel({ id: 'os', x: 104, y: 578, w: 592, title: 'NuttX + BSP', lines: ['Scheduling · device registration · hardware'], accent: p.blue })}
`);
}

function ownerEventLoop(p) {
  const slots = Array.from({ length: 10 }, (_, i) => `<rect x="${225 + i * 36}" y="238" width="30" height="20" rx="4" fill="${i < 3 ? p.blue : p.group}" stroke="${p.groupBorder}"/>`).join('\n');
  return guideFigure(p, 'One owner, one bounded inbox, one wait point',
    'Commands and peer or I/O events enter one bounded inbox. A single owner waits for the next message or its next deadline, updates private state synchronously, and submits outputs with a declared capacity policy before returning to the wait. Interrupt notifications are deferred to task context before this path. A deadline may expire the wait without enqueuing another message.', 768, `
  ${panel({ id: 'commands', x: 44, y: 24, w: 328, title: 'Commands', lines: ['Application control'] })}
  ${panel({ id: 'events', x: 428, y: 24, w: 328, title: 'Peer and I/O events', lines: ['Delivered in task context'] })}
  ${edge('commands', 'inbox', 'M208 112 V140 H280 V166')}
  ${edge('events', 'inbox', 'M592 112 V140 H520 V166')}
  ${panel({ id: 'inbox', x: 160, y: 168, w: 480, h: 128, title: 'Bounded typed inbox', lines: ['Capacity and full-queue policy are explicit'] })}
  ${slots}
  ${group(p, 'owner', 64, 336, 672, 408, 'One owner · private mutable state')}
  ${panel({ id: 'wait', x: 132, y: 392, w: 520, title: 'Wait for an event or deadline', lines: ['recv() or recv_timeout()'], accent: p.blue })}
  ${panel({ id: 'handle', x: 132, y: 516, w: 520, title: 'Handle one event', lines: ['Synchronous, bounded state update'], accent: p.orange })}
  ${panel({ id: 'output', x: 176, y: 640, w: 432, h: 76, title: 'Submit outputs', lines: ['Apply the chosen delivery policy'] })}
  ${edge('inbox', 'wait', 'M400 296 V390')}
  ${edge('wait', 'handle', 'M400 480 V514')}
  ${edge('handle', 'output', 'M400 604 V638')}
  ${edge('output', 'wait', 'M608 678 H700 V436 H654')}
`);
}

function ownerLifecycle(p) {
  return guideFigure(p, 'Explicit startup and shutdown ownership',
    'Allocate bounded resources and construct the owners, then start producers. During operation the owner tracks accepted work. To stop, close admission, request cancellation, wake blocked waits and join the owners that successfully started. Release resources only after cleanup completes. A cleanup failure retains ownership and an observable error for the capability-specific retry or quarantine policy.', 836, `
  ${panel({ id: 'construct', x: 160, y: 24, w: 480, title: 'Construct bounded resources', lines: ['Inboxes · buffers · handles'] })}
  ${panel({ id: 'start', x: 160, y: 148, w: 480, title: 'Start producers and owners', lines: ['Track which components started'], accent: p.blue })}
  ${panel({ id: 'run', x: 160, y: 272, w: 480, title: 'Process accepted work', lines: ['Each owner maintains its state'] })}
  ${panel({ id: 'cancel', x: 160, y: 396, w: 480, title: 'Close admission and request stop', lines: ['Complete or cancel admitted work'], accent: p.orange })}
  ${panel({ id: 'join', x: 160, y: 520, w: 480, h: 112, title: 'Wake blocked waits and join', lines: ['Stop callbacks and resource access', 'Attempt every started owner’s cleanup'] })}
  ${edge('construct', 'start', 'M400 112 V146')}
  ${edge('start', 'run', 'M400 236 V270')}
  ${edge('run', 'cancel', 'M400 360 V394')}
  ${edge('cancel', 'join', 'M400 484 V518')}
  ${edge('join', 'release', 'M280 632 V664 H206 V702')}
  ${edge('join', 'retain', 'M520 632 V664 H594 V702')}
  ${label(224, 688, 'Cleanup completed', 'start')}
  ${label(612, 688, 'Cleanup failed', 'start')}
  ${panel({ id: 'release', x: 24, y: 704, w: 364, h: 108, title: 'Release resources', lines: ['No producer can access them', 'Shutdown is complete'], accent: p.blue })}
  ${panel({ id: 'retain', x: 412, y: 704, w: 364, h: 108, title: 'Retain ownership', lines: ['Keep the failure observable', 'Apply retry / quarantine policy'], accent: p.orange })}
`);
}

function controllerCommand(p) {
  return guideFigure(p, 'Controller command completion and joined shutdown',
    'The application owns the only command sender. try_command attempts admission to an inbox with two waiting slots and returns a completion ticket when accepted. A single controller thread owns the LED and software-tracked state, handles each command synchronously, and sends one result to its ticket. Ticket.wait observes handling completion, not electrical readback. stop consumes the controller, drops its sole sender, drains accepted commands, requests the output off, and joins the worker. The returned Stopped object retains the output and final state even when the final off request fails. Synchronous Led.set has no cancellation or elapsed-time guarantee.', 944, `
  ${group(p, 'command', 24, 24, 752, 448, 'Follow one command to its result')}
  ${panel({ id: 'caller', x: 44, y: 88, w: 304, h: 112, title: 'Application caller', lines: ['Owns the sole sender', 'try_command(command)'] })}
  ${panel({ id: 'inbox', x: 452, y: 88, w: 304, h: 112, title: 'Bounded inbox', lines: ['2 waiting commands', 'Reject when full'], accent: p.blue })}
  ${panel({ id: 'ticket', x: 44, y: 288, w: 304, h: 112, title: 'Completion ticket', lines: ['Ticket::wait()', 'State or device error'], accent: p.blue })}
  ${panel({ id: 'owner', x: 452, y: 288, w: 304, h: 112, title: 'Controller owner', lines: ['One thread · state + LED', 'Led::set(...) or Status'], accent: p.orange })}
  ${edge('caller', 'inbox', 'M348 144 H450')}
  ${label(400, 126, 'try_send')}
  ${edge('inbox', 'owner', 'M604 200 V286')}
  ${label(620, 248, 'recv()', 'start')}
  ${edge('owner', 'ticket', 'M452 344 H350')}
  ${label(400, 326, 'reply')}
  ${edge('ticket', 'caller', 'M196 288 V202')}
  ${label(212, 248, 'wait returns', 'start')}
  ${label(400, 444, 'Status is software-tracked; it does not read the pin.')}

  ${group(p, 'shutdown', 24, 508, 752, 376, 'stop() consumes the controller')}
  ${panel({ id: 'close', x: 44, y: 568, w: 218, h: 112, title: 'Close admission', lines: ['Drop the sole sender', 'No stop message'], accent: p.orange })}
  ${panel({ id: 'drain', x: 290, y: 568, w: 218, h: 112, title: 'Drain the inbox', lines: ['Finish admitted work', 'Then recv() disconnects'] })}
  ${panel({ id: 'off', x: 536, y: 568, w: 218, h: 112, title: 'Request output off', lines: ['One final Led::set', 'Track its result'] })}
  ${edge('close', 'drain', 'M262 624 H288')}
  ${edge('drain', 'off', 'M508 624 H534')}
  ${panel({ id: 'joined', x: 132, y: 744, w: 536, h: 112, title: 'join() returns Stopped<L>', lines: ['Output ownership returns after the worker ends', 'An off failure keeps the output and its error'], accent: p.blue })}
  ${edge('off', 'joined', 'M645 680 V710 H400 V742')}
  ${label(400, 920, 'A synchronous Led::set call has no shutdown deadline.')}
`);
}

function navigationDataflow(p) {
  return guideFigure(p, 'Acquisition and service ownership in the navigation example',
    'IMU IRQ or HAL timer readiness wakes a HAL worker that drains the FIFO and produces typed samples. GNSS UART readiness wakes a HAL worker that reads and parses UBX into typed fixes. The corresponding sensor services each own an event inbox and forward normalized events to the fusion owner. Commands enter the service inboxes. No service handler runs inline in the acquisition producer or interrupt.', 756, `
  ${panel({ id: 'imu-wake', x: 64, y: 24, w: 300, title: 'IMU readiness', lines: ['IRQ or HAL timer'] })}
  ${panel({ id: 'gnss-wake', x: 436, y: 24, w: 300, title: 'GNSS readiness', lines: ['UART readable'] })}
  ${panel({ id: 'imu-hal', x: 64, y: 172, w: 300, title: 'IMU acquisition worker', lines: ['FIFO drain · typed Sample'], accent: p.blue })}
  ${panel({ id: 'gnss-hal', x: 436, y: 172, w: 300, title: 'GNSS acquisition worker', lines: ['Read / UBX parse · typed Fix'], accent: p.blue })}
  ${panel({ id: 'imu-service', x: 64, y: 324, w: 300, title: 'ImuService', lines: ['Own inbox · local processing'] })}
  ${panel({ id: 'gnss-service', x: 436, y: 324, w: 300, title: 'GnssService', lines: ['Own inbox · local processing'] })}
  ${edge('imu-wake', 'imu-hal', 'M214 112 V170')}
  ${edge('gnss-wake', 'gnss-hal', 'M586 112 V170')}
  ${edge('imu-hal', 'imu-service', 'M214 260 V322')}
  ${edge('gnss-hal', 'gnss-service', 'M586 260 V322')}
  ${label(400, 296, 'Bounded task-context delivery')}
  ${panel({ id: 'fusion', x: 224, y: 500, w: 352, h: 100, title: 'FusionService', lines: ['One inbox · private state'], accent: p.orange })}
  ${edge('imu-service', 'fusion', 'M214 412 V456 H312 V498')}
  ${edge('gnss-service', 'fusion', 'M586 412 V456 H488 V498')}
  ${panel({ id: 'commands', x: 240, y: 656, w: 320, h: 76, title: 'Application commands', lines: ['Typed service inputs'] })}
  ${edge('commands', 'imu-service', 'M240 694 H24 V368 H62')}
  ${edge('commands', 'gnss-service', 'M560 694 H776 V368 H738')}
  ${edge('commands', 'fusion', 'M400 656 V602')}
`);
}

function serviceDependencies(p) {
  return guideFigure(p, 'Capability dependencies in portable service qualification',
    'The camera-support test composition depends on camera, recording and telemetry services. Each service uses its corresponding camera, storage or transport capability contract. Separately, navigation services depend on IMU and GNSS capability contracts, each with a build-selected provider. Production applications do not depend on the camera-support fixture.', 744, `
  ${group(p, 'test', 24, 24, 752, 392, 'Camera qualification fixture')}
  ${panel({ id: 'fixture', x: 188, y: 80, w: 424, h: 68, title: 'tests/camera-support' })}
  ${panel({ id: 'camera-service', x: 44, y: 204, w: 218, title: 'Camera service', lines: ['nxrs-camera-service'] })}
  ${panel({ id: 'recording-service', x: 290, y: 204, w: 218, title: 'Recording service', lines: ['nxrs-recording-service'] })}
  ${panel({ id: 'telemetry-service', x: 536, y: 204, w: 218, title: 'Telemetry service', lines: ['nxrs-telemetry-service'] })}
  ${panel({ id: 'camera', x: 44, y: 332, w: 218, h: 60, title: 'nxrs-camera', accent: p.blue })}
  ${panel({ id: 'storage', x: 290, y: 332, w: 218, h: 60, title: 'nxrs-storage', accent: p.blue })}
  ${panel({ id: 'transport', x: 536, y: 332, w: 218, h: 60, title: 'nxrs-transport', accent: p.blue })}
  ${edge('fixture', 'camera-service', 'M272 148 V174 H153 V202')}
  ${edge('fixture', 'recording-service', 'M400 148 V202')}
  ${edge('fixture', 'telemetry-service', 'M528 148 V174 H645 V202')}
  ${edge('camera-service', 'camera', 'M153 292 V330')}
  ${edge('recording-service', 'storage', 'M399 292 V330')}
  ${edge('telemetry-service', 'transport', 'M645 292 V330')}
  ${group(p, 'navigation', 24, 456, 752, 264, 'Navigation service dependencies')}
  ${panel({ id: 'navigation', x: 44, y: 516, w: 290, title: 'service/navigation', lines: ['Reusable behavior'] })}
  ${panel({ id: 'sensors', x: 412, y: 516, w: 342, title: 'nxrs-imu / nxrs-gnss', lines: ['Provider-independent contracts'], accent: p.blue })}
  ${edge('navigation', 'sensors', 'M334 560 H410')}
  ${panel({ id: 'provider', x: 412, y: 646, w: 342, h: 54, title: 'Build-selected providers' })}
  ${edge('sensors', 'provider', 'M583 604 V644')}
`);
}

function aoStress(p) {
  return guideFigure(p, 'Active-object stress traffic and ownership',
    'P configured producer threads send paced or bursty work to W worker active objects. Each worker has a bounded inbox, private state and one event/deadline wait. Workers send results to one collector with its own bounded inbox and state. Both transfer boundaries use nonblocking try_send with counted drop-newest overload. Counts and queue capacities are configured by the test.', 456, `
  ${group(p, 'producers', 24, 32, 220, 318, 'P producer threads')}
  ${panel({ id: 'producer', x: 44, y: 112, w: 180, h: 100, title: 'Traffic source', lines: ['Paced or bursty'] })}
  ${label(134, 270, 'Configured rate')}
  ${label(134, 298, 'and producer count')}
  ${group(p, 'workers', 294, 32, 220, 318, 'W worker owners')}
  ${panel({ id: 'worker', x: 314, y: 112, w: 180, h: 100, title: 'Bounded inbox', lines: ['One per worker'], accent: p.blue })}
  ${label(404, 270, 'Private state')}
  ${label(404, 298, 'Event / deadline wait')}
  ${group(p, 'collector', 564, 32, 212, 318, 'One collector')}
  ${panel({ id: 'collector', x: 580, y: 112, w: 180, h: 100, title: 'Bounded inbox', lines: ['Collected results'], accent: p.orange })}
  ${label(670, 270, 'Private state')}
  ${label(670, 298, 'Completion counts')}
  ${edge('producer', 'worker', 'M224 162 H312')}
  ${edge('worker', 'collector', 'M494 162 H578')}
  ${label(400, 394, 'Both boundaries: try_send · drop newest when full · count each rejection')}
  ${label(400, 424, 'Queue capacity is bounded separately for each worker and the collector.')}
`);
}

function cameraPipeline(p) {
  return guideFigure(p, 'Independent streaming and bounded inference paths',
    'The camera HAL feeds CameraService, which supplies MJPEG streaming and offers one owned JPEG to an inference worker. The worker invokes the detector HAL synchronously; Monitor turns results into typed state and events. The bounded copy separates inference lifetime from camera buffers, and an occupied worker rejects additional offers instead of queuing video frames.', 792, `
  ${panel({ id: 'camera-hal', x: 240, y: 24, w: 320, title: 'Camera HAL', lines: ['Capture ownership'] })}
  ${panel({ id: 'camera-service', x: 240, y: 150, w: 320, title: 'CameraService', lines: ['Frame history and delivery'], accent: p.blue })}
  ${edge('camera-hal', 'camera-service', 'M400 112 V148')}
  ${panel({ id: 'stream', x: 24, y: 302, w: 320, title: 'MJPEG service', lines: ['Streaming keeps progressing'] })}
  ${panel({ id: 'jpeg', x: 436, y: 302, w: 340, title: 'One owned JPEG', lines: ['Bounded copy; excess offers drop'], accent: p.orange })}
  ${edge('camera-service', 'stream', 'M320 238 V270 H184 V300')}
  ${edge('camera-service', 'jpeg', 'M480 238 V270 H606 V300')}
  ${panel({ id: 'worker', x: 436, y: 430, w: 340, title: 'Inference worker', lines: ['Owns the copied frame'] })}
  ${panel({ id: 'detector', x: 436, y: 558, w: 340, title: 'Detector HAL', lines: ['Synchronous model execution'] })}
  ${panel({ id: 'monitor', x: 24, y: 558, w: 320, title: 'Monitor', lines: ['Confirmation · freshness policy'], accent: p.blue })}
  ${panel({ id: 'events', x: 24, y: 686, w: 320, title: 'Typed state and events', lines: ['Application-visible results'] })}
  ${edge('jpeg', 'worker', 'M606 390 V428')}
  ${edge('worker', 'detector', 'M606 518 V556')}
  ${edge('detector', 'monitor', 'M436 602 H346')}
  ${edge('monitor', 'events', 'M184 646 V684')}
`);
}

function deviceAccess(p) {
  return guideFigure(p, 'Typed Rust access to NuttX device endpoints',
    'Portable code calls a capability contract. Its private NuttX provider uses Rust standard-library file operations for ordinary opens and data transfer. Controls that std cannot express use small private target-header ABI helpers. Both paths reach NuttX native operations and the registered VFS driver; NuttX owns lower-half hardware access. Ordinary networking instead uses qualified std::net socket operations.', 792, `
  ${panel({ id: 'api', x: 160, y: 24, w: 480, title: 'Portable capability API', lines: ['Typed operations · errors · lifecycle'], accent: p.blue })}
  ${panel({ id: 'provider', x: 160, y: 150, w: 480, title: 'Private NuttX provider', lines: ['Rust owns behavior and resource lifetime'] })}
  ${edge('api', 'provider', 'M400 112 V148')}
  ${panel({ id: 'std', x: 24, y: 302, w: 348, title: 'Rust std file I/O', lines: ['OpenOptions · Read · Write'], accent: p.blue })}
  ${panel({ id: 'ffi', x: 428, y: 302, w: 348, title: 'Private target ABI helper', lines: ['Device controls not expressed by std'], accent: p.orange })}
  ${edge('provider', 'std', 'M280 238 V270 H198 V300')}
  ${edge('provider', 'ffi', 'M520 238 V270 H602 V300')}
  ${panel({ id: 'native', x: 160, y: 454, w: 480, title: 'NuttX native operations', lines: ['open / read / write · ioctl'] })}
  ${edge('std', 'native', 'M198 390 V422 H280 V452')}
  ${edge('ffi', 'native', 'M602 390 V422 H520 V452')}
  ${panel({ id: 'vfs', x: 160, y: 580, w: 480, title: 'Registered VFS driver', lines: ['Device lookup · driver file operations'] })}
  ${edge('native', 'vfs', 'M400 542 V578')}
  ${panel({ id: 'hardware', x: 160, y: 706, w: 480, h: 60, title: 'NuttX lower-half / hardware' })}
  ${edge('vfs', 'hardware', 'M400 668 V704')}
`);
}

function priorityInboxes(p) {
  return guideFigure(p, 'Independent queue capacity with one selection point',
    'Lifecycle stop requests have a reserved bounded queue. Critical commands and terminal faults use an important queue. Ordinary HAL solutions and peer events use an ordinary queue. One owner selects across the queues and processes one event at a time. Separate capacity does not preempt a running handler or provide fairness without an explicit scheduling policy.', 514, `
  ${panel({ id: 'stop-source', x: 24, y: 24, w: 250, title: 'Lifecycle owner', lines: ['Stop requests'] })}
  ${panel({ id: 'important-source', x: 24, y: 170, w: 250, title: 'Critical events', lines: ['Commands · terminal faults'] })}
  ${panel({ id: 'ordinary-source', x: 24, y: 316, w: 250, title: 'Ordinary inputs', lines: ['HAL data · peer events'] })}
  ${panel({ id: 'stop', x: 314, y: 24, w: 212, title: 'Stop queue', lines: ['Reserved capacity'], accent: p.orange })}
  ${panel({ id: 'important', x: 314, y: 170, w: 212, title: 'Important queue', lines: ['Independent bound'] })}
  ${panel({ id: 'ordinary', x: 314, y: 316, w: 212, title: 'Ordinary queue', lines: ['Independent bound'], accent: p.blue })}
  ${panel({ id: 'select', x: 584, y: 170, w: 192, h: 114, title: 'One selection', lines: ['One owner', 'Bounded handlers'] })}
  ${edge('stop-source', 'stop', 'M274 68 H312')}
  ${edge('important-source', 'important', 'M274 214 H312')}
  ${edge('ordinary-source', 'ordinary', 'M274 360 H312')}
  ${edge('stop', 'select', 'M526 68 H554 V196 H582')}
  ${edge('important', 'select', 'M526 214 H582')}
  ${edge('ordinary', 'select', 'M526 360 H554 V258 H582')}
  ${label(400, 452, 'All producers of a class share its queue; each class has a separate bound.')}
  ${label(400, 484, 'Selection order, deadlines and fairness remain explicit owner policy.')}
`);
}

function acquisitionBoundary(p) {
  return guideFigure(p, 'Acquisition produces typed input for an application owner',
    'Device readiness wakes the HAL acquisition path, which waits, reads, parses and normalizes device data. Its narrow typed delivery endpoint admits data to the destination service queues. Commands and peer events enter those queues separately. One owner waits or selects and applies local processing policy. The endpoint runs in producer task context and never runs the receiving handler inline.', 654, `
  ${panel({ id: 'device', x: 24, y: 24, w: 340, title: 'Device readiness', lines: ['Qualified wake / cancellation'] })}
  ${panel({ id: 'hal', x: 24, y: 150, w: 340, h: 112, title: 'HAL acquisition', lines: ['Wait · read · parse · normalize', 'Own device resources'], accent: p.blue })}
  ${edge('device', 'hal', 'M194 112 V148')}
  ${panel({ id: 'sink', x: 24, y: 302, w: 340, title: 'Typed delivery endpoint', lines: ['Map and admit; never handle inline'] })}
  ${edge('hal', 'sink', 'M194 262 V300')}
  ${panel({ id: 'control', x: 436, y: 302, w: 340, title: 'Commands / peer events', lines: ['Independent producers'] })}
  ${panel({ id: 'queues', x: 160, y: 454, w: 480, title: 'Service queues', lines: ['Bounded admission · explicit full policy'] })}
  ${edge('sink', 'queues', 'M194 390 V422 H280 V452')}
  ${edge('control', 'queues', 'M606 390 V422 H520 V452')}
  ${panel({ id: 'owner', x: 160, y: 578, w: 480, h: 52, title: 'One wait / selection · local processing', accent: p.orange })}
  ${edge('queues', 'owner', 'M400 542 V576')}
`);
}


const guideDiagrams = [
  ['nxrs-build-workflow', buildWorkflow],
  ['nxrs-profile-composition', profileComposition],
  ['nxrs-interface-boundaries', interfaceBoundaries],
  ['nxrs-owner-event-loop', ownerEventLoop],
  ['nxrs-owner-lifecycle', ownerLifecycle],
  ['nxrs-controller-command', controllerCommand],
  ['nxrs-navigation-dataflow', navigationDataflow],
  ['nxrs-service-dependencies', serviceDependencies],
  ['nxrs-ao-stress', aoStress],
  ['nxrs-camera-pipeline', cameraPipeline],
  ['nxrs-device-access', deviceAccess],
  ['nxrs-priority-inboxes', priorityInboxes],
  ['nxrs-acquisition-boundary', acquisitionBoundary],
];

const checkOnly = process.argv.includes('--check');
if (process.argv.slice(2).some(arg => arg !== '--check')) throw new Error('Usage: node docs/assets/render-architecture.mjs [--check]');
const stale = [];
let outputCount = 0;
for (const [theme, palette] of Object.entries(palettes)) {
  const suffix = theme === 'light' ? '' : '-dark';
  for (const [name, render] of [
    ['nxrs-architecture', diagram],
    ['nxrs-event-communication', eventCommunication],
    ...guideDiagrams,
  ]) {
    const file = new URL(`${name}${suffix}.svg`, import.meta.url);
    const output = render(palette).replace(/[ \t]+$/gm, '');
    if (checkOnly) {
      let current;
      try { current = readFileSync(file, 'utf8'); } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      if (current !== output) stale.push(`${name}${suffix}.svg`);
    } else {
      writeFileSync(file, output);
    }
    outputCount += 1;
  }
}

if (stale.length) {
  console.error(`Stale or missing diagrams: ${stale.join(', ')}. Run node docs/assets/render-architecture.mjs.`);
  process.exitCode = 1;
} else {
  console.log(`${checkOnly ? 'Checked' : 'Generated'} ${outputCount} architecture SVG files.`);
}
