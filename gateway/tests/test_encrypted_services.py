"""Generated service wiring, process credential pipes, admission and evidence scope."""
import copy,importlib.util,io,json,os,re,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import encrypted_service_reference as e
import systemd_recording_source as source

class EncryptedServiceTests(unittest.TestCase):
    def test_personal_host_refuses_before_commands_source_reads_or_allocation(self):
        with patch.dict(os.environ,{},clear=True),patch.object(e,'command') as command,patch.object(e,'source_inputs') as inputs,\
             patch.object(Path,'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError,'hosted-linux-root-required'):e.run(types.SimpleNamespace())
            command.assert_not_called();inputs.assert_not_called();mkdir.assert_not_called()

    def fixture_units(self):
        return e.units('nxrs-es-0123abcd',Path('/run/nxrs-es-0123abcd'),'es0123456789abcdef',r'run-nxrs\x2des\x2d0123abcd-volume.mount')

    def test_actual_generated_group_retains_encrypted_sandbox_and_mount_dependencies(self):
        prefix='nxrs-es-0123abcd';mount=r'run-nxrs\x2des\x2d0123abcd-volume.mount';units=self.fixture_units()
        normalized={name.replace(mount,'owned-volume.mount').replace(prefix,'owned') for name in units}
        self.assertEqual(normalized,e.UNIT_KEYS)
        activity=units[prefix+'-activity.service'];overlay=units[prefix+'-control.service.d/encrypted-storage.conf']
        for text in [activity,overlay]:
            self.assertIn('--require-encrypted-storage',text);self.assertIn('BindsTo=',text);self.assertIn(mount,text)
            self.assertIn('ReadWritePaths=/run/'+prefix+'/volume',text)
        self.assertIn('User='+prefix+'-control',activity);self.assertIn('Group='+prefix+'-control',activity)
        for directive in ['NoNewPrivileges=yes','PrivateDevices=yes','ProtectSystem=strict','KillMode=control-group',
                          '--require-media-isolation','--check','Restart=no']:
            self.assertIn(directive,activity)
        self.assertIn('BindsTo='+prefix+'-activity.service '+mount,units[prefix+'-monitor.target.d/encrypted-storage.conf'])
        self.assertIn('Options=rw,nosuid,nodev,noexec',units[mount]);self.assertIn('What=/dev/mapper/es0123456789abcdef',units[mount])
        self.assertEqual(units[prefix+'-network.service'],'[Service]\nType=oneshot\nExecStart=/usr/bin/true\nRemainAfterExit=yes\n')
        self.assertNotIn('nxrs-firewall.service',units)
        self.assertTrue(all('ExecStart=/usr/sbin/nft' not in text for text in units.values()))

    def test_unit_commands_and_loaded_credentials_bind_to_the_actual_staged_resources(self):
        prefix='nxrs-es-0123abcd';root=Path('/run')/prefix;units=self.fixture_units()
        for role,program in [('control','control'),('media','media'),('proxy','proxy'),('activity','activity')]:
            text=units[prefix+'-'+role+'.service']
            self.assertIn('ExecStart='+str(root/'bin'/program)+' ',text)
            self.assertNotIn('/usr/local/libexec/',text)
            self.assertNotIn('/etc/'+prefix,text)
            self.assertNotIn('/etc/nxrs-monitor',text)
        for role,filename in [('control','config.json'),('media','mediamtx.json'),('proxy','nginx.conf'),('activity','activity.json')]:
            self.assertIn('LoadCredential=config:'+str(root/'conf'/filename),units[prefix+'-'+role+'.service'])
        self.assertIn('LoadCredential=security:'+str(root/'conf/security.json'),units[prefix+'-control.service'])
        self.assertIn('LoadCredential=recorder:'+str(root/'conf/recorder.token'),units[prefix+'-activity.service'])
        self.assertIn(' --policy '+str(root/'proxy-artifact/approval.json'),units[prefix+'-proxy.service'])
        self.assertIn('ExecStartPre='+str(root/'bin/sandbox')+' --check',units[prefix+'-activity.service'])

    def test_unit_renderer_refuses_unowned_names_paths_and_injected_native_arguments(self):
        valid=['nxrs-es-0123abcd',Path('/run/nxrs-es-0123abcd'),'es0123456789abcdef',e.mount_name('nxrs-es-0123abcd')]
        for index,value in [(0,'host-service'),(0,'nxrs-es-0123abcd\nExecStart=other'),(1,Path('/etc')),
                            (2,'existing-mapping'),(3,'owned.mount\nExecStart=other')]:
            args=list(valid);args[index]=value
            with self.assertRaisesRegex(ValueError,'owned-unit-resources'):e.units(*args)

    def test_synthetic_source_uses_only_pipe_credentials_and_joins_both_children(self):
        class Captured(io.BytesIO):
            def close(self):self.captured=self.getvalue();super().close()
        class Child:
            def __init__(self,encoder):
                self.stdin=None if encoder else Captured();self.stdout=io.BytesIO(b'owned MPEG-TS') if encoder else None
                self.status=None;self.terminated=False;self.waited=False
            def poll(self):return self.status
            def terminate(self):self.terminated=True;self.status=0
            def kill(self):self.status=-9
            def wait(self,timeout):self.waited=True;return self.status
        with tempfile.TemporaryDirectory() as directory:
            token='a'*64;path=Path(directory)/'private.json'
            path.write_text(json.dumps(dict(token=token,publisher='/owned/publisher',ffmpeg='/owned/ffmpeg',rtsp='127.0.0.1:8554',username='publisher')))
            publisher=Child(False);encoder=Child(True);calls=[]
            def launch(args,**options):
                calls.append((args,options));return publisher if len(calls)==1 else encoder
            with patch.object(source.subprocess,'Popen',side_effect=launch),patch.object(source.signal,'signal'):
                with self.assertRaisesRegex(ValueError,'synthetic-source-ended'):source.run(path)
            self.assertEqual(publisher.stdin.captured,token.encode()+b'owned MPEG-TS')
            self.assertTrue(all(child.terminated and child.waited for child in (encoder,publisher)))
            for args,options in calls:
                self.assertTrue(all(token not in str(value) for value in args));self.assertNotIn(token,str(options['env']))
                self.assertEqual(options['env'],{'PATH':'/usr/bin:/bin','LANG':'C'})
            self.assertNotIn('rtsp://',str(calls[1][0]))

    def test_native_mount_name_must_match_the_exact_controlled_path(self):
        prefix='nxrs-es-0123abcd'
        self.assertEqual(e.mount_name(prefix),r'run-nxrs\x2des\x2d0123abcd-volume.mount')
        for name in ['run-nxrs-es-0123abcd-volume.mount','run-nxrs\\x2des\\x2d0123abcd-other.mount','other.mount']:
            with self.assertRaises(ValueError):e.units(prefix,Path('/run')/prefix,'es0123456789abcdef',name)
        with self.assertRaises(ValueError):e.mount_name('existing-host-service')

    def test_volume_root_stays_privileged_and_only_private_storage_is_service_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            volume=Path(directory)
            with patch.object(e.os.path,'ismount',return_value=True),patch.object(e.os,'chown') as chown:
                e.provision_storage(volume,501,502)
            self.assertEqual(volume.stat().st_mode&0o777,0o711)
            self.assertEqual([call.args for call in chown.call_args_list],[(volume,0,0),(volume/'activity',501,502),(volume/'maintenance',501,502)])
            self.assertTrue(all((volume/name).stat().st_mode&0o777==0o700 for name in ('activity','maintenance')))
            before={name:(volume/name).stat().st_mode for name in ('activity','maintenance')}
            with patch.object(e.os.path,'ismount',return_value=True),patch.object(e.os,'chown') as chown:
                with self.assertRaisesRegex(ValueError,'new-encrypted-storage-layout'):e.provision_storage(volume,501,502)
                chown.assert_not_called()
            self.assertEqual(before,{name:(volume/name).stat().st_mode for name in before})
        with tempfile.TemporaryDirectory() as directory,patch.object(e.os.path,'ismount',return_value=False),patch.object(e.os,'chown') as chown:
            with self.assertRaisesRegex(ValueError,'owned-mounted-storage'):e.provision_storage(Path(directory),501,502)
            chown.assert_not_called();self.assertEqual(list(Path(directory).iterdir()),[])

    def test_invalid_source_credential_refuses_before_any_child(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.json';path.write_text(json.dumps({'token':'Z'*64}))
            with patch.object(source.subprocess,'Popen') as launch:
                with self.assertRaisesRegex(ValueError,'synthetic-token'):source.run(path)
                launch.assert_not_called()

    def receipt(self):
        return dict(schema=1,scope=e.SCOPE,qualified=True,checkout=dict(commit='a'*40,clean=True),inputs={'fixture':'b'*64},
            executables=dict.fromkeys(e.PROGRAMS,'c'*64),unit_sha256=dict.fromkeys(e.UNIT_KEYS,'d'*64),normalized_unit_sha256=e.expected_unit_policy(),
            proofs=dict.fromkeys(('system_regex','media_runtime','media_isolation','proxy_qualification','proxy_review'),'e'*64),
            controls=dict.fromkeys(e.CONTROLS,True),clip_sha256='f'*64,clip_bytes=500000,decoded_frames=48,**dict.fromkeys(e.FLAGS,False))

    def test_success_receipt_requires_real_controls_and_refuses_scope_or_private_fields(self):
        value=self.receipt()
        with patch.object(e,'source_inputs',return_value={'fixture':'b'*64}):
            e.validate(value)
            for mutate in [lambda row:row.update(private_log='private'),lambda row:row.update(decoded_frames=0),
                lambda row:row['controls'].update(owned_cleanup_joined=False),lambda row:row['controls'].update(actual_generated_units=1),
                lambda row:row['unit_sha256'].update(unrelated_unit='d'*64),lambda row:row['executables'].pop('publisher'),
                lambda row:row['normalized_unit_sha256'].update({'owned-activity.service':'0'*64}),lambda row:row['proofs'].pop('media_isolation')]:
                changed=copy.deepcopy(value);mutate(changed)
                with self.assertRaises(ValueError):e.validate(changed)
            for flag in e.FLAGS:
                changed=copy.deepcopy(value);changed[flag]=True
                with self.subTest(flag=flag),self.assertRaises(ValueError):e.validate(changed)

    def test_encrypted_workflow_pr_trigger_covers_every_executed_source_declaration(self):
        text=(e.ROOT/'.github/workflows/encrypted-activity.yml').read_text()
        block=re.search(r'^    paths:\n(.*?)(?=^  [A-Za-z_]+:|^\S)',text,re.M|re.S)[1]
        patterns=[line.strip()[3:-1] for line in block.splitlines() if line.strip().startswith("- '")]
        missing={name for name in e.source_inputs() if not any(name==pattern or
            pattern.endswith('/**') and name.startswith(pattern[:-2]) for pattern in patterns)}
        self.assertEqual(missing,set())

if __name__=='__main__':unittest.main()
