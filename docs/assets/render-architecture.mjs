// Regenerate: node docs/assets/render-architecture.mjs
// Preserve the original Mermaid topology; customize only visual presentation.
// Both SVGs retain editable labels and accessible descriptions without dependencies.
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

function diagram(p) {
  const edge = (from, to, d, dashed = false) =>
    `<path id="edge-${from}-${to}" data-from="${from}" data-to="${to}" d="${d}" class="edge"${dashed ? ' stroke-dasharray="4 6"' : ''}/>`;
  const card = ({ id, x, y, w, h, title, subtitle, accent, size = 24 }) =>
    `<g id="node-${id}">
      <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="15" class="card" filter="url(#shadow)"/>
      ${accent ? `<rect x="${x + 20}" y="${y + 24}" width="4" height="${h - 48}" rx="2" fill="${accent}"/>` : ''}
      <text x="${x + w / 2 + (accent ? 8 : 0)}" y="${y + (subtitle ? 35 : h / 2 + 8)}" text-anchor="middle" class="label" font-size="${size}">${title}</text>
      ${subtitle ? `<text x="${x + w / 2 + (accent ? 8 : 0)}" y="${y + 65}" text-anchor="middle" class="subtitle">${subtitle}</text>` : ''}
    </g>`;

  return `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="810" viewBox="0 0 1200 810" role="img" aria-labelledby="title description">
  <title id="title">NXRS architecture</title>
  <desc id="description">Platform profiles configure the reusable Rust firmware group at build time. Within that group, applications connect to services, capability HALs and chip drivers. The firmware group has two separate paths: ordinary Rust APIs through a standard library compiled for NuttX, and device access through Rust peripheral wrappers. Both paths lead to NuttX, which provides the POSIX C environment, RTOS and BSP on the embedded MCU.</desc>
  <defs>
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
      .subtitle { fill: ${p.secondary}; font-size: 19px; }
      .edge { fill: none; stroke: ${p.line}; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; marker-end: url(#arrow); }
      .edge-label { fill: ${p.secondary}; font-size: 16px; }
    </style>
  </defs>

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

for (const [theme, palette] of Object.entries(palettes)) {
  const filename = theme === 'light' ? 'nxrs-architecture.svg' : 'nxrs-architecture-dark.svg';
  writeFileSync(new URL(filename, import.meta.url), diagram(palette).replace(/[ \t]+$/gm, ''));
}
