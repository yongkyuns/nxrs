"""Offline advisory disposition controls; no packages, network or hardware used."""
import copy
from datetime import datetime, timezone
import json
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import applicability as a


class NativeApplicabilityTests(unittest.TestCase):
    def test_workflow_runs_independent_assessment_without_losing_observation_or_advisory_failure(self):
        bash=shutil.which('bash')
        if bash is None:self.skipTest('bash required for the hosted shell contract')
        workflow=(ROOT/'.github/workflows/minimized-native-packages.yml').read_text()
        start=workflow.index('          python_payload_status=0\n')
        end=workflow.index('      - name: Retain bounded public origins',start)
        script=textwrap.dedent(workflow[start:end])
        for name,label in [('python_payloads','payload'),('applicability','assessment')]:
            script,count=re.subn(r'(?m)^timeout[^\n]*tools/native-packages/'+name+r'\.py[^\n]*?(?= \|\| )',
                '"$NXRS_TEST_OBSERVER" '+label,script)
            self.assertEqual(count,1)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);observer=root/'observer';log=root/'log'
            observer.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$NXRS_TEST_LOG"\n'
                'if [ "$1" = payload ]; then exit "$NXRS_TEST_PAYLOAD_STATUS"; fi\n'
                'exit "$NXRS_TEST_ASSESSMENT_STATUS"\n')
            observer.chmod(0o700)
            for payload,assessment in [(0,0),(1,0),(0,1),(1,1),(137,1),(0,124)]:
                log.unlink(missing_ok=True)
                env=dict(os.environ,NXRS_TEST_OBSERVER=str(observer),NXRS_TEST_LOG=str(log),
                    NXRS_TEST_PAYLOAD_STATUS=str(payload),NXRS_TEST_ASSESSMENT_STATUS=str(assessment))
                result=subprocess.run([bash,'-euo','pipefail','-c',script],env=env,
                    stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
                with self.subTest(payload=payload,assessment=assessment):
                    self.assertEqual(log.read_text().splitlines(),['payload','assessment'])
                    self.assertEqual(result.returncode,payload if payload else assessment)

    def setUp(self):
        self.config=json.loads((ROOT/a.POLICY).read_bytes())
        self.now=datetime(2026,10,27,12,tzinfo=timezone.utc)

    def fixture(self,other=True,source=True):
        rule=self.config['review'];owner=rule['binary_package']
        package=dict(name='libc6',version=rule['source_version'],architecture='amd64',
                     source='glibc',source_version=rule['source_version'])
        images={name:dict(copy.deepcopy(value),binary_package=owner) for name,value in rule['images'].items()}
        finding=dict(source_package='glibc',source_version=rule['source_version'],advisory=a.ADVISORY,
                     record_sha256='b'*64,canonical_record_sha256=a.RECORD,modified=a.MODIFIED)
        findings=[finding]
        if other:findings.append(dict(finding,advisory='UBUNTU-CVE-2026-18374',canonical_record_sha256='c'*64))
        retained=[dict(source_component='expat',advisory='CVE-2025-66382',applicability='unresolved')] if source else []
        parent=dict(checkout=dict(clean=True,commit='a'*40),inputs={'fixture':'d'*64},qualified=False,
            findings=findings,source_advisory_findings=retained,advisory_status='complete',
            inventory=dict(files=images,packages={owner:package},source_packages={'glibc':rule['source_version']},
                roots={name:{} for name in set(a.u.SYSTEM_ROOTS)|set(a.u.REPOSITORY_ROOTS)}))
        proof=dict(schema=2,scope=a.payloads.SCOPE,qualified=True,verification_status='complete',
            distribution_payloads_authenticated=True,checkout=copy.deepcopy(parent['checkout']),
            inputs=copy.deepcopy(parent['inputs']),advisory_gate_qualified=False,source_advisory_findings=copy.deepcopy(retained),
            advisory_findings=len(findings)+len(retained),packages={owner:dict(package,sha256=rule['package_sha256'])},
            files={name:dict(sha256=row['sha256'],size=row['size'],binary_package=owner,
                archive_sha256=rule['package_sha256'],uid=0,gid=0,mode=0o755) for name,row in images.items()})
        return parent,proof

    def test_exact_review_preserves_all_raw_findings_and_source_blockers(self):
        parent,proof=self.fixture();before=copy.deepcopy((parent,proof,self.config))
        result=a.assess(parent,proof,self.config,self.now)
        self.assertFalse(result['qualified']);self.assertFalse(result['raw_advisory_gate_qualified'])
        self.assertEqual(len(result['reviewed_findings']),1);self.assertEqual(len(result['blocking_distribution_findings']),1)
        self.assertEqual(result['raw_distribution_findings'],parent['findings'])
        self.assertEqual(result['raw_source_findings'],parent['source_advisory_findings'])
        self.assertEqual(result['blocking_source_findings'],parent['source_advisory_findings'])
        self.assertEqual(before,(parent,proof,self.config))
        result['reviewed_findings'][0]['finding']['advisory']='mutated output'
        self.assertEqual(before,(parent,proof,self.config))

    def test_review_alone_can_pass_only_separate_scope_and_never_rewrite_raw_gate(self):
        parent,proof=self.fixture(other=False,source=False)
        result=a.assess(parent,proof,self.config,self.now)
        self.assertTrue(result['qualified']);self.assertFalse(parent['qualified'])
        self.assertEqual(len(parent['findings']),1);self.assertFalse(result['raw_advisory_gate_qualified'])
        parent,proof=self.fixture(other=False,source=True)
        self.assertFalse(a.assess(parent,proof,self.config,self.now)['qualified'])

    def test_retiring_review_keeps_every_raw_finding_blocking(self):
        parent,proof=self.fixture()
        result=a.assess(parent,proof,dict(self.config,review=None),self.now)
        self.assertEqual(result['reviewed_findings'],[])
        self.assertEqual(result['blocking_distribution_findings'],parent['findings'])

    def test_exact_policy_schema_and_primary_references_required(self):
        for key,value in [('schema',True),('schema',2),('scope','other'),('extra',True)]:
            with self.subTest(key=key),self.assertRaises(a.u.Refusal):a.policy(dict(self.config,**{key:value}),self.now)
        changes=[('advisory','UBUNTU-CVE-2026-18374'),('source_version','new'),('binary_package','libc6:ppc64el'),
                 ('canonical_record_sha256','0'*64),('modified','2026-10-02T00:00:00Z'),
                 ('source_package','other'),('id','arbitrary-waiver'),('package_sha256','bad'),('references',[]),('extra',True)]
        for key,value in changes:
            with self.subTest(key=key),self.assertRaises(a.u.Refusal):
                a.policy(dict(self.config,review=dict(self.config['review'],**{key:value})),self.now)

    def test_policy_current_expiry_window_and_utc_format_are_mandatory(self):
        a.policy(self.config,self.now)
        for key,value in [('valid_until','2026-10-27T12:00:00Z'),('reviewed_at','2026-10-28T00:00:00Z'),
                          ('valid_until','2026-12-01T00:00:00Z'),('valid_until','2026-11-24T00:00:00+00:00'),
                          ('valid_until','2026-11-31T00:00:00Z'),('reviewed_at',None)]:
            with self.subTest(key=key,value=value),self.assertRaises(a.u.Refusal):
                a.policy(dict(self.config,review=dict(self.config['review'],**{key:value})),self.now)
        with self.assertRaises(a.u.Refusal):a.policy(self.config,self.now.replace(tzinfo=None))

    def test_coherently_changed_advisory_and_policy_cannot_add_a_waiver(self):
        parent,proof=self.fixture();config=copy.deepcopy(self.config)
        parent['findings'][0]['canonical_record_sha256']='0'*64;config['review']['canonical_record_sha256']='0'*64
        with self.assertRaises(a.u.Refusal):a.assess(parent,proof,config,self.now)

    def test_changed_advisory_semantics_or_missing_canonical_binding_stays_blocking(self):
        for key,value in [('canonical_record_sha256','0'*64),('modified','2026-10-02T00:00:00Z'),
                          ('source_version','new'),('source_package','other')]:
            parent,proof=self.fixture();parent['findings'][0][key]=value
            if key=='source_version':
                parent['inventory']['source_packages']['glibc']=value
                for row in parent['findings']:row['source_version']=value
            if key=='source_package':parent['inventory']['source_packages']['other']=parent['findings'][0]['source_version']
            with self.subTest(key=key):
                result=a.assess(parent,proof,self.config,self.now)
                self.assertEqual(result['reviewed_findings'],[]);self.assertFalse(result['qualified'])
                self.assertEqual(result['rejected_reviews'][0]['reason'],'advisory-record-or-source-changed')
        parent,proof=self.fixture();del parent['findings'][0]['canonical_record_sha256']
        self.assertEqual(a.assess(parent,proof,self.config,self.now)['reviewed_findings'],[])

    def test_changed_image_hash_size_or_typed_elf_identity_stays_blocking(self):
        for field,value in [('sha256','0'*64),('size',64),('bits',32),('byte_order','big'),('machine',21),
                            ('type',2),('osabi',0),('abi_version',False),('flags',False)]:
            parent,proof=self.fixture();name=sorted(a.IMAGES)[0];row=parent['inventory']['files'][name]
            if field in ('sha256','size'):row[field]=value;proof['files'][name][field]=value
            else:row['elf'][field]=value
            with self.subTest(field=field):
                result=a.assess(parent,proof,self.config,self.now)
                self.assertEqual(result['reviewed_findings'],[]);self.assertFalse(result['qualified'])
                self.assertEqual(result['rejected_reviews'][0]['reason'],'glibc-image-identity-changed')

    def test_exact_glibc_package_and_image_scope_cannot_expand_or_shrink(self):
        for kind in ('missing-image','extra-image','extra-package','package-bytes','architecture'):
            parent,proof=self.fixture();owner=self.config['review']['binary_package'];name=sorted(a.IMAGES)[0]
            if kind=='missing-image':del parent['inventory']['files'][name];del proof['files'][name]
            if kind=='extra-image':
                parent['inventory']['files']['/usr/lib/extra.so']=copy.deepcopy(parent['inventory']['files'][name])
                proof['files']['/usr/lib/extra.so']=copy.deepcopy(proof['files'][name])
            if kind=='extra-package':
                parent['inventory']['packages']['libc-bin:amd64']=copy.deepcopy(parent['inventory']['packages'][owner])
                proof['packages']['libc-bin:amd64']=copy.deepcopy(proof['packages'][owner])
            if kind=='package-bytes':
                proof['packages'][owner]['sha256']='0'*64
                for row in proof['files'].values():row['archive_sha256']='0'*64
            if kind=='architecture':
                parent['inventory']['packages'][owner]['architecture']='ppc64el';proof['packages'][owner]['architecture']='ppc64el'
            with self.subTest(kind=kind):
                result=a.assess(parent,proof,self.config,self.now)
                self.assertEqual(result['reviewed_findings'],[]);self.assertFalse(result['qualified'])

    def test_policy_elf_types_and_image_set_are_strict(self):
        name=sorted(a.IMAGES)[0]
        for kind in ('missing','extra','bool','machine','size','hash'):
            config=copy.deepcopy(self.config);images=config['review']['images']
            if kind=='missing':del images[name]
            if kind=='extra':images['/extra.so']=copy.deepcopy(images[name])
            if kind=='bool':images[name]['elf']['abi_version']=False
            if kind=='machine':images[name]['elf']['machine']=21
            if kind=='size':images[name]['size']=True
            if kind=='hash':images[name]['sha256']='bad'
            with self.subTest(kind=kind),self.assertRaises(a.u.Refusal):a.policy(config,self.now)

    def test_payload_must_be_complete_success_and_exactly_join_raw_candidate(self):
        for key,value in [('schema',True),('scope','other'),('qualified',False),('verification_status','failed'),
                          ('distribution_payloads_authenticated',False),('checkout',{}),('inputs',{}),
                          ('source_advisory_findings',[]),('advisory_findings',0),('advisory_gate_qualified',True),
                          ('packages',{}),('files',{})]:
            parent,proof=self.fixture();proof[key]=value
            with self.subTest(key=key),self.assertRaises(a.u.Refusal):a.assess(parent,proof,self.config,self.now)

    def test_mismatched_payload_file_and_package_joins_refuse(self):
        for field,value in [('sha256','0'*64),('size',64),('binary_package','other'),('archive_sha256','0'*64)]:
            parent,proof=self.fixture();proof['files'][sorted(a.IMAGES)[0]][field]=value
            with self.subTest(field=field),self.assertRaises(a.u.Refusal):a.assess(parent,proof,self.config,self.now)
        for field in ('name','version','architecture','source','source_version'):
            parent,proof=self.fixture();proof['packages']['libc6:amd64'][field]='other'
            with self.subTest(field=field),self.assertRaises(a.u.Refusal):a.assess(parent,proof,self.config,self.now)

    def test_duplicate_or_stale_raw_advisory_rows_refuse(self):
        for kind in ('duplicate','stale'):
            parent,proof=self.fixture()
            if kind=='duplicate':parent['findings'].append(copy.deepcopy(parent['findings'][0]));proof['advisory_findings']+=1
            else:parent['inventory']['source_packages']['glibc']='changed'
            with self.subTest(kind=kind),self.assertRaises(a.u.Refusal):a.assess(parent,proof,self.config,self.now)

    def test_personal_host_guard_precedes_all_reads_commands_and_outputs(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True),\
             patch.object(a.candidate,'read') as read,patch.object(a.u,'command') as command:
            root=Path(d)
            with self.assertRaisesRegex(a.u.Refusal,'hosted-ubuntu-required'):a.run(root/'candidate',root/'payload',root/'report')
            read.assert_not_called();command.assert_not_called();self.assertEqual(list(root.iterdir()),[])

    def execute(self,root,changed=None):
        parent,proof=self.fixture(other=False,source=False)
        candidate_path=root/'candidate';payload_path=root/'payload';report=root/'report'
        candidate_path.write_text(json.dumps(parent));raw=candidate_path.read_bytes()
        proof.update(candidate_report_sha256=a.u.digest(raw),archive_trust_policy_sha256='f'*64)
        payload_path.write_text(json.dumps(proof));praw=payload_path.read_bytes()
        cfgraw=(ROOT/a.POLICY).read_bytes();read=a.candidate.read
        def reading(path):
            if Path(path)==ROOT/a.POLICY:return cfgraw,copy.deepcopy(self.config)
            data,value=read(path)
            if changed=='parent' and Path(path)==candidate_path and reading.seen:
                return b'changed',value
            if Path(path)==candidate_path:reading.seen=True
            return data,value
        reading.seen=False
        def image(path):
            result=copy.deepcopy(self.config['review']['images'][str(path)])
            if changed=='image':result['sha256']='0'*64
            return result
        def installed(path,sha):
            row=proof['files'][str(path)]
            result={key:row[key] for key in ('sha256','size','uid','gid','mode')}
            if changed=='owner':result['uid']=1001
            return result
        command=lambda args:parent['checkout']['commit'] if 'rev-parse' in args else ''
        real_assess=a.assess
        times=[self.now,datetime(2026,11,24,tzinfo=timezone.utc) if changed=='expired-during-check' else self.now]
        with patch.object(a.u,'hosted'),patch.object(a.u,'command',side_effect=command),\
             patch.object(a,'current_time',side_effect=times),\
             patch.object(a.u,'file_digest',return_value='f'*64),patch.object(a.candidate,'read',side_effect=reading),\
             patch.object(a.candidate,'source_inputs',return_value=parent['inputs']),\
             patch.object(a.candidate,'validate_advisory_result'),patch.object(a.u,'image_identity',side_effect=image),\
             patch.object(a.payloads,'installed',side_effect=installed),\
             patch.object(a,'assess',side_effect=lambda p,q,c,now:real_assess(p,q,c,self.now)):
            if changed:
                with self.assertRaises(a.u.Refusal):a.run(candidate_path,payload_path,report)
            else:a.run(candidate_path,payload_path,report)
        self.assertEqual(candidate_path.read_bytes(),raw);self.assertEqual(payload_path.read_bytes(),praw)
        return json.loads(report.read_bytes()),parent

    def test_live_image_owner_and_parent_changes_cannot_publish_accepted_review(self):
        for changed in ('image','owner','parent','expired-during-check'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as d:
                result,parent=self.execute(Path(d),changed)
                self.assertFalse(result['qualified']);self.assertEqual(result['assessment_status'],'failed')
                self.assertEqual(result['reviewed_findings'],[])
                self.assertEqual(result['raw_distribution_findings'],parent['findings'])
                self.assertEqual(result['blocking_distribution_findings'],parent['findings'])
                self.assertTrue(all(result[name] is False for name in a.FLAGS))

    def test_run_success_keeps_original_parents_and_all_release_flags_false(self):
        with tempfile.TemporaryDirectory() as d:
            result,parent=self.execute(Path(d))
            self.assertTrue(result['qualified']);self.assertEqual(result['assessment_status'],'complete')
            self.assertFalse(result['raw_advisory_gate_qualified']);self.assertFalse(parent['qualified'])
            self.assertEqual(len(result['reviewed_findings']),1)
            self.assertTrue(all(result[name] is False for name in a.FLAGS))

    def test_existing_output_and_symlink_refuse_before_reading_parents(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a.u,'hosted'),patch.object(a.candidate,'read') as read:
            root=Path(d);report=root/'report';report.write_bytes(b'preserved')
            for path in (report,root/'link'):
                if path!=report:path.symlink_to(root/'missing')
                with self.assertRaisesRegex(a.u.Refusal,'new-applicability-report'):a.run(root/'candidate',root/'payload',path)
                self.assertEqual(report.read_bytes(),b'preserved');read.assert_not_called()

    def test_cli_exits_nonzero_for_retained_blockers_and_only_exports_counts(self):
        for qualified in (False,True):
            result=dict(qualified=qualified,reviewed_findings=[{}],blocking_distribution_findings=[] if qualified else [{}],blocking_source_findings=[])
            output=io.StringIO()
            with self.subTest(qualified=qualified),patch.object(sys,'argv',['applicability','--candidate','c','--payload','p','--report','r']),\
                 patch.object(a,'run',return_value=result),patch('sys.stdout',output):
                with self.assertRaises(SystemExit) as status:a.main()
                self.assertEqual(status.exception.code,0 if qualified else 1)
                self.assertEqual(output.getvalue(),'Native applicability: reviewed=1 blocking='+('0' if qualified else '1')+'\n')


if __name__=='__main__':unittest.main()
