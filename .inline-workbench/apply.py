from pathlib import Path
import hashlib, json, lzma, subprocess
stage=Path('.inline-workbench')
data=b''.join((stage/f'payload.{i:02d}').read_bytes() for i in range(4))
assert hashlib.sha256(data).hexdigest()=='e3988515bdb87e0b177f6aed2d75771569902bfff1396ff29be4e8d3d942012c'
parts=json.loads(lzma.decompress(data))
allowed={'design.py','edit_docs.py','support.py','finish.py','check_docs.py','expected-reference-tree.txt'}|{p+'/browser-checks.json' for p in ['diagrams','openvela/diagrams/inline','zephyr/diagrams/inline','px4/diagrams/inline']}
assert set(parts)==allowed
for name in ['design.py','edit_docs.py','support.py']:
    path=stage/name
    path.write_text(parts[name])
    subprocess.run(['python3',str(path)],check=True)
root=Path('docs/references')
(root/'check_docs.py').write_text(parts['check_docs.py'])
for name in allowed:
    if name.endswith('/browser-checks.json'):
        (root/name).write_text(parts[name])
(stage/'finish.py').write_text(parts['finish.py'])
subprocess.run(['python3',str(stage/'finish.py')],check=True)
(stage/'expected-reference-tree.txt').write_text(parts['expected-reference-tree.txt'])
