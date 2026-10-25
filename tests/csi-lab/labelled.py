"""Deterministic host schedule for one declared CSI recording experiment.

No device access, flashing, room-label inference or threshold tuning. The caller
supplies validated console events and fresh human confirmations, executes the
returned commands and always retains responsibility for hardware restoration.
All durations are host monotonic seconds; the recording retains device-clock
labels/checkpoints for analysis rather than reconstructing them from reports.
"""

import math


class Schedule:
    LIMIT_SECONDS = 590
    HUMAN_SECONDS = 120
    TIMED = {
        "training": (60, "TRAIN_STOP", "wait_train_stop"),
        "background": (31, "CALIBRATE_VALIDATE", "wait_holdout"),
        "holdout": (60, "CALIBRATE_ACCEPT", "wait_accept"),
    }

    def __init__(self):
        self.phase = "settling"
        self.since = 0
        self.last_time = 0
        self.latest = None
        self.gain_ready = False
        self.frozen = None
        self.outcome = None
        self.pending_label = None
        self.counts = {}

    def _clock(self, now):
        if not math.isfinite(now) or now < self.last_time:
            raise ValueError("invalid trial clock")
        self.last_time = now

    def _phase(self, phase, now):
        self.phase, self.since = phase, now

    def stop(self, reason, now):
        self._clock(now)
        if self.outcome is not None:
            return []
        self.outcome = reason
        self.pending_label = None
        self._phase("ending", now)
        return ["END"]

    def confirm(self, label, now):
        self._clock(now)
        if self.pending_label != label or self.outcome is not None:
            raise ValueError("wrong or unsolicited human phase")
        if now - self.since >= self.HUMAN_SECONDS or now >= self.LIMIT_SECONDS:
            raise ValueError("late human phase")
        self.pending_label = None
        phase, command = {
            "walking": ("walking", "LABEL_WALKING"),
            "out": ("empty_return", "LABEL_EMPTY"),
        }[label]
        self._phase(phase, now)
        return [command]

    def tick(self, event, now):
        self._clock(now)
        if self.outcome is not None:
            return []
        if now >= self.LIMIT_SECONDS:
            return self.stop("overall_deadline", now)
        if self.pending_label and now - self.since >= self.HUMAN_SECONDS:
            return self.stop("human_confirmation_timeout", now)
        if isinstance(event, dict):
            kind = event.get("kind")
            if kind == "lab_recorder_status" and (
                event["losses"]
                or any(
                    event[k] is not None and event[k] < 512
                    for k in ("rf_stack", "gain_stack", "compute_stack")
                )
            ):
                return self.stop("recorder_loss_or_stack_margin", now)
            if kind == "numerical_probe" and any(
                event[k]
                for k in (
                    "bad_input",
                    "bad_output",
                    "kernel_errors",
                    "uninspected",
                    "bad_template",
                    "bad_current",
                )
            ):
                return self.stop("native_numerical_fault", now)
            if kind == "numeric_failure":
                return self.stop("native_numerical_fault", now)
            if kind == "rf_trace_status":
                if event["faults"]:
                    return self.stop("rf_provenance_fault", now)
                self.gain_ready = event["gain_ready"]
            if kind == "corrected_calibration":
                self.latest = event
                if event["phase"] == "rejected":
                    return self.stop("calibration_rejected:" + event["rejection"], now)
                if event["phase"] in ("holdout", "ready"):
                    values = tuple(
                        event[k]
                        for k in ("enter", "exit", "background_min", "background_max")
                    )
                    if self.frozen is None:
                        self.frozen = values
                    elif values != self.frozen:
                        return self.stop("thresholds_changed_after_background", now)
                if self.phase in ("empty_control", "walking", "empty_return"):
                    if event["phase"] != "ready":
                        return self.stop("accepted_model_lost", now)
                    counts = self.counts.setdefault(
                        self.phase, dict(unknown=0, not_detected=0, presence_hint=0)
                    )
                    counts[event["state"]] += 1
            if kind == "corrected_unavailable" and self.phase in (
                "empty_control",
                "walking",
                "empty_return",
            ):
                self.counts.setdefault(
                    self.phase, dict(unknown=0, not_detected=0, presence_hint=0)
                )["unknown"] += 1
            if (
                self.phase == "wait_vendor"
                and kind is None
                and event.get("rf_freshness") == "not_exported"
            ):
                if (
                    not event["training"]
                    and event["calibrated"]
                    and event["ready"]
                    and event["templates"] > 0
                ):
                    self._phase("wait_background", now)
                    return ["CALIBRATE_BACKGROUND"]
        if isinstance(event, str):
            acknowledgements = {
                "wait_train_start": ("PRESENCE_TRAIN_START ", "training"),
                "wait_train_stop": ("PRESENCE_TRAIN_STOP ", "wait_vendor"),
                "wait_background": (
                    "PRESENCE_CALIBRATION_CONTROL phase=background",
                    "background",
                ),
                "wait_holdout": (
                    "PRESENCE_CALIBRATION_CONTROL phase=holdout",
                    "holdout",
                ),
                "wait_accept": (
                    "PRESENCE_CALIBRATION_CONTROL phase=ready",
                    "wait_ready",
                ),
            }
            if self.phase in acknowledgements:
                prefix, phase = acknowledgements[self.phase]
                if event.startswith(prefix):
                    if self.phase == "wait_train_stop" and " status=0 " not in event:
                        return self.stop("vendor_training_failed", now)
                    self._phase(phase, now)
                    return []
        if self.phase == "settling" and now - self.since >= 10 and self.gain_ready:
            self._phase("wait_train_start", now)
            return ["TRAIN_START"]
        if self.phase in self.TIMED:
            seconds, command, phase = self.TIMED[self.phase]
            if now - self.since >= seconds:
                if self.phase in ("background", "holdout") and (
                    self.latest is None
                    or self.latest["phase"] != self.phase
                    or self.latest["features"] < 128
                ):
                    return self.stop("insufficient_calibration_features", now)
                self._phase(phase, now)
                return [command]
        if (
            self.phase == "wait_ready"
            and isinstance(event, dict)
            and event.get("kind") == "corrected_calibration"
            and event["phase"] == "ready"
        ):
            self._phase("empty_control", now)
        if self.phase == "empty_control" and now - self.since >= 30:
            counts = self.counts.get(self.phase, {})
            if (
                counts.get("not_detected", 0) < 10
                or counts.get("presence_hint", 0)
                or counts.get("unknown", 0)
            ):
                return self.stop("empty_control_failed_or_unavailable", now)
            self.pending_label = "walking"
            self._phase("await_walking", now)
            return ["LABEL_UNKNOWN"]
        if self.phase == "walking" and now - self.since >= 60:
            self.pending_label = "out"
            self._phase("await_empty_return", now)
            return ["LABEL_UNKNOWN"]
        if self.phase == "empty_return" and now - self.since >= 60:
            if any(
                sum(self.counts.get(p, {}).values()) < 10
                for p in ("walking", "empty_return")
            ):
                return self.stop("insufficient_labelled_reports", now)
            return self.stop("labelled_sequence_complete", now)
        if (
            self.phase.startswith("wait_") or self.phase == "settling"
        ) and now - self.since >= 20:
            return self.stop("command_or_readiness_deadline", now)
        return []
