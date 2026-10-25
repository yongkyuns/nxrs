"""Actual origin classification must never relabel a missing or changed image."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/native-packages'))
import candidate
native=candidate.ubuntu


class CandidateNativeTests(unittest.TestCase):
    def test_personal_host_refuses_before_receipts_native_tools_or_queries(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True),patch.object(native,'command') as command,patch.object(native,'query') as query,patch.object(candidate,'runtime_inventory') as inventory:
            path=Path(d)/'absent.json'
            with self.assertRaises(native.Refusal):candidate.run(path,Path(d)/'baseline',Path(d)/'refresh')
            command.assert_not_called();query.assert_not_called();inventory.assert_not_called();self.assertFalse(path.exists())

    def test_actual_custom_bytes_are_classified_without_a_distribution_owner_query(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'libx264.so.165';path.write_bytes(b'owned native image');digest=hashlib.sha256(path.read_bytes()).hexdigest()
            source={path:dict(sha256=digest,source_component='x264')};packages={}
            with patch.object(native,'owner',side_effect=AssertionError('source must not become a distro package')):
                value=native.image_record(path,source,packages)
                self.assertEqual(value,dict(sha256=digest,source_component='x264',origin='pinned-source'));self.assertEqual(packages,{})
                path.write_bytes(b'changed native image')
                with self.assertRaisesRegex(native.Refusal,'unchanged-qualified-source-image'):native.image_record(path,source,packages)

    def test_custom_image_identity_cannot_hide_unknown_components_or_change_ownership(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'image';path.write_bytes(b'owned image');digest=hashlib.sha256(path.read_bytes()).hexdigest()
            for value in [dict(sha256=digest,source_component='unknown'),dict(sha256='bad',source_component='ffmpeg'),
                          dict(sha256=digest,source_component='ffmpeg',binary_package='invented')]:
                with self.assertRaises(native.Refusal):native.image_record(path,{path:value},{})
            with self.assertRaisesRegex(native.Refusal,'distribution-image-location'):native.image_record(path,{}, {})

    def test_distribution_image_keeps_actual_package_revision_and_is_not_reclassified(self):
        path=Path('/usr/lib/owned.so');packages={};package=dict(name='libowned1',version='1:2-1ubuntu1')
        with patch.object(native,'owner',return_value='libowned1:amd64') as owner,patch.object(native,'package',return_value=package),patch.object(native,'file_digest',return_value='a'*64):
            value=native.image_record(path,{},packages)
        owner.assert_called_once_with(path);self.assertEqual(value,dict(sha256='a'*64,binary_package='libowned1:amd64'))
        self.assertEqual(packages,{'libowned1:amd64':package})

    def build(self):
        names=['bin/ffmpeg','bin/ffprobe']+['lib/'+n+'.so.1' for n in ['libavcodec','libavformat','libavutil','libavfilter','libswscale','libx264']]
        return dict(runtime_files={n:'a'*64 for n in names})

    def test_exact_custom_roots_require_both_executables_and_every_library_family(self):
        build=self.build();images=candidate.source_images(build)
        self.assertEqual(len(images),8);self.assertEqual(sum(v['source_component']=='x264' for v in images.values()),1)
        for name in build['runtime_files']:
            changed=copy.deepcopy(build);changed['runtime_files'].pop(name)
            with self.assertRaises(native.Refusal):candidate.source_images(changed)
        changed=copy.deepcopy(build);changed['runtime_files']['lib/libavcodec.so.2']='b'*64
        with self.assertRaisesRegex(native.Refusal,'one-exact-custom'):candidate.source_images(changed)

    def test_actual_packages_must_equal_the_executed_refresh_not_just_name_match(self):
        actual=dict(packages={'libowned1:amd64':dict(version='1:2-1ubuntu1')})
        refreshed=dict(packages_after=copy.deepcopy(actual['packages']))
        candidate.require_refreshed(actual,refreshed)
        refreshed['packages_after']['libowned1:amd64']['version']='1:2-1ubuntu2'
        with self.assertRaises(native.Refusal):candidate.require_refreshed(actual,refreshed)
        with self.assertRaises(native.Refusal):candidate.require_refreshed(dict(packages={}),refreshed)

    def test_inventory_records_actual_roots_and_separates_both_origins(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();roots={name:root/name for name in [*native.SYSTEM_ROOTS,*native.REPOSITORY_ROOTS]}
            for name,path in roots.items():path.write_bytes(name.encode())
            image=root/'libavcodec.so.1';image.write_bytes(b'owned avcodec');system=Path('/usr/lib/owned-libc.so')
            all_custom={path:dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),source_component='ffmpeg') for path in [image,*(roots[n] for n in native.SYSTEM_ROOTS)]}
            package=dict(name='libc6',source='glibc',version='2.39-0ubuntu1',source_version='2.39-0ubuntu1')
            def digest(path):return 'b'*64 if path==system else hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(native,'mapped_images',return_value={system}),patch.object(native,'dependencies',return_value={image,system}),patch.object(native,'owner',return_value='libc6:amd64'),patch.object(native,'package',return_value=package),patch.object(native,'file_digest',side_effect=digest):
                value=native.inventory(roots,all_custom)
            self.assertEqual(value['source_packages'],{'glibc':'2.39-0ubuntu1'});self.assertEqual(len(value['packages']),1)
            self.assertEqual(value['files'][str(image)]['origin'],'pinned-source')
            self.assertEqual(value['files'][str(system)]['binary_package'],'libc6:amd64')
            self.assertEqual(value['roots']['publisher']['origin'],'repository-build')
            self.assertEqual(value['roots']['ffmpeg']['origin'],'pinned-source')

    def test_refresh_receipts_bind_exact_checkout_sources_baseline_and_changed_packages(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);baseline=root/'baseline';refresh=root/'refresh';head='a'*40;inputs={'owned':'b'*64}
            before={'libowned1:amd64':dict(version='1:2-1ubuntu1')};after={'libowned1:amd64':dict(version='1:2-1ubuntu2')}
            b=dict(schema=1,scope='hosted-ubuntu-native-gateway-startup-images-and-system-python-tls',
                advisory_status='complete',qualified=False,checkout=dict(clean=True,commit=head),inputs=inputs,inventory=dict(packages=before))
            baseline.write_text(json.dumps(b));r=dict(schema=1,scope='hosted-native-package-refresh-not-security-admission',
                qualified=False,package_refresh_complete=True,checkout=dict(clean=True,commit=head),inputs=inputs,
                baseline_sha256=hashlib.sha256(baseline.read_bytes()).hexdigest(),packages_before=before,packages_after=after,
                changed_packages=['libowned1:amd64'])
            refresh.write_text(json.dumps(r))
            with patch.object(native,'source_inputs',return_value=inputs):
                self.assertEqual(candidate.refresh_evidence(baseline,refresh,head)[1],r)
                for key,value in [('baseline_sha256','c'*64),('qualified',True),('package_refresh_complete',False),
                                  ('inputs',{}),('changed_packages',[]),('checkout',dict(clean=False,commit=head))]:
                    invalid=dict(r,**{key:value});refresh.write_text(json.dumps(invalid))
                    with self.subTest(key=key),self.assertRaises(native.Refusal):candidate.refresh_evidence(baseline,refresh,head)
                refresh.write_text(json.dumps(r));baseline.write_text(json.dumps(dict(b,advisory_status='failed')))
                with self.assertRaises(native.Refusal):candidate.refresh_evidence(baseline,refresh,head)

    def test_parent_receipt_cannot_be_a_symlink_fifo_or_duplicate_key_document(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'receipt';path.write_text('{}');alias=root/'alias';alias.symlink_to(path)
            with self.assertRaises(OSError):candidate.read(alias)
            path.write_text('{"schema":1,"schema":1}')
            with self.assertRaises(native.Refusal):candidate.read(path)
            path.unlink();os.mkfifo(path,0o600)
            with self.assertRaisesRegex(native.Refusal,'regular-candidate'):candidate.read(path)


if __name__=='__main__':unittest.main()
