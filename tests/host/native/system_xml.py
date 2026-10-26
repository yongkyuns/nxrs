"""Run only as a disposable hosted system-Python child, after source admission."""
import hashlib,json,os,sys
from pathlib import Path

def main():
    assert sys.platform=='linux' and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    expected=Path(sys.argv[1]).resolve(strict=True)
    assert expected.parent==Path('/usr/lib/nxrs-gateway-system-xml-qualification/lib')
    from xml.parsers import expat
    import xml.etree.ElementTree as tree
    assert expat.EXPAT_VERSION=='expat_2.9.0'
    root=tree.fromstring("<r xmlns='urn:nxrs:owned'><x a='1'>owned</x></r>")
    assert root.tag=='{urn:nxrs:owned}r' and len(root)==1 and root[0].text=='owned'
    parser=expat.ParserCreate(namespace_separator='|');events=[]
    parser.StartElementHandler=lambda name,attrs:events.append(name)
    raw="<r xmlns='urn:nxrs:owned'><x>caf\u00e9</x></r>".encode()
    for offset in range(0,len(raw),3):parser.Parse(raw[offset:offset+3],False)
    parser.Parse(b'',True)
    assert events==['urn:nxrs:owned|r','urn:nxrs:owned|x']
    try:tree.fromstring('<r><x></r>')
    except tree.ParseError:pass
    else:raise AssertionError('malformed XML was accepted')
    images=set()
    for line in Path('/proc/self/maps').read_text().splitlines():
        fields=line.split(maxsplit=5)
        if len(fields)==6 and '/libexpat.so.' in fields[5]:images.add(Path(fields[5]).resolve(strict=True))
    assert images=={expected}
    print(json.dumps(dict(schema=1,version=expat.EXPAT_VERSION,exact_loaded_image=True,
        image_sha256=hashlib.sha256(expected.read_bytes()).hexdigest(),elementtree=True,
        fragmented_namespace_utf8=True,malformed_document_refused=True)))

if __name__=='__main__':main()
