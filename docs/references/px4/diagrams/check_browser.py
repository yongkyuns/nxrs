#!/usr/bin/env python3
"""Check real SVG text bounds with Chromium, including D2's embedded fonts.

Optional dependency: Playwright plus Chromium. This complements check.py and
route_svg.py; it is not required for the basic D2 rendering command.
"""
from __future__ import annotations
import argparse
import base64
import html
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent

def contains(outer, inner, margin=0):
    x, y, w, h = inner
    X, Y, W, H = outer
    return x >= X+margin and y >= Y+margin and x+w <= X+W-margin and y+h <= Y+H-margin

def overlap(a, b, pad=2):
    x, y, w, h = a
    X, Y, W, H = b
    return min(x+w+pad, X+W) > max(x-pad, X) and min(y+h+pad, Y+H) > max(y-pad, Y)

def crossing(a, b, box, pad=2):
    x, y, w, h = box
    x -= pad; y -= pad; w += 2*pad; h += 2*pad
    if a[0] == b[0]:
        return x < a[0] < x+w and max(min(a[1], b[1]), y) < min(max(a[1], b[1]), y+h)
    if a[1] == b[1]:
        return y < a[1] < y+h and max(min(a[0], b[0]), x) < min(max(a[0], b[0]), x+w)
    raise ValueError('Non-orthogonal route')

EXTRACT = """() => [...document.querySelectorAll('svg g')]
  .filter(g => g.querySelector(':scope > text')).flatMap(g => {
    const id = g.getAttribute('class').split(' ')[0];
    const rect = g.querySelector(':scope > g.shape > rect');
    const bg = g.querySelector(':scope > rect.routing-label-bg');
    const attrs = el => el ? ['x','y','width','height'].map(k => +el.getAttribute(k)) : null;
    let els = [...g.querySelectorAll(':scope > text')];
    els = els.flatMap(t => t.children.length ? [...t.children] : [t]);
    return els.filter(t => t.textContent.trim()).map(t => {
      const b = t.getBBox();
      return {id, text:t.textContent, bbox:[b.x,b.y,b.width,b.height],
              rect:attrs(rect), background:attrs(bg)};
    });
  })"""

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--chromium', help='Optional installed Chromium executable')
    ap.add_argument('--screenshots-dir', type=Path)
    args = ap.parse_args()
    spec = json.loads((HERE/'routes.json').read_text())
    results = []
    with sync_playwright() as p:
        launch = {'headless': True}
        if args.chromium:
            launch['executable_path'] = args.chromium
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={'width':1800, 'height':1200})
        version = browser.version
        for name, edges in spec['diagrams'].items():
            page.set_content('<style>body{margin:0}body>svg{width:100%;height:auto}</style>' +
                             (HERE/(name+'.svg')).read_text())
            page.evaluate('document.fonts.ready')
            boxes = page.evaluate(EXTRACT)
            for row in boxes:
                row['id'] = html.unescape(base64.b64decode(row['id']).decode())
            failures = {'text_outside_node': [], 'overlapping_text': [], 'text_wire': []}
            masked = 0
            for row in boxes:
                if row['rect'] and not contains(row['rect'], row['bbox'], 2):
                    failures['text_outside_node'].append(row)
                for edge in edges:
                    for a, b in zip(edge['points'], edge['points'][1:]):
                        if not crossing(a, b, row['bbox']):
                            continue
                        # Only decorative lifelines can pass behind an opaque text panel.
                        # Functional data/wake arrows never receive this exemption.
                        if (edge.get('role') == 'lifeline' and row['background'] and
                                contains(row['background'], row['bbox'], 2)):
                            masked += 1
                        else:
                            failures['text_wire'].append({'text': row, 'edge':edge['edge'], 'segment':[a,b]})
            for i, row in enumerate(boxes):
                for other in boxes[i+1:]:
                    if row['id'] != other['id'] and overlap(row['bbox'], other['bbox']):
                        failures['overlapping_text'].append([row, other])
            results.append({'name':name, 'text_lines_checked':len(boxes),
                            'masked_decorative_lifeline_intersections':masked, **failures})
            if args.screenshots_dir:
                args.screenshots_dir.mkdir(parents=True, exist_ok=True)
                page.locator('body > svg').screenshot(path=str(args.screenshots_dir/(name+'.png')))
        browser.close()
    report = {'chromium_version':version, 'clearance_px':2, 'diagrams':results}
    (HERE/'browser-checks.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    if any(row[k] for row in results for k in ('text_outside_node','overlapping_text','text_wire')):
        raise SystemExit('Browser geometry validation failed')

if __name__ == '__main__':
    main()
