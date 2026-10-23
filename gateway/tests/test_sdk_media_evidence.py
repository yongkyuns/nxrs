"""Refusal controls for public SDK/media receipt composition."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sdk_media_evidence import read_sdk, validate_media


class SdkMediaEvidenceTests(unittest.TestCase):
    def test_sdk_report_binds_source_bytes_and_endpoint_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.rs';source.write_bytes(b'owned source')
            camera=root/'camera';camera.write_bytes(b'owned executable')
            report=dict(schema=1,scope='actual-volatile-device-identity-host-sdk-and-issuer',qualified=True,
                device_tls_host_qualified=True,device_tls_capture_worker_host_qualified=True,
                physical_qualified=False,fips_validated=False,deployment_permitted=False,
                inputs={'source.rs':hashlib.sha256(source.read_bytes()).hexdigest()},
                device_tls_camera_fixture_sha256=hashlib.sha256(camera.read_bytes()).hexdigest())
            path=root/'sdk.json';path.write_text(json.dumps(report))
            self.assertEqual(read_sdk(root,path,camera)[1],report)
            source.write_bytes(b'changed source')
            with self.assertRaisesRegex(AssertionError,'SDK source evidence changed'):read_sdk(root,path,camera)
            source.write_bytes(b'owned source');camera.write_bytes(b'changed executable')
            with self.assertRaisesRegex(AssertionError,'endpoint binary'):read_sdk(root,path,camera)

    def test_unqualified_oversize_and_escaping_sdk_reports_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);camera=root/'camera';camera.write_bytes(b'executable');path=root/'sdk.json'
            report=dict(schema=1,scope='actual-volatile-device-identity-host-sdk-and-issuer',qualified=True,
                device_tls_host_qualified=True,device_tls_capture_worker_host_qualified=True,
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
            source_connections=2,source_frames=100,source_peak=1,stopped_source_active=0)
        validate_media(valid)
        changes=[('encoder_publisher_children_reaped',False),('initial_viewer_frames',[5,0]),
                 ('source_peak',2),('stopped_source_active',1),('recovered_viewer_frames',True),
                 ('source_connections',1),('physical_qualified',True),('source_frames',2**64)]
        for key,value in changes:
            with self.subTest(key=key):
                report=copy.deepcopy(valid);report[key]=value
                with self.assertRaises(AssertionError):validate_media(report)


if __name__=='__main__':unittest.main()
