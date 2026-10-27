import json
import unittest

from proxy_diagnostics import file_refusals


class ProxyFailureObservationsTests(unittest.TestCase):
    def test_only_complete_fixed_role_check_pairs_cross_the_report_boundary(self):
        raw = (b'PRIVATE path or token\n'
            b'nxrs-gateway-proxy: file-refusal (approval: file-open)\n'
            b'nxrs-gateway-proxy: file-refusal (ssl: file-links)\n'
            b'nxrs-gateway-proxy: file-refusal (PRIVATE: file-open)\n'
            b'nxrs-gateway-proxy: file-refusal (approval: PRIVATE)\n'
            b'nxrs-gateway-proxy: file-refusal (binary: file-type) PRIVATE\n'
            b'nxrs-gateway-proxy: file-refusal (approval: file-open)\n')
        result = file_refusals(raw)
        self.assertEqual(result, [dict(role='approval', check='file-open'), dict(role='ssl', check='file-links')])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(file_refusals(b'x'*65536+b'\n'+raw), [])

    def test_fixed_observations_remain_bounded_with_repeated_and_distinct_failures(self):
        from proxy_diagnostics import ROLES, CHECKS
        raw=''.join(f'nxrs-gateway-proxy: file-refusal ({role}: {check})\n' for role in sorted(ROLES) for check in sorted(CHECKS)).encode()
        result=file_refusals(raw*100)
        self.assertEqual(len(result),8)
        self.assertLess(len(json.dumps(result)),1024)


if __name__ == '__main__': unittest.main()
