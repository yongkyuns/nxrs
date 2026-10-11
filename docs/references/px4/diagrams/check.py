#!/usr/bin/env python3
"""Measure every SVG at explicit, readable standalone widths."""
import json,re,xml.etree.ElementTree as ET
from pathlib import Path
HERE=Path(__file__).resolve().parent
rows=[]
for src in sorted(HERE.glob('*.d2')):
    if src.stem=='material':continue
    path=src.with_suffix('.svg');root=ET.parse(path).getroot()
    _,_,w,h=map(float,root.attrib['viewBox'].split())
    fonts=[]
    for t in root.iter('{http://www.w3.org/2000/svg}text'):
        m=re.search(r'font-size\s*:\s*([\d.]+)',t.get('style',''))
        if m:fonts.append(float(m.group(1)))
        elif t.get('font-size'):fonts.append(float(t.get('font-size').removesuffix('px')))
    assert fonts and w>h,(src,'not landscape or missing font sizes')
    reading={'sensor-to-ekf-execution-map':1600,'execution-loops-data-flow':1800,'execution-map':1000,'imu-to-ekf':1200,'gnss-to-ekf':1200}.get(src.stem,800)
    scale=min(1,reading/w);assert min(fonts)*scale>=14,(src,'small text at documented reading width')
    assert not list(root.iter('{http://www.w3.org/2000/svg}image')),(src,'embedded raster')
    rows.append(dict(name=src.stem,width=w,height=h,height_at_800=round(h*min(1,800/w),1),min_font_at_800=round(min(fonts)*min(1,800/w),1),reading_width=reading,height_at_reading_width=round(h*scale,1),min_font_at_reading_width=round(min(fonts)*scale,1)))
assert len(rows)==14,len(rows)
(HERE/'layout-metrics.json').write_text(json.dumps(rows,indent=2)+'\n')
print(json.dumps(rows,indent=2))
