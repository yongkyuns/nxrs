"""Refusal controls for public SDK/media receipt composition."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import shutil
import sys
import unittest

from sdk_media_evidence import read_sdk, validate_media, public_failure


class SdkMediaEvidenceTests(unittest.TestCase):
    def test_full_staged_media_tree_contains_every_native_qualification_source(self):
        repository=Path(__file__).resolve().parents[2]
        sys.path.insert(0,str(repository/'gateway/tools'))
        from sdk_media_evidence import source_names
        from gateway_media_evidence import source_names as native_sources
        names=source_names(repository,dict(inputs={}))
        self.assertFalse(native_sources(repository)-names)
        with tempfile.TemporaryDirectory() as directory:
            staged=Path(directory)
            for name in names:
                target=staged/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(repository/name,target)
            self.assertEqual(native_sources(staged),native_sources(repository))
            for name in native_sources(staged):
                self.assertEqual(hashlib.sha256((staged/name).read_bytes()).digest(),hashlib.sha256((repository/name).read_bytes()).digest())

    def test_sdk_report_binds_source_bytes_and_endpoint_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.rs';source.write_bytes(b'owned source')
            camera=root/'camera';camera.write_bytes(b'owned executable')
            report=dict(schema=1,scope='actual-volatile-device-identity-host-sdk-and-issuer',qualified=True,
                device_tls_host_qualified=True,device_tls_capture_worker_host_qualified=True,device_tls_endpoint_lifecycle_host_qualified=True,
                physical_qualified=False,fips_validated=False,deployment_permitted=False,
                inputs={'source.rs':hashlib.sha256(source.read_bytes()).hexdigest()},
                device_tls_camera_fixture_sha256=hashlib.sha256(camera.read_bytes()).hexdigest())
            report['endpoint_controls']=dict(scope='actual-host-sdk-endpoint-live-controls',qualified=True,
                camera_sha256=report['device_tls_camera_fixture_sha256'],exact_jpegs_verified=8,
                signed_updates_applied=2,malformed_forged_rollback_refusals=3,entropy_refreshes=1,
                revoked_peer_media_frames=0,idle_stream_retired=True,reconnect_refused=True,joined_cleanup=True,
                physical_qualified=False,protected_sources_qualified=False,device_key_rotation_qualified=False,deployment_permitted=False)
            report.update(record_crypto_profile='chacha20-poly1305-only',variants={name:dict(compiled_unsafe_record_crypto_refusals=4) for name in ['normal','sanitized']})
            path=root/'sdk.json';path.write_text(json.dumps(report))
            self.assertEqual(read_sdk(root,path,camera)[1],report)
            for mutate in [lambda value:value.pop('record_crypto_profile'),
                           lambda value:value['variants']['normal'].update(compiled_unsafe_record_crypto_refusals=0),
                           lambda value:value['variants']['sanitized'].update(compiled_unsafe_record_crypto_refusals=True)]:
                bad=copy.deepcopy(report);mutate(bad);path.write_text(json.dumps(bad))
                with self.assertRaisesRegex(AssertionError,'record crypto profile'):read_sdk(root,path,camera)
            bad=copy.deepcopy(report);bad['endpoint_controls']['revoked_peer_media_frames']=1;path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(AssertionError,'endpoint lifecycle evidence'):read_sdk(root,path,camera)
            path.write_text(json.dumps(report))
            source.write_bytes(b'changed source')
            with self.assertRaisesRegex(AssertionError,'SDK source evidence changed'):read_sdk(root,path,camera)
            source.write_bytes(b'owned source');camera.write_bytes(b'changed executable')
            with self.assertRaisesRegex(AssertionError,'endpoint binary'):read_sdk(root,path,camera)

    def test_unqualified_oversize_and_escaping_sdk_reports_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);camera=root/'camera';camera.write_bytes(b'executable');path=root/'sdk.json'
            report=dict(schema=1,scope='actual-volatile-device-identity-host-sdk-and-issuer',qualified=True,
                device_tls_host_qualified=True,device_tls_capture_worker_host_qualified=True,device_tls_endpoint_lifecycle_host_qualified=True,
                physical_qualified=False,fips_validated=False,deployment_permitted=False,
                inputs={'../private':'a'*64},device_tls_camera_fixture_sha256='a'*64)
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(AssertionError,'canonical SDK source identity'):read_sdk(root,path,camera)
            report['inputs']={'/private':'a'*64};path.write_text(json.dumps(report))
            with self.assertRaisesRegex(AssertionError,'canonical SDK source identity'):read_sdk(root,path,camera)
            report['physical_qualified']=True;path.write_text(json.dumps(report))
            with self.assertRaisesRegex(AssertionError,'host SDK/capture qualification'):read_sdk(root,path,camera)
            path.write_bytes(b' '*65537)
            with self.assertRaisesRegex(AssertionError,'bounded public SDK evidence'):read_sdk(root,path,camera)

    def test_qualified_flag_cannot_replace_media_recovery_ownership_or_scope(self):
        valid=dict(schema=1,scope='actual-host-sdk-camera-gateway-media',qualified=True,
            sdk_endpoint_used=True,source_stall_reconnect_executed=True,
            credentials_absent_from_argv_environment=True,encoder_publisher_children_reaped=True,
            physical_qualified=False,fips_validated=False,deployment_permitted=False,
            device_tls_runtime_qualified=False,hardware_entropy_qualified=False,
            protected_device_key_storage_qualified=False,trusted_time_acquisition_qualified=False,
            protected_generation_qualified=False,initial_viewer_frames=[5,5],recovered_viewer_frames=5,
            source_connections=2,source_frames=100,source_peak=1,stopped_source_active=0,
            live_controls=dict(malformed_forged_rollback_refusals=3,entropy_refreshes=1,
                signed_update_reconnect_executed=True,signed_peer_revocation_stops_media=True,revoked_peer_media_frames=0,revocation_viewer_frames=5))
        validate_media(valid)
        changes=[('encoder_publisher_children_reaped',False),('initial_viewer_frames',[5,0]),
                 ('source_peak',2),('stopped_source_active',1),('recovered_viewer_frames',True),
                 ('source_connections',1),('physical_qualified',True),('source_frames',2**64),('live_controls',{})]
        for key,value in changes:
            with self.subTest(key=key):
                report=copy.deepcopy(valid);report[key]=value
                with self.assertRaises(AssertionError):validate_media(report)

    def test_native_media_proof_cannot_claim_unexecuted_resources_or_release_admission(self):
        # Reuse the complete behavioral contract; native proof cannot replace it.
        valid=dict(schema=1,scope='actual-host-sdk-camera-gateway-media',qualified=True,
            sdk_endpoint_used=True,source_stall_reconnect_executed=True,credentials_absent_from_argv_environment=True,
            encoder_publisher_children_reaped=True,physical_qualified=False,fips_validated=False,deployment_permitted=False,
            device_tls_runtime_qualified=False,hardware_entropy_qualified=False,protected_device_key_storage_qualified=False,
            trusted_time_acquisition_qualified=False,protected_generation_qualified=False,initial_viewer_frames=[5,5],
            recovered_viewer_frames=5,source_connections=2,source_frames=100,source_peak=1,stopped_source_active=0,
            live_controls=dict(malformed_forged_rollback_refusals=3,entropy_refreshes=1,signed_update_reconnect_executed=True,
                signed_peer_revocation_stops_media=True,revoked_peer_media_frames=0,revocation_viewer_frames=5),
            executables=dict(ffmpeg='a'*64,publisher='b'*64))
        proof=dict(schema=1,scope='qualified-host-minimized-media-runtime',qualified=True,
            checkout=dict(commit='c'*40,clean=True),sdk=dict(publisher='b'*64,reader='d'*64),
            upstream_advisory_closure=False,full_native_advisory_closure=False,physical_qualified=False,
            fips_validated=False,deployment_permitted=False,
            **{n:'a'*64 for n in ['build_sha256','qualification_sha256','isolation_sha256','ffmpeg_sha256',
                'ffprobe_sha256','runtime_payloads_sha256','inputs_sha256']})
        valid['media_runtime']=proof;validate_media(valid)
        for name,value in [('ffmpeg_sha256','e'*64),('physical_qualified',True),('qualified',False),('scope','unqualified')]:
            changed=copy.deepcopy(valid);changed['media_runtime'][name]=value
            with self.assertRaises(AssertionError):validate_media(changed)

    def test_failure_location_projects_known_sources_without_messages_paths_or_locals(self):
        from sdk_media_evidence import failure_locations,require
        try:require(False,'private-value never export /private/custody')
        except AssertionError as error:locations=failure_locations(error)
        self.assertTrue(locations)
        self.assertTrue(all(value['source']=='gateway/tests/sdk_media_evidence.py' for value in locations))
        raw=('SDK_MEDIA_FAILURE_LOCATION '+json.dumps(locations)+'\n').encode()
        result=public_failure(raw)
        self.assertEqual(result['source_locations'],locations);self.assertFalse(result['qualified'])
        self.assertNotIn('private-value',json.dumps(result));self.assertNotIn('/private/custody',json.dumps(result))
        invalid=[dict(source='/private/custody',line=1,error='AssertionError'),
                 dict(source='gateway/tests/sdk_media_evidence.py',line=True,error='AssertionError'),
                 dict(source='gateway/tests/sdk_media_evidence.py',line=1,error='private-value')]
        result=public_failure(('SDK_MEDIA_FAILURE_LOCATION '+json.dumps(invalid)+'\n').encode())
        self.assertNotIn('source_locations',result)

    def test_private_child_output_can_export_only_fixed_phase_and_error_categories(self):
        private=b'private-key=never-export-this /private/custody/file\n'
        result=public_failure(private+b'SDK_MEDIA_PHASE preflight\nINGEST REFERENCE FAILED: FileNotFoundError\n'+private)
        self.assertEqual(result['phase'],'preflight');self.assertEqual(result['reason'],'missing-file')
        self.assertFalse(result['qualified'])
        self.assertNotIn('never-export-this',json.dumps(result));self.assertNotIn('/private',json.dumps(result))
        unknown=public_failure(b'SDK_MEDIA_PHASE /private/custody\nINGEST REFERENCE FAILED: secret=private\n')
        self.assertEqual(unknown['phase'],'unknown');self.assertEqual(unknown['reason'],'unclassified')
        oversized=public_failure(b'x'*65536+b'\nSDK_MEDIA_PHASE execution\nINGEST REFERENCE FAILED: PermissionError\n')
        self.assertEqual(oversized['phase'],'unknown');self.assertEqual(oversized['reason'],'unclassified')


class RecordingFailureProjectionTests(unittest.TestCase):
    def test_initial_health_distinguishes_existing_clips_from_new_progress_without_private_fields(self):
        raw = ('SDK_RECORDING_OBSERVATIONS ' + json.dumps(dict(
            initial_health=dict(frames_observed=49, clips_created=3, token='private-value'),
            health=dict(frames_observed=49, clips_created=3),
            worker_stages=['segment interval too long', 'private-value'])) + '\n').encode()
        value = public_failure(raw)['recording']
        self.assertEqual(value['initial_health'], dict(frames_observed=49, clips_created=3))
        self.assertEqual(value['health'], dict(frames_observed=49, clips_created=3))
        self.assertEqual(value['worker_stages'], ['segment interval too long'])
        self.assertNotIn('private-value', json.dumps(value))

    def test_recording_failure_exports_only_fixed_categories_and_counters(self):
        raw=('SDK_RECORDING_OBSERVATIONS '+json.dumps(dict(
            health=dict(frames_observed=27,reconnects=5,token='private-value',clips_created=True),
            worker_stages=['reader exited','private-value','activity storage busy'],secret='private-value'))+
            '\nINGEST REFERENCE FAILED: actual TLS-ingested frames produce a new motion clip with observed child metadata\n').encode()
        value=public_failure(raw)
        self.assertEqual(value['reason'],'motion-clip')
        self.assertEqual(value['recording'],dict(health=dict(frames_observed=27,reconnects=5),
            worker_stages=['reader exited','activity storage busy']))
        self.assertNotIn('private-value',json.dumps(value))
        for data in [b'{invalid',b'null',b'[]',b'x'*2049]:
            value=public_failure(b'SDK_RECORDING_OBSERVATIONS '+data+b'\n')
            self.assertEqual(value.get('recording',dict(health={},worker_stages=[])),dict(health={},worker_stages=[]))


if __name__=='__main__':unittest.main()
