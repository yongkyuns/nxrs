"""Generated-policy tests and real nginx/TLS boundary tests. No host trust or network changes."""
import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'deploy/vpn.py'
spec = importlib.util.spec_from_file_location('vpn', PATH)
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)
KEY = base64.b64encode(bytes(range(32))).decode()


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class PolicyTests(unittest.TestCase):
    def test_bad_inputs_never_write_or_invoke_wg(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)/'output'
            valid = dict(host='monitor.example.test', gateway='10.77.0.1', peer='10.77.0.2', endpoint='home.example.test', peer_key=KEY)
            for field, value in [('host','host; include /tmp/evil'), ('host','a\nserver'), ('host','-host'),
                ('host','a..b'), ('gateway','0.0.0.0'), ('gateway','127.0.0.1'), ('gateway','::1'),
                ('peer','10.77.0.1'), ('peer','8.8.8.8'), ('endpoint','host:51820'), ('endpoint','https://host'),
                ('peer_key','invalid'), ('peer_key',base64.b64encode(bytes(32)).decode())]:
                args = dict(valid); args[field] = value
                with self.subTest(field=field, value=value), patch.object(vpn.subprocess, 'run') as run:
                    with self.assertRaises(ValueError): vpn.generate(root, **args)
                    run.assert_not_called()
                    self.assertFalse(root.exists())

    def test_private_output_and_separate_keys(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)/'output'
            private_key=base64.b64encode(b'p'*32).decode()
            gateway_key=base64.b64encode(b'g'*32).decode()
            responses=[SimpleNamespace(stdout=(private_key+'\n').encode()),SimpleNamespace(stdout=(gateway_key+'\n').encode())]
            with patch.object(vpn.subprocess, 'run', side_effect=responses):
                vpn.generate(root, '10.77.0.1','10.77.0.1','10.77.0.2','home.example.test',KEY)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for file in root.iterdir(): self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertIn('AllowedIPs = 10.77.0.2/32', (root/'wg-nxrs.conf').read_text())
            phone = (root/'phone-settings.txt').read_text()
            self.assertIn('Allowed IPs: 10.77.0.1/32', phone)
            self.assertNotIn('PrivateKey =', phone)
            config = json.loads((root/'config.json').read_text())
            self.assertEqual(config['cameras'][0]['source'], 'synthetic')
            self.assertEqual(config['cameras'][0]['whep_endpoint'], 'https://10.77.0.1:8443/reference/whep')
            self.assertNotEqual((root/'viewer.token').read_text(), (root/'publisher.token').read_text())
            with patch.object(vpn.subprocess, 'run') as run:
                with self.assertRaises(FileExistsError): vpn.generate(root,'host','10.77.0.1','10.77.0.2','endpoint',KEY)
                run.assert_not_called()

    def test_media_is_private_and_never_advertises_lan_candidates(self):
        cfg = vpn.media_config('10.77.0.1')
        self.assertEqual(cfg['authHTTPExclude'], [])
        self.assertEqual(cfg['webrtcAdditionalHosts'], ['10.77.0.1'])
        self.assertFalse(cfg['webrtcIPsFromInterfaces'])
        self.assertEqual(cfg['webrtcICEServers2'], [])
        self.assertEqual(cfg['webrtcAddress'], '127.0.0.1:8889')
        self.assertEqual(cfg['rtspAddress'], '127.0.0.1:8554')
        self.assertEqual(cfg['paths']['reference']['maxReaders'], 4)
        for name in ['api','metrics','pprof','playback','rtmp','hls','srt','moq']:
            self.assertFalse(cfg[name])

    def test_firewall_never_flushes_unrelated_rules_or_forwards_lan(self):
        rules = vpn.firewall('10.77.0.1','10.77.0.2')
        self.assertNotIn('flush ruleset', rules)
        self.assertIn('ip daddr 10.77.0.1 iifname != "wg-nxrs" drop', rules)
        self.assertIn('iifname "wg-nxrs" drop', rules)
        self.assertIn('oifname "wg-nxrs" drop', rules)
        self.assertNotIn('masquerade', rules)

    def test_services_are_unprivileged_and_fail_closed_without_dependencies(self):
        units = vpn.services()
        for name in ['nxrs-control.service','nxrs-media.service','nxrs-proxy.service']:
            self.assertIn('DynamicUser=yes',units[name])
            self.assertIn('NoNewPrivileges=yes',units[name])
            self.assertIn('Requires=wg-quick@wg-nxrs.service nxrs-firewall.service',units[name])
            self.assertIn('BindsTo=wg-quick@wg-nxrs.service',units[name])
            self.assertIn('LoadCredential=',units[name])
        self.assertNotIn('\nExecStop=', units['nxrs-firewall.service'])


