import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import vpn


class ProxyRuntimePathsTests(unittest.TestCase):
    def test_all_compiled_module_temp_paths_stay_in_the_service_runtime(self):
        runtime = '/run/nxrs-proxy-fixture'
        text = vpn.nginx_config('camera.test', '127.0.0.1', runtime=runtime)
        for directive, suffix in [('client_body_temp_path', 'body'), ('proxy_temp_path', 'proxy'),
                                  ('fastcgi_temp_path', 'fastcgi'), ('uwsgi_temp_path', 'uwsgi'), ('scgi_temp_path', 'scgi')]:
            self.assertIn(f'{directive} {runtime}/{suffix};', text)
        self.assertNotIn('/var/lib/nginx', text)
        self.assertNotIn('fastcgi_pass', text)
        self.assertNotIn('uwsgi_pass', text)
        self.assertNotIn('scgi_pass', text)


if __name__ == '__main__':
    unittest.main()
