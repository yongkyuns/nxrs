"""Bounded, software-only controller for temporal-motion-v1 qualification.

Rust owns calibration and detection. This controller owns human cues and fixed
evaluation phases. Only complete recorded RF bins count; console status cadence
and time spent waiting for a human never count as measurements. A completed
sequence is data, not an accuracy or stationary-presence qualification.
"""

import math
import re
import struct
import zlib


def record_event(line):
    """Read label/motion evidence; the recorder separately verifies the journal.

    This is intentionally a bounded envelope decoder, not a replacement for the
    Rust recorder's sequence, footer, native identity and exact replay checks.
    """
    if not re.fullmatch(rb"CSI_LAB (?:[0-9a-fA-F]{8}){8,264}\n", line):
        raise ValueError("recorder framing")
    words = [int(line[i : i + 8], 16) for i in range(8, len(line) - 1, 8)]
    if words[:2] != [0x4C495343, 1] or words[6] > 256 or len(words) != words[6] + 8:
        raise ValueError("recorder envelope")
    if zlib.crc32(struct.pack("<" + "I" * (len(words) - 1), *words[:-1])) != words[-1]:
        raise ValueError("recorder CRC")
    time_us = words[3] | words[4] << 32
    payload = words[7:-1]
    if words[5] == 7:
        if len(payload) != 1 or payload[0] > 3:
            raise ValueError("recorded label")
        return dict(kind="recorded_label", time_us=time_us, label=payload[0])
    if words[5] != 10:
        return None
    w = payload
    if (
        len(w) != 32
        or w[0] != 1
        or not 1 <= w[1] <= 9
        or w[2] > 4
        or w[3] > 2
        or w[4] > 12
        or w[5] > 7
        or (w[4] == 1) != (w[5] != 0)
        or w[6] > 3
        or w[10] > 1
        or w[15] > 1
        or any(w[26:])
        or (not w[10] and any(w[11:15]))
        or (not w[15] and any(w[16:26]))
        or (w[2] == 4) != bool(w[4])
        or (w[2] != 3 and w[3] != 0)
        or w[8] + w[9] > w[7]
        or (w[2] in (2, 3) and not w[10])
    ):
        raise ValueError("motion checkpoint")
    if w[10]:
        centre, mad, enter, exit_score = struct.unpack(
            "<ffff", struct.pack("<IIII", *w[11:15])
        )
        if (
            any(
                not math.isfinite(v) or not 0 <= v < 1
                for v in (centre, mad, enter, exit_score)
            )
            or not centre <= exit_score < enter
        ):
            raise ValueError("motion checkpoint thresholds")
    event = dict(
        kind="recorded_motion",
        time_us=time_us,
        phase=("uncalibrated", "background", "holdout", "ready", "rejected")[w[2]],
        state=("unknown", "idle", "moving")[w[3]],
        fault=w[4],
        label=w[6],
        thresholds=tuple(w[11:15]) if w[10] else None,
        bin=None,
    )
    if w[15]:
        b = dict(
            id=w[16] | w[17] << 32,
            oldest_us=w[18] | w[19] << 32,
            newest_us=w[20] | w[21] << 32,
            samples=w[24],
        )
        score = struct.unpack("<f", struct.pack("<I", w[25]))[0]
        if not (
            3 <= b["samples"] <= 64
            and b["oldest_us"] <= b["newest_us"] <= time_us
            and math.isfinite(score)
            and 0 <= score <= 1
        ):
            raise ValueError("motion bin support")
        event["bin"] = b
    return event


