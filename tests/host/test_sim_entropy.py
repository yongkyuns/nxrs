"""Actual simulator driver/host shim with injected syscall outcomes; not hardware proof."""
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT/'upstream/nuttx/patches/0092-sim-host-random.patch'
DEVICE = 'arch/sim/src/sim/sim_random.c'
HOST = 'arch/sim/src/sim/posix/sim_hostrandom.c'

HEADERS = {
    'nuttx/config.h': '#pragma once\n#define CONFIG_SIM_RANDOM 1\n#define CONFIG_DEV_RANDOM 1\n#define CONFIG_DEV_URANDOM_ARCH 1\n',
    'nuttx/fs/fs.h': '''#pragma once
#include <sys/types.h>
#include <stddef.h>
struct file { int unused; };
struct file_operations {
  ssize_t (*read)(struct file *, char *, size_t);
  void *write;
};
int register_driver(const char *, const struct file_operations *, unsigned, void *);
''',
    'sim_internal.h': '''#pragma once
#include "nuttx/config.h"
#include <stdint.h>
#include <sys/types.h>
uint64_t up_irq_save(void);
void up_irq_restore(uint64_t);
int host_errno_convert(int);
ssize_t host_random(void *, size_t);
''',
    'sys/random.h': '#include <sys/types.h>\n#define GRND_NONBLOCK 37\nssize_t getrandom(void *, size_t, unsigned);\n',
}

HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <string.h>
#define getrandom test_getrandom
#include "arch/sim/src/sim/posix/sim_hostrandom.c"
#undef getrandom
#include "arch/sim/src/sim/sim_random.c"
static const struct file_operations *device;
static unsigned registrations, calls, restores;
static int masked, failure;
static ssize_t answer;
uint64_t up_irq_save(void) { assert(!masked); masked = 1; return 123; }
void up_irq_restore(uint64_t flags)
{ assert(masked && flags == 123); masked = 0; restores++; errno = ERANGE; }
int host_errno_convert(int code) { assert(!masked); return code; }
ssize_t test_getrandom(void *bytes, size_t size, unsigned flags)
{
  assert(masked && flags == GRND_NONBLOCK && size <= 256);
  calls++;
  if (answer < 0) { errno = failure; return -1; }
  assert((size_t)answer <= size);
  memset(bytes, 0x33, (size_t)answer);
  return answer;
}
int register_driver(const char *path, const struct file_operations *ops,
                    unsigned mode, void *context)
{
  assert(mode == 0444 && context == NULL && ops->read && !ops->write);
  assert(strcmp(path, registrations++ ? "/dev/urandom" : "/dev/random") == 0);
  if (device) assert(device == ops);
  device = ops;
  return 0;
}
int main(void)
{
  unsigned char bytes[512];
  devrandom_register(); devurandom_register();
  assert(registrations == 2);
  unsigned sizes[] = { 0, 1, 255, 256, 257, sizeof(bytes) };
  for (unsigned index = 0; index < sizeof(sizes) / sizeof(sizes[0]); index++)
    {
      unsigned size = sizes[index];
      memset(bytes, 0x5a, sizeof(bytes));
      answer = size > 256 ? 256 : size;
      assert(device->read(NULL, (char *)bytes, size) == answer);
      for (unsigned i = 0; i < sizeof(bytes); i++)
        assert(bytes[i] == (i < (unsigned)answer ? 0x33 : 0x5a));
    }
  answer = 7;
  assert(device->read(NULL, (char *)bytes, sizeof(bytes)) == 7);
  int errors[] = { EAGAIN, EINTR, ENOSYS, EIO };
  for (unsigned i = 0; i < sizeof(errors) / sizeof(errors[0]); i++)
    {
      answer = -1; failure = errors[i];
      unsigned before = calls;
      assert(device->read(NULL, (char *)bytes, sizeof(bytes)) == -errors[i]);
      assert(calls == before + 1); /* No retries, seed or fallback. */
    }
  assert(!masked && calls == restores);
  return 0;
}
'''


class SimulatorEntropyTests(unittest.TestCase):
    def test_device_binding_and_bounded_host_errors_preserve_errno(self):
        compiler = shutil.which('cc') or shutil.which('clang')
        if compiler is None:
            self.skipTest('host C compiler required')
        series = runpy.run_path(str(ROOT/'tools/apply-nuttx-patches.py'))['SERIES']['nuttx']
        self.assertIn(PATCH.name, series)
        with tempfile.TemporaryDirectory(prefix='nxrs-sim-entropy-') as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'apply', '--include='+DEVICE, '--include='+HOST, str(PATCH)],
                           cwd=root, check=True, capture_output=True,
                           env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(root.parent)})
            for name, text in HEADERS.items():
                path = root/'include'/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            (root/'control.c').write_text(HARNESS)
            binary = root/'control'
            subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                            '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                            '-I'+str(root/'include'), '-I'+str(root), str(root/'control.c'),
                            '-o', str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, timeout=10,
                           env={**os.environ, 'ASAN_OPTIONS': 'abort_on_error=1:detect_leaks=0',
                                'UBSAN_OPTIONS': 'halt_on_error=1'})
