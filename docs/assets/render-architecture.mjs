// Regenerate: node docs/assets/render-architecture.mjs
// Generate matching light and dark SVGs with editable labels and accessible descriptions.
import { writeFileSync } from 'node:fs';

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

for (const [theme, palette] of Object.entries(palettes)) {
  const suffix = theme === 'light' ? '' : '-dark';
  for (const [name, render] of [
    ['nxrs-architecture', diagram],
    ['nxrs-event-communication', eventCommunication],
  ]) {
    writeFileSync(new URL(`${name}${suffix}.svg`, import.meta.url), render(palette).replace(/[ \t]+$/gm, ''));
  }
}
