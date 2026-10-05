from pathlib import Path
import hashlib,json,subprocess
root=Path('docs/references');out=Path('/tmp/inline-qualification');out.mkdir(exist_ok=True)
base='98fe22419803e32f947b0ff8b70e61439e3ebeed'
old=[p for p in subprocess.check_output(['git','ls-tree','-r','--name-only',base,'docs/references'],text=True).splitlines() if p.endswith('.svg')]
for path in old:
    assert Path(path).read_bytes()==subprocess.check_output(['git','show',base+':'+path]),path
newsets=[root/'diagrams']+[root/s/'diagrams/inline' for s in ['openvela','zephyr','px4']]
browser=[d for p in newsets for d in json.loads((p/'browser-checks.json').read_text())['diagrams']]
routing=[d for p in newsets for d in json.loads((p/'routing-checks.json').read_text())['diagrams']]
assert len(browser)==31 and all(not d['errors'] for d in browser)
assert all(not d['block_collisions'] and not d['wire_crossings'] and not d['collinear_overlaps'] for d in routing)
files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}
(out/'file-hashes.json').write_text(json.dumps(files,indent=2)+'\n')
subprocess.run(['git','add','docs/references'],check=True)
tree=subprocess.check_output(['git','write-tree'],text=True).strip()
actual=subprocess.check_output(['git','rev-parse',tree+':docs/references'],text=True).strip()
expected=Path('.inline-workbench/expected-reference-tree.txt').read_text().strip()
assert actual==expected,(actual,expected)
report={'source_baseline':base,'reference_tree':actual,'matches_locally_reviewed_tree':True,'existing_svgs_preserved':len(old),'new_diagrams':len(browser),'all_diagrams':len(list(root.rglob('*.svg'))),'new_connectors':sum(d['connectors'] for d in routing),'new_boundary_ports':sum(d['boundary_ports'] for d in routing),'local_browser_text_lines':sum(d['text_lines'] for d in browser),'minimum_type_at_800px':min(d['minimum_type_at_reading_width'] for d in browser),'docs':json.loads((root/'reading-checks.json').read_text()),'browser_scope':'Local Chromium checks and visual review are bound to this exact reference tree; this job is not a new hosted browser run.','scope':'Documentation only; no firmware or hardware qualification.'}
(out/'qualification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
