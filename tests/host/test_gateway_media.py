import importlib.util
import hashlib
import http.client
import io
from pathlib import Path
import socket
import ssl
import tarfile
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('gateway_media',ROOT/'tools/gateway-media/prepare.py')
media=importlib.util.module_from_spec(spec);spec.loader.exec_module(media)


class GatewayMedia(unittest.TestCase):
    def response(self,body,length=None,url='https://example.invalid/PRIVATE-URL'):
        length=len(body) if length is None else length
        wire=b'HTTP/1.1 200 OK\r\nContent-Length: '+str(length).encode()+b'\r\n\r\n'+body
        class Socket:
            def makefile(self,*args):return io.BytesIO(wire)
        response=http.client.HTTPResponse(Socket());response.url=url;response.begin()
        return response

    def archive(self):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w') as archive:
            item=tarfile.TarInfo('source/README');body=b'PRIVATE SOURCE CONTENT';item.size=len(body)
            archive.addfile(item,io.BytesIO(body))
        raw=output.getvalue()
        return raw,dict(url='https://example.invalid/PRIVATE-URL',sha256=hashlib.sha256(raw).hexdigest(),root='source')

    def test_transient_timeout_and_dns_retry_same_pinned_url_then_extract_real_archive(self):
        raw,pin=self.archive();output=io.StringIO();response=self.response(raw)
        errors=[urllib.error.URLError(TimeoutError('PRIVATE REASON')),
                urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN,'PRIVATE DNS')),response]
        with tempfile.TemporaryDirectory() as d,patch.object(media,'open_source',side_effect=errors) as opening, \
             patch.object(media.time,'sleep') as sleeping,redirect_stdout(output):
            result=media.fetch(pin,Path(d),'source')
            self.assertEqual((result/'README').read_bytes(),b'PRIVATE SOURCE CONTENT')
        self.assertEqual(opening.call_count,3)
        self.assertTrue(all(call.args==(pin['url'],) and call.kwargs==dict(timeout=45) for call in opening.call_args_list))
        self.assertEqual([call.args for call in sleeping.call_args_list],[(1,),(2,)])
        self.assertTrue(response.closed)
        self.assertEqual(output.getvalue().splitlines(),['GATEWAY_MEDIA_SOURCE_RETRY timeout attempt=1',
            'GATEWAY_MEDIA_SOURCE_RETRY dns-transient attempt=2'])
        self.assertNotIn('PRIVATE',output.getvalue())

    def test_transient_http_retries_close_error_responses(self):
        raw,pin=self.archive();bodies=[io.BytesIO(b'PRIVATE SERVER BODY') for _ in range(2)]
        errors=[urllib.error.HTTPError(pin['url'],code,'PRIVATE REASON',{},body) for code,body in zip((429,503),bodies)]
        with tempfile.TemporaryDirectory() as d,patch.object(media,'open_source',side_effect=[*errors,self.response(raw)]) as opening, \
             patch.object(media.time,'sleep'),redirect_stdout(io.StringIO()):
            self.assertEqual((media.fetch(pin,Path(d),'source')/'README').read_bytes(),b'PRIVATE SOURCE CONTENT')
        self.assertEqual(opening.call_count,3)
        self.assertTrue(all(body.closed for body in bodies))

    def test_interrupted_body_closes_before_retry_and_never_stages_partial_bytes(self):
        raw,pin=self.archive()
        class Interrupted(io.BytesIO):
            def geturl(self):return pin['url']
            def read(self,limit):raise http.client.IncompleteRead(b'PRIVATE PARTIAL RESPONSE',10)
        failed=Interrupted(raw);complete=self.response(raw)
        with tempfile.TemporaryDirectory() as d,patch.object(media,'open_source',side_effect=[failed,complete]) as opening, \
             patch.object(media.time,'sleep') as sleeping,redirect_stdout(io.StringIO()) as output:
            def before_retry(_):
                self.assertTrue(failed.closed)
                self.assertEqual(list(Path(d).iterdir()),[])
            sleeping.side_effect=before_retry
            self.assertEqual((media.fetch(pin,Path(d),'source')/'README').read_bytes(),b'PRIVATE SOURCE CONTENT')
        self.assertEqual(opening.call_count,2);self.assertTrue(complete.closed)
        self.assertEqual(output.getvalue(),'GATEWAY_MEDIA_SOURCE_RETRY truncated-response attempt=1\n')

    def test_real_http_response_short_declared_body_retries_before_staging_or_extracting(self):
        raw,pin=self.archive()
        failed=self.response(raw[:17],len(raw));complete=self.response(raw)
        with tempfile.TemporaryDirectory() as d,patch.object(media,'open_source',side_effect=[failed,complete]) as opening, \
             patch.object(media.time,'sleep') as sleeping,redirect_stdout(io.StringIO()) as output:
            def before_retry(_):
                self.assertTrue(failed.closed)
                self.assertEqual(list(Path(d).iterdir()),[])
            sleeping.side_effect=before_retry
            self.assertEqual((media.fetch(pin,Path(d),'source')/'README').read_bytes(),b'PRIVATE SOURCE CONTENT')
        self.assertEqual(opening.call_count,2);self.assertTrue(complete.closed)
        self.assertEqual(output.getvalue(),'GATEWAY_MEDIA_SOURCE_RETRY truncated-response attempt=1\n')

    def test_real_http_response_truncation_exhausts_three_attempts_without_archive(self):
        raw,pin=self.archive();responses=[self.response(raw[:17],len(raw)) for _ in range(3)]
        with tempfile.TemporaryDirectory() as d,patch.object(media,'open_source',side_effect=responses) as opening, \
             patch.object(media.time,'sleep') as sleeping,patch.object(media.extractor,'extract') as extract,redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(media.native.Refusal,'gateway-source-download-truncated-response'):
                media.fetch(pin,Path(d),'source')
            self.assertEqual(opening.call_count,3);self.assertEqual(sleeping.call_count,2)
            self.assertTrue(all(response.closed for response in responses))
            extract.assert_not_called();self.assertEqual(list(Path(d).iterdir()),[])

    def test_declared_length_never_raises_archive_cap_or_retries_invalid_hash(self):
        _,pin=self.archive()
        for length in [0,5,32*1024*1024+1,10**30,'invalid']:
            with self.subTest(length=length),tempfile.TemporaryDirectory() as d, \
                 patch.object(media,'open_source',return_value=self.response(b'wrong',length)) as opening, \
                 patch.object(media.time,'sleep') as sleeping,patch.object(media.extractor,'extract') as extract:
                with self.assertRaisesRegex(media.native.Refusal,'pinned-source-archive'):media.fetch(pin,Path(d),'source')
                self.assertEqual(opening.call_count,1);sleeping.assert_not_called();extract.assert_not_called()
                self.assertEqual(list(Path(d).iterdir()),[])

    def test_non_https_redirect_refuses_even_matching_archive_before_read_or_staging(self):
        raw,pin=self.archive()
        for url in ['http://example.invalid/PRIVATE-DOWNGRADE','ftp://example.invalid/PRIVATE','file:///PRIVATE']:
            response=self.response(raw,url=url)
            with self.subTest(scheme=url.split(':')[0]),tempfile.TemporaryDirectory() as d, \
                 patch.object(media,'open_source',return_value=response) as opening, \
                 patch.object(response,'read',side_effect=AssertionError('body read before transport admission')) as reading, \
                 patch.object(media.time,'sleep') as sleeping,patch.object(media.extractor,'extract') as extract,redirect_stdout(io.StringIO()) as output:
                with self.assertRaisesRegex(media.native.Refusal,'https-source-redirect') as caught:media.fetch(pin,Path(d),'source')
                self.assertNotIn('PRIVATE',str(caught.exception));self.assertEqual(output.getvalue(),'')
                self.assertEqual(opening.call_count,1);reading.assert_not_called();sleeping.assert_not_called();extract.assert_not_called()
                self.assertTrue(response.closed);self.assertEqual(list(Path(d).iterdir()),[])

    def test_real_redirect_handler_closes_and_refuses_before_insecure_request(self):
        handler=media.SourceRedirects()
        opener=media.urllib.request.build_opener(handler)
        request=media.urllib.request.Request('https://example.invalid/PRIVATE')
        for code in [301,302,303,307,308]:
            with self.subTest(code=code):
                body=io.BytesIO(b'PRIVATE REDIRECT BODY')
                with patch.object(opener,'open',side_effect=AssertionError('insecure request issued')) as redirected, \
                     patch.object(body,'read',side_effect=AssertionError('redirect body read')) as reading:
                    with self.assertRaisesRegex(media.native.Refusal,'https-source-redirect'):
                        handler.http_error_302(request,body,code,'PRIVATE',{'location':'http://example.invalid/PRIVATE'})
                    redirected.assert_not_called();reading.assert_not_called()
                self.assertTrue(body.closed)

    def test_real_redirect_handler_preserves_https_and_default_tls_handler(self):
        handler=media.SourceRedirects();request=media.urllib.request.Request('https://example.invalid/PRIVATE')
        redirected=handler.redirect_request(request,io.BytesIO(),302,'Found',{},'https://cdn.example.invalid/PRIVATE')
        self.assertEqual(redirected.full_url,'https://cdn.example.invalid/PRIVATE')
        opener=media.urllib.request.build_opener(handler)
        https=[item for item in opener.handlers if isinstance(item,media.urllib.request.HTTPSHandler)]
        self.assertEqual(len(https),1)
        self.assertEqual(https[0]._context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(https[0]._context.check_hostname)
        with patch.object(media.urllib.request,'build_opener',return_value=opener) as building, \
             patch.object(opener,'open',return_value=io.BytesIO(b'body')) as opening:
            response=media.open_source(request.full_url,timeout=45);response.close()
        self.assertIsInstance(building.call_args.args[0],media.SourceRedirects)
        opening.assert_called_once_with(request.full_url,timeout=45)

    def test_https_redirect_still_requires_original_archive_hash(self):
        raw,pin=self.archive()
        for body in [raw,b'changed source']:
            response=self.response(body,url='https://cdn.example.invalid/PRIVATE-REDIRECT')
            with self.subTest(valid=body==raw),tempfile.TemporaryDirectory() as d, \
                 patch.object(media,'open_source',return_value=response) as opening, \
                 patch.object(media.time,'sleep') as sleeping:
                if body==raw:self.assertEqual((media.fetch(pin,Path(d),'source')/'README').read_bytes(),b'PRIVATE SOURCE CONTENT')
                else:
                    with self.assertRaisesRegex(media.native.Refusal,'pinned-source-archive'):media.fetch(pin,Path(d),'source')
                    self.assertEqual(list(Path(d).iterdir()),[])
                self.assertTrue(response.closed);self.assertEqual(opening.call_count,1);sleeping.assert_not_called()

    def test_certificate_tls_permanent_dns_http_and_unknown_fail_without_retry_or_output(self):
        _,pin=self.archive()
        errors=[urllib.error.URLError(ssl.SSLCertVerificationError(1,'PRIVATE CERTIFICATE')),
                urllib.error.URLError(ssl.SSLError(1,'PRIVATE TLS')),
                urllib.error.URLError(socket.gaierror(socket.EAI_NONAME,'PRIVATE DNS')),
                urllib.error.URLError('PRIVATE UNKNOWN'),
                *(urllib.error.HTTPError(pin['url'],code,'PRIVATE SERVER',{},io.BytesIO(b'PRIVATE BODY')) for code in (400,401,403,404))]
        for error in errors:
            with self.subTest(kind=type(error).__name__),tempfile.TemporaryDirectory() as d, \
                 patch.object(media,'open_source',side_effect=error) as opening, \
                 patch.object(media.time,'sleep') as sleeping,patch.object(media.extractor,'extract') as extract,redirect_stdout(io.StringIO()) as output:
                with self.assertRaisesRegex(media.native.Refusal,'gateway-source-download-') as caught:media.fetch(pin,Path(d),'source')
                self.assertNotIn('PRIVATE',str(caught.exception));self.assertEqual(output.getvalue(),'')
                self.assertEqual(opening.call_count,1);sleeping.assert_not_called();extract.assert_not_called()
                self.assertEqual(list(Path(d).iterdir()),[])
                if isinstance(error,urllib.error.HTTPError):self.assertTrue(error.fp.closed)

    def test_retry_exhaustion_is_bounded_and_never_stages_partial_source(self):
        _,pin=self.archive()
        for error in [urllib.error.URLError(TimeoutError('PRIVATE')),ConnectionResetError('PRIVATE'),
                      http.client.IncompleteRead(b'PRIVATE PARTIAL',100)]:
            with self.subTest(kind=type(error).__name__),tempfile.TemporaryDirectory() as d, \
                 patch.object(media,'open_source',side_effect=error) as opening, \
                 patch.object(media.time,'sleep') as sleeping,patch.object(media.extractor,'extract') as extract,redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(media.native.Refusal):media.fetch(pin,Path(d),'source')
                self.assertEqual(opening.call_count,3);self.assertEqual(sleeping.call_count,2)
                extract.assert_not_called();self.assertEqual(list(Path(d).iterdir()),[])
                self.assertNotIn('PRIVATE',output.getvalue())

    def test_hash_or_size_refusal_never_retries_or_extracts(self):
        raw,pin=self.archive()
        for body in [b'changed source',b'x'*(32*1024*1024+1)]:
            with self.subTest(size=len(body)),tempfile.TemporaryDirectory() as d, \
                 patch.object(media,'open_source',return_value=self.response(body)) as opening, \
                 patch.object(media.time,'sleep') as sleeping,patch.object(media.extractor,'extract') as extract:
                with self.assertRaisesRegex(media.native.Refusal,'pinned-source-archive'):media.fetch(pin,Path(d),'source')
                self.assertEqual(opening.call_count,1);sleeping.assert_not_called();extract.assert_not_called()
                self.assertEqual(list(Path(d).iterdir()),[])

    def test_personal_host_refuses_before_commands_network_or_files(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d)/'absent'
            with patch.dict(media.os.environ,{},clear=True),patch.object(media.native,'command',side_effect=AssertionError('command ran')),patch.object(media,'open_source',side_effect=AssertionError('network ran')):
                with self.assertRaises(media.native.Refusal):media.run(work)
            self.assertFalse(work.exists())

    def test_actual_file_link_inventory_roundtrips_through_the_runtime_verifier(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            runtime=Path(d).resolve();(runtime/'bin').mkdir();(runtime/'lib').mkdir()
            for name in ['ffmpeg','ffprobe']:(runtime/'bin'/name).write_bytes(('owned '+name).encode())
            library=runtime/'lib/library.so.1';library.write_bytes(b'owned library')
            alias=runtime/'lib/library.so';alias.symlink_to('library.so.1')
            # Loader-output parsing has its own native-package controls. Here
            # real file/alias collection must join that result without mixing it.
            with patch.object(media.native,'dependencies',return_value={library}):
                inventory=media.inventory_runtime(runtime)
            encoded=json.loads(json.dumps(inventory))
            self.assertEqual(media.evidence.check_payloads(runtime,encoded),inventory['runtime_files'])
            replacement=runtime/'lib/library.so.2';replacement.write_bytes(library.read_bytes())
            alias.unlink();alias.symlink_to('library.so.2')
            with self.assertRaises(ValueError):media.evidence.check_payloads(runtime,encoded)

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

    def test_annotated_h264_output_requires_its_own_muxer_as_well_as_the_demuxer(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);base=(root/'config.h').read_text()
            self.assertIn('h264',media.resolved(root)['selected']['muxer'])
            self.assertIn('h264',media.resolved(root)['selected']['demuxer'])
            self.assertIn('h264',next(flag.split('=',1)[1].split(',') for flag in media.configuration(media.RUNTIME)
                if flag.startswith('--enable-muxer=')))
            (root/'config.h').write_text(base.replace('#define CONFIG_H264_MUXER 1','#define CONFIG_H264_MUXER 0'))
            with self.assertRaises(media.native.Refusal):media.resolved(root)

    def test_generated_configuration_rejects_reenabled_external_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root);base=(root/'config.h').read_text()
            for name in ['AUTODETECT','AVDEVICE','OPENSSL','GNUTLS','LIBXML2','LIBASS','SDL2']:
                (root/'config.h').write_text(base.replace('CONFIG_'+name+' 0','CONFIG_'+name+' 1'))
                with self.assertRaises(media.native.Refusal):media.resolved(root)


if __name__=='__main__':unittest.main()
