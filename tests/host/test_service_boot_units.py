"""Boot ordering, source admission and exact fixture resource regression tests."""
import copy,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tests'))
import service_boot_units as boot
import encrypted_service_reference as service

def fields(text):
    result={}
    for line in text.splitlines():
        if '=' in line:
            key,value=line.split('=',1);result.setdefault(key,[]).append(value)
    return result

class ServiceBootUnitsTests(unittest.TestCase):
    def test_proxy_import_diagnostics_admit_only_declared_source_coordinates(self):
        for source in ('tools/gateway-proxy/advisories.py','tools/native-packages/ubuntu.py',
                       'gateway/tests/failure_location.py'):
            row=dict(source=source,line=14,error='AttributeError')
            self.assertEqual(boot.public_failure_locations([row]),[row])
        for row in [dict(source='/private/custody/file',line=14,error='AttributeError'),
                    dict(source='tools/gateway-proxy/advisories.py',line=14,error='private-value'),
                    dict(source='tools/gateway-proxy/advisories.py',line=14,error='AttributeError',message='private-value')]:
            with self.assertRaises(ValueError):boot.public_failure_locations([row])

    def test_provider_can_precede_local_filesystems_without_early_boot_cycle(self):
        group=boot.units();mount=fields(group[boot.MOUNT]);provider=fields(group[boot.PROVIDER])
        self.assertIn(boot.PROVIDER,mount['After'][0].split())
        self.assertEqual(provider['DefaultDependencies'],['no'])
        self.assertIn('local-fs.target',' '.join(provider['Before']).split())
        after=' '.join(provider['After']).split()
        self.assertIn('systemd-remount-fs.service',after)
        self.assertFalse(set(after)&{'basic.target','local-fs.target','multi-user.target'})
        broken=group[boot.PROVIDER].replace('DefaultDependencies=no\n','')
        self.assertNotIn('DefaultDependencies',fields(broken))

    def test_checker_runs_even_when_the_autostart_group_is_refused(self):
        checker=fields(boot.units()[boot.CHECKER])
        self.assertEqual(checker['Wants'],[boot.TARGET]);self.assertNotIn('Requires',checker)
        self.assertIn(boot.TARGET,checker['After'][0].split())
        self.assertEqual(checker['WantedBy'],['multi-user.target'])

    def test_real_application_mount_and_retirement_units_are_preserved(self):
        parent=service.units(boot.PREFIX,boot.ROOT,boot.MAPPING,boot.MOUNT);actual=boot.units()
        for name,text in parent.items():
            if name in (boot.PROVIDER,boot.PREFIX+'-source.service'):continue
            self.assertEqual(actual[name],text,name)
        self.assertEqual(set(actual),set(parent)|{boot.CHECKER})

    def test_source_cannot_autostart_before_checker_readiness_and_does_not_write_source_cache(self):
        source=fields(boot.units()[boot.PREFIX+'-source.service'])
        self.assertNotIn('WantedBy',source);self.assertNotIn('RequiredBy',source)
        self.assertIn('/usr/bin/python3 -B ',source['ExecStart'][0])
        self.assertIn(str(boot.ROOT/'source.py'),source['ExecStart'][0])
        self.assertEqual(source['LoadCredential'],['source:'+str(boot.ROOT/'conf/source.json')])

    def test_plan_binds_exact_programs_proofs_units_and_acyclic_media_sonames(self):
        library='/usr/lib/nxrs-gateway-media-qualification/lib/'
        files={'/var/lib/service-boot/bin/'+name:'a'*64 for name in
               ('control','media','proxy','activity','sandbox','publisher','reader','ffmpeg','nginx','installer','xml-probe')}
        files.update({'/var/lib/service-boot/'+name:'b'*64 for name in
                      ('proxy-parent.json','proxy-review.json','source-manifest.json')})
        files[library+'libavcodec.so.1.2.3']='c'*64
        files.update({str(boot.xml.LIBRARY):'c'*64,str(boot.xml.QUALIFICATION):'b'*64})
        expected_inputs=service.source_inputs()
        import candidate
        plan=dict(schema=2,scope='owned-generated-service-boot-plan',nonce='d'*32,head='e'*40,
            kernel_release='fixture-kernel',pid1_sha256='f'*64,source_manifest_sha256='b'*64,
            parent_proxy_qualification_sha256='b'*64,parent_proxy_review_sha256='b'*64,
            inputs=expected_inputs,proxy_inputs=candidate.source_inputs(),files=files,
            system_xml_runtime=dict(qualification_sha256='b'*64,library_sha256='c'*64),
            proxy_libraries={name:'a'*64 for name in ('loader','libc','crypto','ssl')},unit_sha256=boot.unit_hashes(),
            runtime_links={library+'libavcodec.so':'libavcodec.so.1',library+'libavcodec.so.1':'libavcodec.so.1.2.3'})
        boot.validate_plan(plan)
        changes=[]
        changed=copy.deepcopy(plan);changed['files'].pop('/var/lib/service-boot/bin/activity');changes.append(changed)
        changed=copy.deepcopy(plan);changed['files']['/outside/program']='a'*64;changes.append(changed)
        changed=copy.deepcopy(plan);changed['files']['/var/lib/service-boot/proxy-parent.json']='d'*64;changes.append(changed)
        changed=copy.deepcopy(plan);changed['runtime_links'][library+'libavcodec.so']='../outside';changes.append(changed)
        changed=copy.deepcopy(plan);changed['runtime_links'][library+'libavcodec.so.1']='libavcodec.so';changes.append(changed)
        changed=copy.deepcopy(plan);changed['runtime_links'][library+'libavcodec.so.1']='missing.so';changes.append(changed)
        changed=copy.deepcopy(plan);changed['proxy_inputs']['outside-source']='a'*64;changes.append(changed)
        changed=copy.deepcopy(plan);changed['unit_sha256'][boot.PROVIDER]='a'*64;changes.append(changed)
        changed=copy.deepcopy(plan);changed['schema']=True;changes.append(changed)
        changed=copy.deepcopy(plan);changed['schema']=1;changes.append(changed)
        for name in (str(boot.xml.QUALIFICATION),str(boot.xml.LIBRARY),str(boot.xml.PROBE)):
            changed=copy.deepcopy(plan);changed['files'].pop(name);changes.append(changed)
        changed=copy.deepcopy(plan);changed['system_xml_runtime']['library_sha256']='0'*64;changes.append(changed)
        for index,changed in enumerate(changes):
            with self.subTest(index=index),self.assertRaises(ValueError):boot.validate_plan(changed)

    def test_case_parser_refuses_duplicate_missing_unknown_and_injected_controls(self):
        nonce='a'*32
        for case in boot.CASES:self.assertEqual(boot.command_case('root=/dev/vda service_case='+case+' service_nonce='+nonce),(case,nonce))
        for value in ('','service_case=other service_nonce='+nonce,
                      'service_case=locked service_case=recovered service_nonce='+nonce,
                      'service_case=locked service_nonce='+nonce+' service_nonce='+nonce,
                      'service_case=locked service_nonce='+nonce+';poweroff'):
            with self.subTest(value=value),self.assertRaises(ValueError):boot.command_case(value)

if __name__=='__main__':unittest.main()
