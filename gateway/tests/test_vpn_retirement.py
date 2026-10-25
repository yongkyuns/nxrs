"""Lost-device retirement and explicit recovery; private file fixtures only."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_vpn_rotation as rotation

peer=rotation.peer
key=rotation.key


class RetirementTests(unittest.TestCase):
    setUp=rotation.RotationTests.setUp

    def retire(self,destination=None):
        return peer.stage_retirement(self.config,self.history,destination or self.root/'retired',key(1))

    def recover(self,source,destination=None,expected=2,new=None):
        return peer.stage_recovery(source/peer.CONFIG,source/peer.HISTORY,destination or self.root/'recovered',expected,new or key(2))

    def test_retirement_needs_no_replacement_and_preserves_gateway_and_source(self):
        before={p.name:p.read_bytes() for p in self.source.iterdir()}
        with patch.object(subprocess,'run') as network:
            output=self.retire();self.assertTrue(peer.check(self.config,self.history,output));network.assert_not_called()
        self.assertEqual({p.name:p.read_bytes() for p in self.source.iterdir()},before)
        config=(output/peer.CONFIG).read_bytes()
        expected=(f'[Interface]\nAddress = 10.77.0.1/32\nListenPort = 51820\nMTU = 1380\n'
            f'PreUp = /usr/sbin/nft -f /etc/nxrs-monitor/firewall.nft\nPrivateKey = {key(9)}\n').encode()
        self.assertEqual(config,expected);self.assertNotIn(b'[Peer]',config)
        self.assertEqual(json.loads((output/peer.HISTORY).read_bytes()),dict(version=2,generation=2,
            gateway_public_key=key(8),active_peer=None,retired_peers=[key(1)],peer_address='10.77.0.2',
            config_sha256=hashlib.sha256(expected).hexdigest()))
        marker=json.loads((output/peer.MARKER).read_bytes())
        self.assertEqual(marker['operation'],'retire');self.assertFalse(marker['deployment_permitted'])
        self.assertFalse(marker['physical_qualified'])

    def test_recovery_and_later_transitions_keep_every_retired_key_and_increase_generation(self):
        retired=self.retire();recovered=self.recover(retired)
        self.assertTrue(peer.check(retired/peer.CONFIG,retired/peer.HISTORY,recovered))
        _,_,selected,state=peer.pair(recovered/peer.CONFIG,recovered/peer.HISTORY)
        self.assertEqual(selected,dict(gateway='10.77.0.1',peer='10.77.0.2',secret=key(9),peer_key=key(2)))
        self.assertEqual((state['version'],state['generation'],state['retired_peers']),(2,3,[key(1)]))
        rotated=peer.stage(recovered/peer.CONFIG,recovered/peer.HISTORY,self.root/'rotated',key(2),key(3))
        self.assertTrue(peer.check(recovered/peer.CONFIG,recovered/peer.HISTORY,rotated))
        again=peer.stage_retirement(rotated/peer.CONFIG,rotated/peer.HISTORY,self.root/'retired-again',key(3))
        final=self.recover(again,self.root/'recovered-again',5,key(4))
        final_state=json.loads((final/peer.HISTORY).read_bytes())
        self.assertEqual(final_state['generation'],6);self.assertEqual(final_state['retired_peers'],[key(1),key(2),key(3)])
        for old in [key(1),key(2),key(3)]:
            with self.assertRaises(ValueError):peer.stage(final/peer.CONFIG,final/peer.HISTORY,self.root/'forbidden',key(4),old)
            self.assertFalse((self.root/'forbidden').exists())

    def test_recovery_requires_retired_state_fresh_key_and_exact_integer_generation(self):
        with self.assertRaises(ValueError):self.recover(self.source)
        self.assertFalse((self.root/'recovered').exists())
        retired=self.retire()
        for generation in [True,False,0,1,3,-1,2**64]:
            with self.subTest(generation=generation),self.assertRaises(ValueError):self.recover(retired,expected=generation)
            self.assertFalse((self.root/'recovered').exists())
        for reused in [key(1),key(8)]:
            with self.assertRaises(ValueError):self.recover(retired,new=reused)
            self.assertFalse((self.root/'recovered').exists())
        with self.assertRaises(ValueError):peer.stage_retirement(self.config,self.history,self.root/'wrong',key(2))
        self.assertFalse((self.root/'wrong').exists())

    def test_missing_or_corrupt_history_never_restores_an_active_peer(self):
        retired=self.retire();history=retired/peer.HISTORY;original=history.read_bytes();history.unlink()
        with self.assertRaises(FileNotFoundError):self.recover(retired)
        self.assertFalse(history.exists());self.assertFalse((self.root/'recovered').exists())
        peer.write_private(history,original)
        for bad in [b'{',b'{"version":2,"version":2}',b'{}']:
            history.write_bytes(bad)
            with self.assertRaises(ValueError):self.recover(retired)
            self.assertFalse((self.root/'recovered').exists())
        self.assertNotIn(b'[Peer]',(retired/peer.CONFIG).read_bytes())

    def test_retired_pairs_cannot_downgrade_or_change_the_retained_route(self):
        retired=self.retire();path=retired/peer.HISTORY;original=json.loads(path.read_bytes())
        cases=[]
        for field,value in [('version',1),('version',True),('generation',True),('generation',1),('generation',3),
                            ('peer_address','10.77.0.1'),('peer_address','8.8.8.8'),('retired_peers',[])]:
            modified=copy.deepcopy(original);modified[field]=value;cases.append(modified)
        cases.append({**original,'unknown':True})
        for value in cases:
            path.write_bytes(peer.encoded(value))
            with self.subTest(value=value),self.assertRaises(ValueError):self.recover(retired)
            self.assertFalse((self.root/'recovered').exists())
        path.write_bytes(peer.encoded(original))
        # Even a coherently rehashed original active config cannot match inactive state.
        (retired/peer.CONFIG).write_bytes(self.raw);original['config_sha256']=peer.digest(self.raw)
        path.write_bytes(peer.encoded(original))
        with self.assertRaisesRegex(ValueError,'matching VPN'):self.recover(retired)

    def test_full_legacy_history_has_one_emergency_retirement_slot_but_no_new_admission(self):
        retired_keys=[base64.b64encode(n.to_bytes(32,'little')).decode() for n in range(1,peer.MAX_RETIRED+1)]
        self.state.update(retired_peers=retired_keys,generation=peer.MAX_RETIRED+1)
        before=peer.encoded(self.state);self.history.write_bytes(before)
        output=self.retire();self.assertTrue(peer.check(self.config,self.history,output))
        final=json.loads((output/peer.HISTORY).read_bytes())
        self.assertEqual(final['retired_peers'],retired_keys+[key(1)]);self.assertIsNone(final['active_peer'])
        self.assertEqual(final['generation'],peer.MAX_RETIRED+2);self.assertEqual(self.history.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'capacity'):self.recover(output,expected=peer.MAX_RETIRED+2)
        self.assertFalse((self.root/'recovered').exists())
        final.update(active_peer=key(2))
        with self.assertRaisesRegex(ValueError,'bounded'):peer.parse_history(peer.encoded(final))

    def test_stale_recovery_plan_and_coherently_rehashed_history_erasure_refuse(self):
        retired=self.retire();recovered=self.recover(retired)
        with self.assertRaises(ValueError):peer.check(recovered/peer.CONFIG,recovered/peer.HISTORY,recovered)
        history=json.loads((recovered/peer.HISTORY).read_bytes());history.update(retired_peers=[],generation=1)
        raw=peer.encoded(history);(recovered/peer.HISTORY).write_bytes(raw)
        marker=json.loads((recovered/peer.MARKER).read_bytes());marker['staged_history_sha256']=peer.digest(raw)
        (recovered/peer.MARKER).write_bytes(peer.encoded(marker))
        with self.assertRaisesRegex(ValueError,'only a fresh peer'):peer.check(retired/peer.CONFIG,retired/peer.HISTORY,recovered)

    def test_failed_retirement_does_not_publish_a_completion_marker_or_change_source(self):
        write=peer.write_private
        def fail(path,raw):
            write(path,raw)
            if path.name==peer.MARKER:raise OSError('owned simulated completion failure')
        before=self.history.read_bytes()
        with patch.object(peer,'write_private',side_effect=fail),self.assertRaises(OSError):self.retire()
        self.assertFalse((self.root/'retired'/peer.MARKER).exists());self.assertEqual(self.config.read_bytes(),self.raw)
        self.assertEqual(self.history.read_bytes(),before)

    def test_cli_operates_only_on_new_private_files_and_withholds_private_values(self):
        output=self.root/'cli-retired';script=rotation.DEPLOY/'vpn_peer.py'
        args=[sys.executable,'-B',str(script),'stage-retirement','--config',str(self.config),'--history',str(self.history),
            '--destination',str(output),'--expected-peer',key(1)]
        result=subprocess.run(args,capture_output=True,timeout=5);self.assertEqual(result.returncode,0)
        self.assertTrue(peer.check(self.config,self.history,output));self.assertEqual(self.config.read_bytes(),self.raw)
        failed=subprocess.run([sys.executable,'-B',str(script),'stage-recovery','--config',str(output/peer.CONFIG),
            '--history',str(output/peer.HISTORY),'--destination',str(self.root/'bad-cli'),'--expected-generation','1',
            '--replacement-peer',key(2)],capture_output=True,timeout=5)
        self.assertEqual(failed.returncode,1);self.assertFalse((self.root/'bad-cli').exists())
        for raw in [result.stdout,result.stderr,failed.stdout,failed.stderr]:self.assertNotIn(key(9).encode(),raw)
        self.assertEqual(failed.stdout,b'');self.assertNotIn(b'Traceback',failed.stderr)


if __name__=='__main__':unittest.main()
