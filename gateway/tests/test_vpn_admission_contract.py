"""The staging and startup consumers intentionally share this private protocol."""
import json
from pathlib import Path
import tempfile
import unittest
import vpn_admission_contract as contract


class ContractTests(unittest.TestCase):
    def test_common_synthetic_contract_matches_python_pair_admission(self):
        cases=contract.corpus();self.assertEqual(len(cases),32)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();config=root/'wg-nxrs.conf';history=root/'vpn-peers.json'
            contract.vpn_peer.write_private(config,b'initial');contract.vpn_peer.write_private(history,b'initial')
            for case in cases:
                config.write_text(case['config']);history.write_text(case['history'])
                try:contract.vpn_peer.pair(config,history)
                except ValueError:valid=False
                else:valid=True
                with self.subTest(case=case['name']):self.assertEqual(valid,case['valid'])

    def test_generated_network_check_is_read_only_and_precedes_interface_start(self):
        import vpn
        unit=vpn.network_admission()
        self.assertIn('ExecStartPre=/usr/local/libexec/nxrs-vpn-admission ',unit)
        self.assertIn('--config /etc/wireguard/wg-nxrs.conf --history /etc/wireguard/vpn-peers.json',unit)
        self.assertIn('--firewall /etc/nxrs-monitor/firewall.nft',unit)
        self.assertNotIn('ExecStart=',unit);self.assertNotIn('initialize',unit)
        self.assertIn('UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES',unit)


if __name__=='__main__':unittest.main()
