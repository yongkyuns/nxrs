"""Compile real NuttX endpoint setup and observe its Thread initialization."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PLATFORM = Path(__file__).resolve().parent / 'rust/src/platform/nuttx.rs'
HARNESS = r'''
#![allow(dead_code)]
use std::sync::Mutex;

static EVENTS: Mutex<Vec<&str>> = Mutex::new(Vec::new());
static SCHEDULE: Mutex<Option<[i32; 2]>> = Mutex::new(None);

// Only the included platform module sees this binding. Thread creation and
// all standard-library behavior stay real; the wrapper records this boundary.
mod recorded_std {
    pub use ::std::*;
    pub mod thread {
        pub use ::std::thread::*;
        pub fn current() -> Thread {
            crate::EVENTS.lock().unwrap().push("current");
            ::std::thread::current()
        }
    }
}

mod platform {
    pub mod native {
        pub fn restore(value: [i32; 2]) {
            crate::EVENTS.lock().unwrap().push("restore");
            *crate::SCHEDULE.lock().unwrap() = Some(value);
        }
        pub fn priority() -> u32 {
            crate::EVENTS.lock().unwrap().push("priority");
            crate::SCHEDULE.lock().unwrap().expect("schedule was set")[1] as u32
        }
        pub fn fatal() -> ! {
            panic!("unexpected native fatal")
        }
    }
    pub mod nuttx {
        use crate::recorded_std as std;
        include!(env!("NUTTX_SETUP_SOURCE"));
    }
}

fn check(ok: bool, why: &str) { assert!(ok, "{why}"); }
fn mpsc_entry() { panic!("the workload must not run during setup") }
fn execute_std(_: &[&str]) {}
fn execute_native(_: &[&str]) {}

fn main() {
    // configure is also the controller's entry: it must establish CURRENT
    // without relying on the later worker warmup or any channel operation.
    let priority = platform::nuttx::configure();
    EVENTS.lock().unwrap().push("returned");
    assert_eq!(priority, 180);
    assert_eq!(*SCHEDULE.lock().unwrap(), Some([1, 180]));
    let events = EVENTS.lock().unwrap();
    let initialized = events.iter().position(|event| *event == "current")
        .expect("configure returned without initializing the controller Thread");
    let returned = events.iter().position(|event| *event == "returned").unwrap();
    assert!(initialized < returned);
    println!("controller Thread initialized before configure returned");
}
'''


def run_setup(source, backend):
    """Use the actual module with the same generated backend entry as build.rs."""
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        (directory / 'backend.rs').write_text(
            f'pub fn execute(words: &[&str]) {{ crate::execute_{backend}(words); }}\n')
        harness = directory / 'setup.rs'
        harness.write_text(HARNESS)
        binary = directory / 'setup'
        env = dict(os.environ, OUT_DIR=str(directory),
                   NUTTX_SETUP_SOURCE=str(Path(source).resolve()))
        subprocess.run(['rustc', '--edition=2024', '-C', 'debuginfo=0',
                        str(harness), '-o', str(binary)],
                       env=env, check=True, capture_output=True, text=True, timeout=60)
        return subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)


class NuttxSetupTests(unittest.TestCase):
    def test_controller_thread_is_initialized_for_both_transports(self):
        for backend in ('std', 'native'):
            with self.subTest(backend=backend):
                result = run_setup(PLATFORM, backend)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('controller Thread initialized before configure returned',
                              result.stdout)


if __name__ == '__main__':
    unittest.main()
