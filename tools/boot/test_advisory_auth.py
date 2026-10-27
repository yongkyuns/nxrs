"""Synthetic CI token transport/origin/descriptor controls; no real credentials."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import qualify as process
import upstream_advisories as upstream

TOKEN = b"synthetic_ci_token_" + b"x" * 64
URL = "https://api.github.com/repos/mcu-tools/mcuboot/security-advisories?per_page=100"


class AuthTests(unittest.TestCase):
    def test_bounded_pipe_round_trip_is_not_inheritable_and_closes_reader(self):
        with upstream.token_pipe(TOKEN) as reader:
            self.assertFalse(os.get_inheritable(reader))
            duplicate = os.dup(reader)
            self.assertEqual(upstream.read_token_fd(duplicate), TOKEN)
            with self.assertRaises(OSError):
                os.fstat(duplicate)
        with upstream.token_pipe(b"x" * 4096) as reader:
            self.assertEqual(upstream.read_token_fd(os.dup(reader)), b"x" * 4096)
        with upstream.token_pipe(None) as reader:
            self.assertIsNone(reader)

    def test_nonpipe_write_endpoint_stdio_bad_tokens_and_oversize_are_refused(self):
        with tempfile.TemporaryFile() as file:
            with self.assertRaises(ValueError):
                upstream.read_token_fd(os.dup(file.fileno()))
        reader, writer = os.pipe()
        try:
            with self.assertRaises(ValueError):
                upstream.read_token_fd(os.dup(writer))
        finally:
            os.close(reader)
            os.close(writer)
        for fd in (0, 1, 2, -1):
            with self.assertRaises(ValueError):
                upstream.read_token_fd(fd)
        for value in (b"short", TOKEN+b"\n", TOKEN+b"\r\nInjected: value", b"x"*4097, "not-bytes"):
            with self.assertRaises(ValueError):
                upstream.validated_token(value)

    def test_pipe_requires_eof_and_a_bounded_deadline(self):
        reader, writer = os.pipe()
        os.write(writer, TOKEN)
        start = time.monotonic()
        try:
            with self.assertRaises(ValueError):
                upstream.read_token_fd(reader)
            self.assertLess(time.monotonic()-start, 5)
        finally:
            os.close(writer)

    def test_sdk_children_do_not_inherit_the_token_pipe_but_collector_can(self):
        with tempfile.TemporaryDirectory(prefix="nxrs-advisory-fd-") as temporary:
            folder = Path(temporary)
            environment = {key:os.environ[key] for key in ("PATH","HOME") if key in os.environ}
            with upstream.token_pipe(TOKEN) as reader:
                identity = os.fstat(reader)
                script = ("import os,sys\ntry:\n s=os.fstat(int(sys.argv[1])); inherited=(s.st_ino==int(sys.argv[2]) and s.st_dev==int(sys.argv[3]))\n"
                          "except OSError:\n inherited=False\nprint('inherited' if inherited else 'closed')\n")
                status, output = process.run([sys.executable,"-c",script,str(reader),str(identity.st_ino),str(identity.st_dev)],
                    folder,environment,folder,"sdk-child",10)
                self.assertEqual((status,output),(0,"closed\n"))
                script = ("import sys\nsys.path.insert(0,sys.argv[1])\nimport upstream_advisories as u\n"
                          "u.read_token_fd(int(sys.argv[2])); print('collector-read')\n")
                status, output = process.run([sys.executable,"-c",script,str(Path(upstream.__file__).parent),str(reader)],
                    folder,environment,folder,"collector-child",10,pass_fds=(reader,))
                self.assertEqual((status,output),(0,"collector-read\n"))

    def test_auth_is_attached_only_to_exact_official_github_endpoints(self):
        class Response:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def geturl(self): return URL
            def read(self,maximum): return b"[]"
        class Opener:
            def open(self,request,timeout):
                self.request = request
                return Response()
        opener = Opener()
        with patch.object(upstream.urllib.request,"build_opener",return_value=opener):
            self.assertEqual(upstream.fetch(URL,time.monotonic()+10,100,TOKEN),b"[]")
        self.assertEqual(opener.request.get_header("Authorization"),"Bearer "+TOKEN.decode())
        for url in (upstream.MBED,"https://untrusted.invalid/","https://api.github.com/repos/mcu-tools/mcuboot/security-advisories?extra=1"):
            with self.assertRaises(ValueError):
                upstream.fetch(url,time.monotonic()+10,100,TOKEN)

    def test_authenticated_opener_replaces_default_redirects_and_never_follows_them(self):
        opener = upstream.urllib.request.build_opener(upstream.NoAuthRedirect())
        handlers = [handler for handler in opener.handlers if isinstance(handler,upstream.urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(handlers),1)
        self.assertIsInstance(handlers[0],upstream.NoAuthRedirect)
        for url in ("https://untrusted.invalid/",URL):
            self.assertIsNone(handlers[0].redirect_request(None,None,302,"private data",{},url))


if __name__ == "__main__":
    unittest.main()
