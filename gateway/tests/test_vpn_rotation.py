import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

DEPLOY=Path(__file__).resolve().parents[1]/'deploy'
sys.path.insert(0,str(DEPLOY))
import vpn_peer as peer
import vpn

def key(byte):return base64.b64encode(bytes([byte])*32).decode()


class RotationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.source=self.root/'source';self.source.mkdir(mode=0o700)
        self.config=self.source/peer.CONFIG;self.history=self.source/peer.HISTORY
        self.raw=peer.configuration('10.77.0.1','10.77.0.2',key(9),key(1)).encode()
        self.state=peer.initial_history(self.raw,key(8))
        peer.write_private(self.config,self.raw);peer.write_private(self.history,peer.encoded(self.state))

    def rotate(self,destination=None,old=None,new=None,config=None,history=None):
        return peer.stage(config or self.config,history or self.history,destination or self.root/'rotation',
            old or key(1),new or key(2))

    def test_pair_rotation_preserves_gateway_routes_other_files_and_source_bytes(self):
        application=self.source/'config.json';application.write_bytes(b'owned application credentials remain here')
        before={p.name:p.read_bytes() for p in self.source.iterdir()}
        with patch.object(subprocess,'run') as commands:
            output=self.rotate();self.assertTrue(peer.check(self.config,self.history,output));commands.assert_not_called()
        self.assertEqual({p.name:p.read_bytes() for p in self.source.iterdir()},before)
        self.assertEqual({p.name for p in output.iterdir()},{peer.CONFIG,peer.HISTORY,peer.MARKER})
        self.assertEqual(stat.S_IMODE(output.stat().st_mode),0o700)
        for p in output.iterdir():self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
        selected=peer.parse_configuration((output/peer.CONFIG).read_bytes())
        self.assertEqual(selected,dict(gateway='10.77.0.1',peer='10.77.0.2',secret=key(9),peer_key=key(2)))
        history=peer.parse_history((output/peer.HISTORY).read_bytes())
        self.assertEqual(history['retired_peers'],[key(1)]);self.assertEqual(history['generation'],2)
        self.assertEqual(history['gateway_public_key'],key(8))

    def test_retired_keys_stale_plans_and_expected_key_mismatch_never_reenroll(self):
        one=self.rotate();two=self.rotate(self.root/'second',key(2),key(3),one/peer.CONFIG,one/peer.HISTORY)
        self.assertTrue(peer.check(one/peer.CONFIG,one/peer.HISTORY,two))
        self.assertEqual(peer.parse_history((two/peer.HISTORY).read_bytes())['retired_peers'],[key(1),key(2)])
        for new in [key(1),key(2),key(8)]:
            output=self.root/('refused-'+str(len(new))+new[0])
            with self.subTest(new=new),self.assertRaises(ValueError):
                self.rotate(output,key(2),new,one/peer.CONFIG,one/peer.HISTORY)
            self.assertFalse(output.exists())
        with self.assertRaises(ValueError):peer.check(one/peer.CONFIG,one/peer.HISTORY,one)
        with self.assertRaises(ValueError):self.rotate(self.root/'stale-expected',key(3),key(4))
        self.assertFalse((self.root/'stale-expected').exists())

    def test_missing_or_malformed_history_never_becomes_an_empty_seed(self):
        original=self.history.read_bytes()
        self.history.unlink()
        with self.assertRaises(FileNotFoundError):self.rotate()
        self.assertFalse((self.root/'rotation').exists());self.assertFalse(self.history.exists())
        peer.write_private(self.history,original)
        values=[]
        for field,value in [('version',True),('version',2),('generation',True),('generation',2),
                            ('retired_peers',[key(2),key(2)]),('active_peer',key(8)),('config_sha256','0'*64)]:
            changed=copy.deepcopy(self.state);changed[field]=value;values.append(peer.encoded(changed))
        values.extend([b'{',b'{"version":1,"version":1}',peer.encoded({**self.state,'unknown':True})])
        for raw in values:
            self.history.write_bytes(raw)
            with self.subTest(raw=raw[:20]),self.assertRaises((ValueError,KeyError)):self.rotate()
            self.assertFalse((self.root/'rotation').exists())
        self.assertEqual(self.config.read_bytes(),self.raw)

    def test_private_regular_inputs_and_new_destinations_are_required(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):self.rotate()
        self.config.chmod(0o600)
        alias=self.source/'alias';alias.symlink_to(self.config)
        with self.assertRaises(OSError):self.rotate(config=alias)
        alias.unlink();os.link(self.config,alias)
        with self.assertRaises(ValueError):self.rotate()
        alias.unlink();fifo=self.source/'fifo';os.mkfifo(fifo,0o600)
        with self.assertRaises(ValueError):self.rotate(config=fifo)
        out=self.root/'exists';out.mkdir()
        with self.assertRaises(FileExistsError):self.rotate(destination=out)
        link=self.root/'link';link.symlink_to(out,target_is_directory=True)
        with self.assertRaises(FileExistsError):self.rotate(destination=link)
        self.assertEqual(list(out.iterdir()),[])

    def test_unknown_hooks_additional_peers_and_profile_drift_refuse_before_output(self):
        for raw in [self.raw+b'PostUp = unexpected\n',self.raw+b'[Peer]\nPublicKey = '+key(3).encode()+b'\n',
                    self.raw.replace(b'MTU = 1380',b'MTU = 1500'),self.raw.replace(b'/32',b'/24'),
                    self.raw+b'PrivateKey = '+key(4).encode()+b'\n']:
            self.config.write_bytes(raw)
            with self.subTest(raw=raw[-30:]),self.assertRaises(ValueError):self.rotate()
            self.assertFalse((self.root/'rotation').exists())

    def test_full_history_refuses_without_dropping_tombstones_or_writing_output(self):
        retired=[base64.b64encode(n.to_bytes(32,'little')).decode() for n in range(1,peer.MAX_RETIRED+1)]
        self.state.update(retired_peers=retired,generation=peer.MAX_RETIRED+1)
        raw=peer.encoded(self.state);self.history.write_bytes(raw)
        with self.assertRaisesRegex(ValueError,'capacity'):self.rotate()
        self.assertFalse((self.root/'rotation').exists());self.assertEqual(self.history.read_bytes(),raw)

    def test_source_change_and_write_failure_leave_no_completion_marker(self):
        original=peer.write_private
        def change(path,raw):
            original(path,raw)
            if path.name==peer.HISTORY:self.config.write_bytes(self.raw+b'changed\n')
        with patch.object(peer,'write_private',side_effect=change),self.assertRaises(ValueError):self.rotate()
        self.assertFalse((self.root/'rotation'/peer.MARKER).exists())
        self.config.write_bytes(self.raw)
        def fail(path,raw):
            original(path,raw)
            if path.name==peer.MARKER:raise OSError('owned completion sync failure')
        with patch.object(peer,'write_private',side_effect=fail),self.assertRaises(OSError):
            self.rotate(destination=self.root/'failed')
        self.assertFalse((self.root/'failed'/peer.MARKER).exists())

    def test_coherently_rehashed_gateway_identity_change_cannot_pass_review(self):
        output=self.rotate();selected=peer.parse_configuration((output/peer.CONFIG).read_bytes());selected['secret']=key(7)
        config=peer.configuration(**selected).encode();(output/peer.CONFIG).write_bytes(config)
        history=json.loads((output/peer.HISTORY).read_bytes());history['config_sha256']=peer.digest(config)
        history_raw=peer.encoded(history);(output/peer.HISTORY).write_bytes(history_raw)
        marker=json.loads((output/peer.MARKER).read_bytes())
        marker.update(staged_config_sha256=peer.digest(config),staged_history_sha256=peer.digest(history_raw))
        (output/peer.MARKER).write_bytes(peer.encoded(marker))
        with self.assertRaisesRegex(ValueError,'only a fresh peer'):peer.check(self.config,self.history,output)

    def test_generator_seeds_history_only_for_new_valid_independent_enrollment(self):
        for private_key,gateway_key in [('invalid',key(8)),(key(9),key(1))]:
            output=self.root/'invalid-generation'
            results=[subprocess.CompletedProcess([],0,stdout=(private_key+'\n').encode()),
                     subprocess.CompletedProcess([],0,stdout=(gateway_key+'\n').encode())]
            with patch.object(vpn.subprocess,'run',side_effect=results),self.assertRaises(ValueError):
                vpn.generate(output,'camera.test','10.77.0.1','10.77.0.2','endpoint.test',key(1))
            self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()
