"""Regression checks for experiment controls; no board access."""
from pathlib import Path
import re
import unittest

HERE = Path(__file__).resolve().parent


class FollowupFirmwareTests(unittest.TestCase):
    def test_c_and_rust_work_modes_have_identical_bounded_iterations(self):
        c = (HERE / "controls.h").read_text()
        rust = (HERE / "controls.rs").read_text()
        for name, expected in (("SHORT", 10000), ("MEDIUM", 100000), ("LONG", 400000)):
            cv = int(re.search(rf"#define ES_WORK_{name} (\d+)u", c)[1])
            rv = int(re.search(rf"pub const WORK_{name}: u32 = ([\d_]+);", rust)[1].replace("_", ""))
            self.assertEqual((cv, rv), (expected, expected))

    def test_one_ms_control_does_not_change_timeslice_or_stack_settings(self):
        values = (HERE / "timer-1ms.conf").read_text()
        self.assertEqual(re.findall(r"^CONFIG_.*", values, re.M),
                         ["CONFIG_SYS_CLOCK_TICKS_PER_SEC=1000"])
        executor = (HERE / "embassy.rs").read_text()
        self.assertIn("timer.delay_millis_async(controls::timer_ms()).await", executor)

    def test_saturation_tasks_are_parked_before_their_inboxes_are_filled(self):
        source = (HERE / "embassy.rs").read_text()
        service = source.split("async fn service(id: usize)", 1)[1].split("fn spawn(", 1)[0]
        self.assertLess(service.index("GATES[id].wait().await"),
                        service.index("== 6"))
        self.assertLess(service.index("== 6"), service.index("while now() < stop"))
        saturation = source.split("fn saturation(console:", 1)[1].split("#[esp_hal::main]", 1)[0]
        self.assertLess(saturation.index("READY.load(Ordering::Acquire) != 20"),
                        saturation.index("try_send(event)"))
        self.assertLess(saturation.index("depth_zero"), saturation.index("gate.signal(())"))

    def test_io_wait_is_pending_timer_not_cpu_work_or_queue_polling(self):
        source = (HERE / "embassy.rs").read_text()
        wait = source.split("async fn wait_io(id: usize)", 1)[1].split("async fn run_work", 1)[0]
        self.assertIn("set_deadline(id, deadline, cx.waker())", wait)
        self.assertIn("Poll::Pending", wait)
        self.assertNotIn("handoff()", wait)
        self.assertNotIn("INBOXES", wait)
        c = (HERE / "platform_nuttx.c").read_text()
        z = (HERE / "platform_zephyr.c").read_text()
        self.assertIn("nanosleep(&delay, NULL)", c)
        self.assertIn("k_sleep(K_USEC(remaining))", z)

    def test_budget_handoff_requires_ready_backlog_and_chunk_index_advances(self):
        source = (HERE / "embassy.rs").read_text()
        wait = source.split("async fn wait_any(", 1)[1].split("async fn wait_io", 1)[0]
        self.assertIn('policy() == "budget" && ready && budget.exhausted(now())', wait)
        self.assertIn("suspended = true", wait)
        work = source.split("async fn run_work(", 1)[1].split("async fn clock(", 1)[0]
        self.assertIn("work_value_range(value, token, start, count)", work)
        self.assertIn("start += count", work)
        self.assertIn("if start < iterations", work)
        self.assertIn("handoff().await", work)

    def test_suspension_and_chunk_handoffs_reset_the_work_budget(self):
        source = (HERE / "embassy.rs").read_text()
        work = source.split("async fn run_work(", 1)[1].split("async fn clock(", 1)[0]
        self.assertRegex(work, r"handoff\(\)\.await;\s+budget\.reset\(now\(\)\)")
        service = source.split("async fn service(id: usize)", 1)[1].split("fn spawn(", 1)[0]
        resumed = service.split("wait_io(id).await;", 1)[1].split("extra.io_jobs", 1)[0]
        self.assertIn("budget.reset(now());", resumed)


if __name__ == "__main__":
    unittest.main()