class Schedule:
    LIMIT_SECONDS = 590
    HUMAN_SECONDS = 120
    BIN_COUNT = 240
    SPAN_US = 60_000_000

    def __init__(self, *, confirmed_empty):
        if confirmed_empty is not True:
            raise ValueError("fresh explicit empty-room confirmation required")
        self.phase, self.since, self.last_time = "settling", 0, 0
        self.pending_label = self.outcome = self.frozen = None
        self.gain_ready = False
        self.counts = {}
        self.measurements = {}
        self.last_rf = self.last_rf_host = self.last_bin = None
        self.boundary = None
        self.expected_label = 1  # Initial LABEL_EMPTY is executed by the caller.
        self.after_label = "settling"

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
        self.outcome, self.pending_label = reason, None
        self._phase("ending", now)
        return ["END"]

    def confirm(self, label, now):
        self._clock(now)
        if self.pending_label != label or self.outcome is not None:
            raise ValueError("wrong or unsolicited human phase")
        if now - self.since >= self.HUMAN_SECONDS or now >= self.LIMIT_SECONDS:
            raise ValueError("late human phase")
        number, phase, command = {
            "walking": (2, "walking", "LABEL_WALKING"),
            "seated": (3, "seated", "LABEL_SEATED"),
            "out": (1, "return_settling", "LABEL_EMPTY"),
        }[label]
        self.expected_label, self.after_label, self.pending_label = number, phase, None
        self._phase("wait_label", now)
        return [command]

    def _request(self, label, now):
        self.pending_label = label
        self._phase("await_" + label, now)
        return ["LABEL_UNKNOWN"]

    def _measure(self, event, now):
        b = event["bin"]
        if b is None or self.boundary is None or b["oldest_us"] < self.boundary:
            return []
        if (
            event["label"]
            != {"empty_control": 1, "walking": 2, "seated": 3, "empty_return": 1}[
                self.phase
            ]
        ):
            return self.stop("measurement_label_mismatch", now)
        counts = self.counts.setdefault(self.phase, dict(unknown=0, idle=0, moving=0))
        counts[event["state"]] += 1
        m = self.measurements.setdefault(
            self.phase, dict(first_oldest_us=b["oldest_us"], last_newest_us=0, bins=0)
        )
        m["last_newest_us"], m["bins"] = b["newest_us"], m["bins"] + 1
        if self.phase == "empty_control" and event["state"] != "idle":
            return self.stop("independent_empty_control_failed", now)
        if (
            m["bins"] >= self.BIN_COUNT
            and b["newest_us"] - m["first_oldest_us"] >= self.SPAN_US
        ):
            next_label = {
                "empty_control": "walking",
                "walking": "seated",
                "seated": "out",
            }.get(self.phase)
            return (
                self._request(next_label, now)
                if next_label
                else self.stop("labelled_sequence_complete", now)
            )
        return []

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
            if kind == "numeric_failure" or (
                kind == "numerical_probe"
                and any(
                    event[k]
                    for k in (
                        "bad_input",
                        "bad_output",
                        "kernel_errors",
                        "uninspected",
                        "bad_template",
                        "bad_current",
                    )
                )
            ):
                return self.stop("native_numerical_fault", now)
            if kind == "rf_trace_status":
                if event["faults"]:
                    return self.stop("rf_provenance_fault", now)
                self.gain_ready = event["gain_ready"]
            if kind == "motion_trial_unavailable":
                return self.stop("motion_owner_unavailable", now)
            if kind == "recorded_label" and event["label"] != 0:
                if event["label"] != self.expected_label:
                    return self.stop("unexpected_recorded_label", now)
                self.expected_label = None
                self.boundary = event["time_us"]
                if self.phase == "wait_label":
                    self._phase(self.after_label, now)
            if kind == "recorded_motion":
                if self.last_rf is not None and event["time_us"] < self.last_rf:
                    return self.stop("recorded_clock_reversed", now)
                self.last_rf, self.last_rf_host = event["time_us"], now
                if event["phase"] == "rejected":
                    return self.stop("motion_rejected:" + str(event["fault"]), now)
                if event["thresholds"] is not None:
                    if self.frozen is None:
                        self.frozen = event["thresholds"]
                    elif self.frozen != event["thresholds"]:
                        return self.stop("thresholds_changed_after_background", now)
                if (
                    self.phase
                    in (
                        "empty_control",
                        "walking",
                        "seated",
                        "return_settling",
                        "empty_return",
                        "ready_settling",
                    )
                    and event["phase"] != "ready"
                ):
                    return self.stop("accepted_model_lost", now)
                if self.phase == "wait_background" and event["phase"] == "background":
                    self._phase("background", now)
                if self.phase == "background" and event["phase"] == "holdout":
                    self._phase("holdout", now)
                if self.phase == "holdout" and event["phase"] == "ready":
                    self.boundary = event["time_us"] + 2_000_000
                    self._phase("ready_settling", now)
                if (
                    self.phase in ("ready_settling", "return_settling")
                    and event["time_us"] >= self.boundary
                ):
                    self._phase(
                        (
                            "empty_control"
                            if self.phase == "ready_settling"
                            else "empty_return"
                        ),
                        now,
                    )
                b = event["bin"]
                if b is not None:
                    if self.last_bin is not None and b["id"] <= self.last_bin:
                        return self.stop("duplicate_or_reversed_bin", now)
                    self.last_bin = b["id"]
                    if self.phase in (
                        "empty_control",
                        "walking",
                        "seated",
                        "empty_return",
                    ):
                        commands = self._measure(event, now)
                        if commands:
                            return commands
        if isinstance(event, str):
            if self.phase == "wait_train_start" and event.startswith(
                "PRESENCE_TRAIN_START "
            ):
                self._phase("training", now)
            elif self.phase == "wait_train_stop" and event.startswith(
                "PRESENCE_TRAIN_STOP "
            ):
                if " status=0 " not in event:
                    return self.stop("vendor_training_failed", now)
                self._phase("wait_background", now)
        if self.phase == "return_settling" and self.boundary is not None:
            # Set once from the actual LABEL_EMPTY time; no quiet-state selection.
            if self.after_label == "return_settling":
                self.boundary += 10_000_000
                self.after_label = None
        if (
            self.phase == "settling"
            and self.expected_label is None
            and now >= 10
            and self.gain_ready
        ):
            self._phase("wait_train_start", now)
            return ["TRAIN_START"]
        if self.phase == "training" and now - self.since >= 60:
            self._phase("wait_train_stop", now)
            return ["TRAIN_STOP"]
        if self.last_rf_host is not None and now - self.last_rf_host >= 2:
            return self.stop("recorded_motion_silent", now)
        deadline = {
            "background": 45,
            "holdout": 75,
            "ready_settling": 5,
            "return_settling": 15,
            "empty_control": 75,
            "walking": 75,
            "seated": 75,
            "empty_return": 75,
        }.get(
            self.phase,
            20 if self.phase.startswith("wait_") or self.phase == "settling" else None,
        )
        if deadline is not None and now - self.since >= deadline:
            return self.stop("phase_deadline:" + self.phase, now)
        return []
