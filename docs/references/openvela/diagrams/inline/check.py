#!/usr/bin/env python3
"""Check inline diagram geometry and the checked-in SVG/report pairing."""
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET
from route_svg import boxes, port


def check(directory: Path) -> dict:
    spec = json.loads((directory / 'routes.json').read_text())
    reports = json.loads((directory / 'routing-checks.json').read_text())['diagrams']
    assert {r['diagram'] for r in reports} == set(spec['diagrams']), 'Missing diagram report'
    for result in reports:
        name = result['diagram']
        path = directory / (name + '.svg')
        assert result['svg_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest(), name
        assert not result['block_collisions'], name
        assert not result['wire_crossings'], name
        assert not result['collinear_overlaps'], name
        root = ET.parse(path).getroot()
        _, _, width, height = map(float, root.attrib['viewBox'].split())
        assert width > height and width <= 1000 and height <= 650, (name, width, height)
        bs = boxes(root)
        for declared, actual in zip(spec['diagrams'][name], result['routes']):
            pts = actual['points']
            a = port(bs[declared['source']], declared['from'])
            b = port(bs[declared['target']], declared['to'])
            assert tuple(pts[0]) == a and tuple(pts[-1]) == b, name
            # A line must leave its source and enter its target through the
            # declared side, not merely happen to end on the right coordinate.
            vectors = {
                'n': (0, -1), 's': (0, 1), 'e': (1, 0), 'w': (-1, 0)
            }
            for side, start, end in (
                (declared['from'][0], pts[0], pts[1]),
                (declared['to'][0], pts[-1], pts[-2]),
            ):
                vx, vy = vectors[side]
                dx, dy = end[0]-start[0], end[1]-start[1]
                assert vx*dx + vy*dy > 0 and vx*dy == vy*dx, (name, side, start, end)
    return {'diagrams': len(reports),
            'connectors': sum(r['connectors'] for r in reports),
            'boundary_ports': sum(r['boundary_ports'] for r in reports)}


if __name__ == '__main__':
    print(json.dumps(check(Path(__file__).resolve().parent), indent=2))
