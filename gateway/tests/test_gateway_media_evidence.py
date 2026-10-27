"""Refuse cross-runtime, partial or changed native media qualification inputs."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'gateway/tools'))
import gateway_media_evidence as media


class NativeRuntimeEvidence(unittest.TestCase):
    def test_reader_and_parent_bind_the_actual_local_cargo_dependency_graph(self):
        import importlib.util,tomllib
        spec=importlib.util.spec_from_file_location('reader_source_graph',ROOT/'gateway/tests/reader_reference.py')
        reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)
        observed=reader.source_names(ROOT)
        self.assertEqual(observed,media.reader_source_names(ROOT))
        visited=set();pending=[ROOT/'gateway/ingest'];required=set()
        while pending:
            package=pending.pop().resolve()
            if package in visited:continue
            visited.add(package)
            manifest=package/'Cargo.toml';required.add(str(manifest.relative_to(ROOT)))
            required.update(str(p.relative_to(ROOT)) for p in (package/'src').rglob('*.rs'))
            value=tomllib.loads(manifest.read_text())
            for dependency in value.get('dependencies',{}).values():
                if isinstance(dependency,dict) and 'path' in dependency:
                    pending.append(package/dependency['path'])
        self.assertIn('service/detection-wire/Cargo.toml',required)
        self.assertIn('service/detection-wire/src/lib.rs',required)
        self.assertTrue(required<=observed)
        self.assertIn('gateway/tools/gateway_media_evidence.py',observed)
        import ast
        tree=ast.parse((ROOT/'gateway/tests/qualify_media_sandbox.py').read_bytes())
        isolation=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
                       and any(isinstance(target,ast.Name) and target.id=='INPUTS' for target in node.targets))
        self.assertEqual(set(media.INPUTS)|observed|set(isolation),media.source_names(ROOT))
        self.assertEqual(media.source_names(ROOT),set(media.INPUTS)|observed|{
            'gateway/tests/qualify_media_sandbox.py','gateway/tests/media_sandbox/probe.c',
            'tests/device-identity/frame.jpg'})

    def fixture(self,temporary):
        base=Path(temporary);repo=base/'source';runtime=base/'runtime';runtime.mkdir();repo.mkdir()
        names=media.source_names(ROOT)
        for name in names:
            target=repo/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
        files={}
        for name in ['bin/ffmpeg','bin/ffprobe','lib/libavcodec.so.63','lib/libavformat.so.63','lib/libavutil.so.61','lib/libx264.so.165']:
            path=runtime/name;path.parent.mkdir(exist_ok=True);path.write_bytes(('owned '+name).encode());files[name]=media.digest(path)
        (runtime/'lib/libavcodec.so').symlink_to('libavcodec.so.63')
        publisher=base/'publisher';reader=base/'reader'
        publisher.write_bytes(b'owned publisher');reader.write_bytes(b'owned reader')
        checkout=dict(commit='a'*40,clean=True)
        flags=dict(physical_qualified=False,fips_validated=False,deployment_permitted=False,upstream_advisory_closure=False,
            full_native_advisory_closure=False,optimized_assembly_fuzz_qualified=False,complete_native_parser_coverage=False)
        libmap={n.removeprefix('lib/'):h for n,h in files.items() if n.startswith('lib/')}
        build=dict(schema=1,scope='hosted-minimized-gateway-media-build-not-release-admission',qualified=False,
            build_complete=True,checkout=checkout,inputs={n:media.digest(repo/n) for n in media.INPUTS},
            sources=dict(ffmpeg=json.loads((repo/'security/native-media-dependencies.json').read_bytes())['ffmpeg'],
                         x264=json.loads((repo/'tools/gateway-media/dependencies.json').read_bytes())['x264']),
            runtime_files=files,runtime_links={'lib/libavcodec.so':'libavcodec.so.63'},roots={n:dict(sha256=files['bin/'+n],libraries={p:h for p,h in files.items() if p.startswith('lib/')}) for n in ['ffmpeg','ffprobe']},
            resolved=dict(selected=dict(decoder=['h264','mjpeg'],encoder=['libx264','mjpeg','rawvideo'],muxer=['h264','mpegts'],hwaccel=[],indev=[],outdev=[])),**flags)
        build['x264_cleanup']=dict(applied=True,**build['sources']['x264']['cleanup'])
        q=dict(schema=1,scope='actual-minimized-host-gateway-media-sdk-isolation-and-clip',qualified=True,checkout=checkout,
            unchanged_isolation_grants=True,native_decoder_open_observer_executed=True,decoded_clip_frames=96,
            annexb_encoded_frames=16,annexb_access_units=16,
            inputs={n:media.digest(repo/n) for n in names},isolation_sha256='b'*64,
            sdk={n:dict(sha256=media.digest(p),libraries=libmap) for n,p in [('publisher',publisher),('reader',reader)]},**flags)
        def save():
            raw=media.encoded(build);(runtime/'build.json').write_bytes(raw);q['build_sha256']=hashlib.sha256(raw).hexdigest()
            (runtime/'qualification.json').write_bytes(media.encoded(q))
        save()
        resources=dict(ffmpeg=runtime/'bin/ffmpeg',publisher=publisher,reader=reader)
        return repo,runtime,resources,build,q,save

    def test_accepts_exact_payloads_and_refuses_changed_executables_or_library(self):
        with tempfile.TemporaryDirectory() as temp:
            repo,runtime,resources,build,q,save=self.fixture(temp)
            valid=media.verify(repo,runtime,**resources);self.assertTrue(valid['qualified']);self.assertFalse(valid['deployment_permitted'])
            for path in [resources['ffmpeg'],resources['publisher'],resources['reader'],runtime/'lib/libavcodec.so.63']:
                before=path.read_bytes();path.write_bytes(before+b'changed')
                with self.assertRaises(ValueError):media.verify(repo,runtime,**resources)
                path.write_bytes(before)
            link=runtime/'lib/libavcodec.so';link.unlink();link.symlink_to('libavformat.so.63')
            with self.assertRaisesRegex(ValueError,'unchanged-native-payload-inventory'):media.verify(repo,runtime,**resources)
            link.unlink();link.symlink_to('libavcodec.so.63')
            extra=runtime/'lib/unobserved.so';extra.write_bytes(b'unrecorded payload')
            with self.assertRaisesRegex(ValueError,'unchanged-native-payload-inventory'):media.verify(repo,runtime,**resources)
            extra.unlink()
            self.assertEqual(media.verify(repo,runtime,**resources),valid)

    def test_refuses_partial_changed_or_escaping_source_inventories(self):
        with tempfile.TemporaryDirectory() as temp:
            repo,runtime,resources,build,q,save=self.fixture(temp);original=copy.deepcopy(q)
            for replacement in [{},dict(q['inputs'],**{'../outside':'c'*64})]:
                q['inputs']=replacement;save()
                with self.assertRaises(ValueError):media.verify(repo,runtime,**resources)
            q.clear();q.update(original);save()
            path=repo/'gateway/ingest/src/media/ffi/mod.rs';path.write_bytes(path.read_bytes()+b'\nchanged')
            with self.assertRaisesRegex(ValueError,'current-native-source'):media.verify(repo,runtime,**resources)

    def test_refuses_changed_scope_stale_checkout_partial_execution_and_unqualified_build(self):
        with tempfile.TemporaryDirectory() as temp:
            repo,runtime,resources,build,q,save=self.fixture(temp);original=copy.deepcopy(q)
            changes=[('qualified',False),('physical_qualified',True),('full_native_advisory_closure',True),
                     ('unchanged_isolation_grants',False),('decoded_clip_frames',1),
                     ('annexb_encoded_frames',0),('annexb_access_units',15),
                     ('checkout',dict(commit='c'*40,clean=True)),('native_decoder_open_observer_executed',False)]
            for name,value in changes:
                q.clear();q.update(copy.deepcopy(original));q[name]=value;save()
                with self.assertRaises(ValueError):media.verify(repo,runtime,**resources)
            q.clear();q.update(original)
            build['x264_cleanup']['applied']=False;save()
            with self.assertRaisesRegex(ValueError,'executed-x264-cleanup'):media.verify(repo,runtime,**resources)
            build['x264_cleanup']['applied']=True
            build['qualified']=True;save()
            with self.assertRaises(ValueError):media.verify(repo,runtime,**resources)

    def test_mpegts_only_or_missing_annexb_witness_cannot_admit_annotated_encoding(self):
        with tempfile.TemporaryDirectory() as temp:
            repo,runtime,resources,build,q,save=self.fixture(temp)
            for field in ['annexb_encoded_frames','annexb_access_units']:
                value=q.pop(field);save()
                with self.assertRaisesRegex(ValueError,'executed-native-runtime'):media.verify(repo,runtime,**resources)
                q[field]=value
            build['resolved']['selected']['muxer']=['mpegts'];save()
            with self.assertRaisesRegex(ValueError,'bounded-native-codecs'):media.verify(repo,runtime,**resources)

    def test_refuses_unjoined_truncated_duplicate_and_nonregular_public_receipts(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            repo,runtime,resources,build,q,save=self.fixture(temp);path=runtime/'qualification.json'
            q['build_sha256']='c'*64;path.write_bytes(media.encoded(q))
            with self.assertRaisesRegex(ValueError,'build-qualification-join'):media.verify(repo,runtime,**resources)
            for raw in [b'{}'*32769,b'{"schema":1,"schema":1}']:
                path.write_bytes(raw)
                with self.assertRaises(ValueError):media.verify(repo,runtime,**resources)
            path.unlink();os.mkfifo(path,0o600)
            with self.assertRaisesRegex(ValueError,'regular-native-evidence'):media.verify(repo,runtime,**resources)


if __name__=='__main__':unittest.main()
