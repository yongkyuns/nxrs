"""Keep the media-only Netlink discovery permission separate from privileges."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import strict_vpn
import vpn


class MediaAddressFamiliesTests(unittest.TestCase):
    def test_only_media_can_query_linux_network_interfaces(self):
        for render in [vpn.services, strict_vpn.services]:
            units = render()
            for name in ['nxrs-control', 'nxrs-media', 'nxrs-proxy']:
                with self.subTest(profile=render.__module__, service=name):
                    lines = units[name + '.service'].splitlines()
                    declarations = [s.split('=', 1)[1] for s in lines if s.startswith('RestrictAddressFamilies=')]
                    self.assertEqual(len(declarations), 1)
                    expected = {'AF_INET', 'AF_UNIX'}
                    if name == 'nxrs-media':
                        expected.add('AF_NETLINK')
                    self.assertEqual(set(declarations[0].split()), expected)
                    self.assertIn('CapabilityBoundingSet=', lines)
                    self.assertNotIn('CAP_NET_ADMIN', units[name + '.service'])
                    self.assertNotIn('CAP_NET_RAW', units[name + '.service'])
                    self.assertIn('NoNewPrivileges=yes', lines)

    def test_discovery_permission_does_not_advertise_all_interfaces(self):
        for render in [vpn.media_config, strict_vpn.media_config]:
            config = render('10.77.0.1')
            self.assertFalse(config['webrtcIPsFromInterfaces'])
            self.assertEqual(config['webrtcAdditionalHosts'], ['10.77.0.1'])
            self.assertEqual(config['webrtcLocalUDPAddress'], '10.77.0.1:8189')
            self.assertEqual(config['webrtcAddress'], '127.0.0.1:8889')
            self.assertEqual(config['authHTTPExclude'], [])


if __name__ == '__main__':
    unittest.main()