@unittest.skipUnless(shutil.which('nginx') and shutil.which('openssl'), 'requires nginx and openssl')
class HTTPSBoundaryTests(unittest.TestCase):
    nginx_binary = 'nginx'
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='nxrs-proxy-')
        root = cls.root = Path(cls.directory.name)
        cls.received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length',0)))
                cls.received.append((self.path, dict(self.headers), body))
                self.send_response(201 if self.command == 'POST' else 200)
                self.send_header('Content-Type','application/sdp' if 'whep' in self.path else 'application/json')
                if self.path == '/reference/whep': self.send_header('Location','/reference/whep/session_1')
                self.end_headers()
                self.wfile.write(b'v=0\r\n' if 'whep' in self.path else b'{}')
            do_GET = do_POST = do_DELETE = handle_request
        cls.backend = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        cls.thread = threading.Thread(target=cls.backend.serve_forever, daemon=True); cls.thread.start()
        cls.ports = dict(vpn.PORTS, https=port(), api=cls.backend.server_port, media=cls.backend.server_port)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=fixture','-addext','subjectAltName=IP:127.0.0.1',
            '-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
        cls.tls = ssl.create_default_context(cafile=str(root/'cert.pem'))
        config = vpn.nginx_config('127.0.0.1','127.0.0.1', cls.ports,root/'cert.pem',root/'key.pem',root)
        (root/'nginx.conf').write_text(config)
        subprocess.run([cls.nginx_binary,'-t','-c',str(root/'nginx.conf')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        cls.process = subprocess.Popen([cls.nginx_binary,'-c',str(root/'nginx.conf')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                with socket.create_connection(('127.0.0.1',cls.ports['https']),timeout=.1): break
            except OSError: time.sleep(.05)
        else: raise AssertionError('proxy failed to start')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try: cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired: cls.process.kill(); cls.process.wait()
        cls.backend.shutdown(); cls.backend.server_close(); cls.thread.join(timeout=5)
        cls.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        time.sleep(.21)  # Stay below the actual configured admission rate outside its dedicated test.
        conn = http.client.HTTPSConnection('127.0.0.1', self.ports['https'], context=self.tls, timeout=5)
        try:
            conn.request(method,path,body=body,headers=headers or {})
            response = conn.getresponse(); data=response.read()
            self.assertEqual(response.getheader('Cache-Control'),'no-store')
            return response.status, response.getheader('Location'), data
        finally: conn.close()

    def test_actual_tls_rejects_untrusted_ca_and_wrong_hostname(self):
        for host, context in [('127.0.0.1',ssl.create_default_context()), ('localhost',self.tls)]:
            conn=http.client.HTTPSConnection(host,self.ports['https'],context=context,timeout=5)
            try:
                with self.assertRaises(ssl.SSLCertVerificationError): conn.request('GET','/healthz')
            finally: conn.close()

    def test_only_intended_headers_and_relative_session_location_are_preserved(self):
        status,location,_=self.request('POST','/reference/whep',b'v=0\r\n',
            {'Authorization':'Bearer TEST_ONLY','Content-Type':'application/sdp','Cookie':'secret','X-Forwarded-For':'10.77.0.3','X-Admin':'true'})
        self.assertEqual(status,201); self.assertEqual(location,'/reference/whep/session_1')
        path,headers,body=self.received[-1]
        self.assertEqual(headers['Authorization'],'Bearer TEST_ONLY'); self.assertEqual(body,b'v=0\r\n')
        self.assertNotIn('Cookie',headers); self.assertNotIn('X-Admin',headers); self.assertNotIn('X-Forwarded-For',headers)
        self.assertEqual(self.request('DELETE',location)[0],200)
        self.assertEqual(self.request('GET','/v1/cameras')[0],200)
        self.assertEqual(self.request('POST','/v1/cameras/cam_reference/playback-grants',b'{}')[0],201)

    def test_private_publish_unknown_and_noncanonical_routes_never_reach_upstream(self):
        before=len(self.received)
        for path in ['/internal/mediamtx/auth','/internal/viewers/viewer_reference/revoke','/metrics','/v3/config/global/get',
                     '/reference/whip','/reference/','/other/whep','/v1/%63ameras','/v1/cameras?token=x','//v1/cameras','/v1/../healthz']:
            with self.subTest(path=path): self.assertIn(self.request('POST',path)[0],[400,404])
        self.assertEqual(len(self.received),before)

    def test_wrong_methods_host_and_oversize_are_rejected(self):
        before=len(self.received)
        self.assertEqual(self.request('GET','/reference/whep')[0],405)
        self.assertEqual(self.request('POST','/v1/cameras')[0],405)
        self.assertEqual(self.request('PATCH','/reference/whep/session_1')[0],405)
        self.assertEqual(self.request('GET','/healthz',headers={'Host':'evil.test'})[0],421)
        self.assertEqual(self.request('POST','/reference/whep',b'x'*262145)[0],413)
        self.assertEqual(len(self.received),before)


if __name__=='__main__': unittest.main()
