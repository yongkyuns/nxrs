import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('gateway_media',ROOT/'tools/gateway-media/prepare.py')
media=importlib.util.module_from_spec(spec);spec.loader.exec_module(media)


class GatewayMedia(unittest.TestCase):
    def test_personal_host_refuses_before_commands_network_or_files(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d)/'absent'
            with patch.dict(media.os.environ,{},clear=True),patch.object(media.native,'command',side_effect=AssertionError('command ran')),patch.object(media.urllib.request,'urlopen',side_effect=AssertionError('network ran')):
                with self.assertRaises(media.native.Refusal):media.run(work)
            self.assertFalse(work.exists())

    def test_dependency_autodiscovery_and_external_families_remain_disabled(self):
        flags=media.configuration(media.RUNTIME)
        self.assertIn('--disable-autodetect',flags)
        self.assertIn('--disable-everything',flags)
        self.assertIn('--enable-rpath',flags)
        for flag in ['--disable-avdevice','--disable-swresample','--disable-vulkan','--disable-vaapi','--disable-vdpau']:
            self.assertIn(flag,flags)
        self.assertEqual(media.SELECTIONS['decoder'],['h264','mjpeg'])
        self.assertEqual(media.SELECTIONS['encoder'],['libx264','mjpeg','rawvideo'])

    def fixture(self,root):
        lines=[]
        for kind,names in media.SELECTIONS.items():
            lines += ['#define CONFIG_'+name.upper()+'_'+kind.upper()+' 1' for name in names]
        lines += ['#define CONFIG_'+name+' 0' for name in ['AUTODETECT','AVDEVICE','SWRESAMPLE','OPENSSL','GNUTLS','LIBXML2','LIBASS','SDL2']]
        (root/'config.h').write_text('\n'.join(lines)+'\n');(root/'config_components.h').write_text('')
        (root/'ffbuild').mkdir();(root/'ffbuild/config.mak').write_text('owned configuration\n')

    def test_generated_configuration_rejects_extra_decoder_hardware_network_and_missing_required_codec(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);base=(root/'config.h').read_text()
            self.assertEqual(media.resolved(root)['selected']['decoder'],['h264','mjpeg'])
            (root/'config.h').write_text(base+'#define CONFIG_FRAME_THREAD_ENCODER 1\n')
            self.assertTrue(media.resolved(root)['implementation_features']['frame_thread_encoder'])
            self.assertEqual(media.resolved(root)['selected']['encoder'],['libx264','mjpeg','rawvideo'])
            for change in ['#define CONFIG_HEVC_DECODER 1\n','#define CONFIG_CUDA_HWACCEL 1\n','#define CONFIG_HTTPS_PROTOCOL 1\n']:
                (root/'config.h').write_text(base+change)
                with self.assertRaises(media.native.Refusal):media.resolved(root)
            (root/'config.h').write_text(base.replace('#define CONFIG_H264_DECODER 1','#define CONFIG_H264_DECODER 0'))
            with self.assertRaises(media.native.Refusal):media.resolved(root)

    def test_generated_configuration_rejects_reenabled_external_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);base=(root/'config.h').read_text()
            for name in ['AUTODETECT','AVDEVICE','OPENSSL','GNUTLS','LIBXML2','LIBASS','SDL2']:
                (root/'config.h').write_text(base.replace('CONFIG_'+name+' 0','CONFIG_'+name+' 1'))
                with self.assertRaises(media.native.Refusal):media.resolved(root)


if __name__=='__main__':unittest.main()
