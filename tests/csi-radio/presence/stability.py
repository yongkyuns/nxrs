"""Bounded host checks for two existing reference modes; never opens hardware."""
from enum import Enum
import re


class Case(Enum):
    VENDOR = "vendor_unrecorded"
    RECORDED = "sampling_recorded"

    def options(self):
        if self is Case.VENDOR:
            return {}
        return dict(trace=True, calibrated=True, numeric_failures=True,
                    motion_case="temporal-motion-v1")


class Trial:
    def __init__(self, case, seconds=120):
        if not isinstance(case, Case) or type(seconds) is not int or not 30 <= seconds <= 120:
            raise ValueError("declared stability case and bounded duration required")
        self.case, self.seconds = case, seconds
        self.samples = self.last_ms = 0
        self.probe = self.stack = self.owner = None
        self.progress_ms = None
        self.end = None

    def observe(self, event):
        if self.end is not None:
            raise ValueError("event after END")
        if isinstance(event, str):
            match = re.fullmatch(
                r"PRESENCE_END ms=(\d+) reports=(\d+) state=unknown resources=retained_until_reset", event)
            if not match:
                raise ValueError("unexpected control acknowledgement")
            self.end = tuple(map(int, match.groups()))
        elif isinstance(event, dict):
            kind = event.get("kind")
            if "rf_freshness" in event:
                if event["training"] or (self.samples and event["ms"] <= self.last_ms):
                    raise ValueError("training or reversed sample clock")
                self.samples += 1
                self.last_ms = event["ms"]
            elif kind == "numerical_probe":
                if any(event[k] for k in ("bad_input", "bad_output", "kernel_errors")):
                    raise ValueError("native numerical failure")
                if self.probe and event["windows"] < self.probe["windows"]:
                    raise ValueError("numerical activity counter reversed")
                if event["windows"] > (self.probe["windows"] if self.probe else 0):
                    self.progress_ms = self.last_ms
                if ((self.progress_ms is None and self.last_ms >= 10000)
                        or (self.progress_ms is not None and self.last_ms - self.progress_ms > 2000)):
                    raise ValueError("native numerical activity stalled")
                self.probe = event
            elif kind == "lab_recorder_status":
                if self.case is not Case.RECORDED or event["losses"]:
                    raise ValueError("unexpected recording or recorder loss")
                self.stack = event
            elif kind == "model_owner_status":
                if self.case is not Case.RECORDED:
                    raise ValueError("unexpected model owner")
                self.owner = event
            elif kind == "numeric_failure":
                raise ValueError("native numerical failure capture")

    def finish(self, recording=None, recorder_exit=None):
        if (self.end is None or not self.seconds * 1000 <= self.end[0] <= self.seconds * 1000 + 2000
                or self.end[1] != self.samples or self.samples < self.seconds
                or self.last_ms < (self.seconds - 2) * 1000
                or not self.probe or self.probe["windows"] < 10
                or self.progress_ms is None or self.last_ms - self.progress_ms > 2000):
            raise ValueError("incomplete duration, reports or native activity")
        if self.case is Case.RECORDED:
            if (recorder_exit != 0 or not recording or not recording.get("footer")
                    or recording["footer"]["losses"] != 0 or recording["feature_events"] < 10
                    or recording["native_outputs"] < 10 or not self.owner or not self.stack
                    or self.owner["received"] != self.owner["applied"]
                    or self.owner["applied"] != recording["feature_events"] or self.owner["pending"]
                    or any(self.stack[k] is None or self.stack[k] < 512
                           for k in ("rf_stack", "gain_stack", "compute_stack"))):
                raise ValueError("incomplete recording, accounting or native stack evidence")
        elif recording is not None or recorder_exit is not None:
            raise ValueError("unrecorded control received recording evidence")
        return dict(case=self.case.value, requested_seconds=self.seconds,
                    elapsed_ms=self.end[0], reports=self.samples, native_windows=self.probe["windows"],
                    result="bounded_workload_completed", presence_qualified=False,
                    cause_proven=False, callback_retirement="whole_chip_reset_required",
                    scope="This interval only; workloads differ in tracing, policy and USB load. No accuracy, CPU-cost or long-term reliability claim.")
