#!/usr/bin/env python3
"""Full reference serial protocol. No discovery, flashing or reset operations."""
import importlib.util
import math
import struct
from pathlib import Path
import re
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "idf_protocol", HERE.parent / "idf60/run.py"
)
IDF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(IDF)
spec = importlib.util.spec_from_file_location(
    "presence_snapshots", HERE / "snapshots.py"
)
SNAPSHOTS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SNAPSHOTS)
spec = importlib.util.spec_from_file_location("presence_trace", HERE / "trace.py")
TRACE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TRACE)
spec = importlib.util.spec_from_file_location(
    "presence_calibration_pairs", HERE / "calibration_pairs.py"
)
PAIRS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PAIRS)
spec = importlib.util.spec_from_file_location("presence_failures", HERE / "failures.py")
FAILURES = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FAILURES)
FIELDS = (
    "ms state motion init training ready calibrated wander jitter average threshold "
    "templates background advisory rf_freshness"
).split()
PROBE_FIELDS = (
    "windows bad_input bad_output kernel_errors uninspected wander_calls "
    "bad_template bad_current constant_template constant_current last_error error_cols error_rows0 error_rows1"
).split()


def probe(line):
    parts = line.decode("ascii").split()
    if not parts or parts[0] != "PRESENCE_PROBE":
        raise ValueError("not a numerical probe")
    pairs = [part.split("=", 1) for part in parts[1:]]
    if len(pairs) != len(PROBE_FIELDS) or any(len(pair) != 2 for pair in pairs):
        raise ValueError("numerical probe schema mismatch")
    result = dict(pairs)
    if set(result) != set(PROBE_FIELDS) or any(
        not re.fullmatch(
            r"-?[0-9]{1,10}" if key == "last_error" else r"[0-9]{1,10}", value
        )
        for key, value in result.items()
    ):
        raise ValueError("invalid numerical probe")
    return {
        "kind": "numerical_probe",
        **{key: int(value) for key, value in result.items()},
    }


def sample(line):
    if not line.startswith(b"PRESENCE_SAMPLE "):
        raise ValueError("not a presence sample")
    parts = line.decode("ascii").split()[1:]
    pairs = [part.split("=", 1) for part in parts]
    if len(pairs) != len(FIELDS) or any(len(pair) != 2 for pair in pairs):
        raise ValueError("presence sample schema mismatch")
    result = dict(pairs)
    if set(result) != set(FIELDS):
        raise ValueError("missing, duplicate or unexpected presence field")
    for key in (
        "ms",
        "motion",
        "init",
        "training",
        "ready",
        "calibrated",
        "templates",
        "background",
        "advisory",
    ):
        if not re.fullmatch(r"[0-9]{1,10}", result[key]):
            raise ValueError("invalid presence counter")
        result[key] = int(result[key])
    for key in ("wander", "jitter", "average", "threshold"):
        result[key] = float(result[key])
        if not math.isfinite(result[key]) or result[key] < 0:
            raise ValueError("invalid presence numerical result")
    if (
        any(result[key] not in (0, 1) for key in ("training", "ready", "calibrated"))
        or result["motion"] not in range(4)
        or result["init"] not in range(3)
        or result["advisory"] != 1
        or result["rf_freshness"] != "not_exported"
    ):
        raise ValueError("invalid presence status")
    available = not result["training"] and result["ready"] and result["calibrated"]
    if result["state"] not in ("unknown", "presence_hint", "not_detected"):
        raise ValueError("invalid presence state")
    if (result["state"] != "unknown") != bool(available):
        raise ValueError("uncalibrated or warming state treated as detection")
    return result


def motion_status(line):
    fields = line.decode("ascii").split()
    if not fields or fields[0] not in (
        "PRESENCE_MOTION",
        "PRESENCE_MOTION_UNAVAILABLE",
    ):
        raise ValueError("not a motion trial report")
    pairs = [v.split("=", 1) for v in fields[1:]]
    if any(len(v) != 2 for v in pairs) or len(dict(pairs)) != len(pairs):
        raise ValueError("motion report fields")
    result = dict(pairs)
    common = {"v", "time_us", "state", "advisory", "physical_detector_admitted"}
    unavailable = fields[0] == "PRESENCE_MOTION_UNAVAILABLE"
    counters = {
        "phase_bins",
        "positive_bins",
        "quiet_bins",
        "thresholds",
        "centre_bits",
        "mad_bits",
        "enter_bits",
        "exit_bits",
    }
    expected = common | ({"reason"} if unavailable else counters | {"phase", "fault"})
    if set(result) != expected:
        raise ValueError("motion report schema")
    for key in common - {"state"} | (set() if unavailable else counters):
        if not re.fullmatch(
            r"[0-9]{1,20}" if key == "time_us" else r"[0-9]{1,10}", result[key]
        ):
            raise ValueError("motion report number")
        result[key] = int(result[key])
        if result[key] > (0x7FFFFFFFFFFFFFFF if key == "time_us" else 0xFFFFFFFF):
            raise ValueError("motion report bound")
    if (
        result["v"] != 2
        or result["advisory"] != 1
        or result["physical_detector_admitted"] != 0
    ):
        raise ValueError("motion report admission/version")
    if unavailable:
        if result["state"] != "unknown" or result["reason"] != "owner_unavailable":
            raise ValueError("motion unavailable state")
        return dict(kind="motion_trial_unavailable", **result)
    phases = {
        "Uncalibrated": "uncalibrated",
        "Background": "background",
        "Holdout": "holdout",
        "Ready": "ready",
        "Rejected": "rejected",
    }
    states = {"Unknown": "unknown", "Idle": "idle", "Moving": "moving"}
    if result["phase"] not in phases or result["state"] not in states:
        raise ValueError("motion report state")
    result["phase"] = phases[result["phase"]]
    result["state"] = states[result["state"]]
    reason = r"Signal\((?:Shape|Nonfinite|Degenerate|SourceReversed|InvalidTime|Expired|ClockRollback)\)|InvalidSample|SourceReversed|ClockRollback|Expired|Overload|BinGap|SparseBin|SaturatedBackground|HoldoutMotion|SourceUnavailable|LabelViolation"
    if result["fault"] != "None" and not re.fullmatch(
        r"Some\((?:" + reason + r")\)", result["fault"]
    ):
        raise ValueError("motion report fault")
    if (result["phase"] == "rejected") != (result["fault"] != "None") or (
        result["phase"] != "ready" and result["state"] != "unknown"
    ):
        raise ValueError("unavailable motion treated as clear")
    if result["positive_bins"] + result["quiet_bins"] > result["phase_bins"] or result[
        "thresholds"
    ] not in (0, 1):
        raise ValueError("motion counters/threshold admission")
    bits = tuple(
        result[k] for k in ("centre_bits", "mad_bits", "enter_bits", "exit_bits")
    )
    values = [struct.unpack("<f", struct.pack("<I", v))[0] for v in bits]
    if not result["thresholds"]:
        if any(bits) or result["phase"] in ("holdout", "ready"):
            raise ValueError("motion thresholds unavailable")
    elif (
        any(not math.isfinite(v) or not 0 <= v <= 1 for v in values)
        or values[2] >= 1
        or values[2] <= values[3]
    ):
        raise ValueError("motion threshold values")
    return dict(kind="motion_trial_status", **result)


class Client:
    def __init__(self, console):
        self.console = console
        self.protocol = IDF.Client(console)
        self.secrets = ()
        self.last_ms = None
        self.snapshots = False
        self.trace = False
        self.calibrated = False
        self.last_corrected_ms = None
        self.numeric_failures = False
        self.failure_sources = set()
        self.failure_calls = {}
        self.diagnostic_pairs = False
        self.pair_tags = set()
        self.trace_ids = set()
        self.snapshot_ids = set()
        self.record_sink = None
        self.motion_case = "historical"
        self.motion_training_closed = False
        self.last_motion_us = None
        self.source_counts = None

    def line(self, deadline):
        while True:
            line = self.protocol.line(deadline, self.secrets)
            if not line.startswith(b"CSI_LAB"):
                break
            if self.record_sink is None:
                raise RuntimeError("unexpected continuous recorder")
            self.record_sink(line + b"\n")
        if line == b"PRESENCE_FAIL" or line.startswith(b"PRESENCE_FAIL "):
            raise RuntimeError("presence reference failed; diagnostic withheld")
        return line

    def start(
        self,
        ssid,
        password,
        seconds,
        amplitude="log",
        *,
        snapshots=False,
        trace=False,
        calibrated=False,
        diagnostic_pairs=False,
        numeric_failures=False,
        record_sink=None,
        motion_case="historical",
    ):
        recording = record_sink is not None
        if motion_case not in ("historical", "temporal-motion-v1", "temporal-motion-ap-v1") or (
            motion_case != "historical" and not recording
        ):
            raise ValueError("motion experiment requires declared recording case")
        self.motion_case = motion_case
        if recording and (
            not callable(record_sink)
            or snapshots
            or diagnostic_pairs
            or not trace
            or not calibrated
            or not numeric_failures
            or amplitude != "linear"
        ):
            raise ValueError(
                "continuous recording requires independent frozen-input mode without legacy snapshots"
            )
        if type(trace) is not bool or (trace and not snapshots and not recording):
            raise ValueError("trace requires explicit snapshot mode")
        self.record_sink = record_sink
        if type(snapshots) is not bool:
            raise ValueError("explicit boolean snapshot selection required")
        if type(calibrated) is not bool or (
            calibrated
            and (
                not trace or (not snapshots and not recording) or amplitude != "linear"
            )
        ):
            raise ValueError("calibrated case requires linear enqueue-owned RF trace")
        if not 30 <= seconds <= 600:
            raise ValueError("presence duration outside bound")
        if type(diagnostic_pairs) is not bool or (diagnostic_pairs and not calibrated):
            raise ValueError("diagnostic pairs require calibrated case")
        if type(numeric_failures) is not bool or (
            numeric_failures and not diagnostic_pairs and not recording
        ):
            raise ValueError("numeric failures require exact-event diagnostic mode")
        if amplitude not in ("log", "linear"):
            raise ValueError("unsupported amplitude case")
        frame, self.secrets = IDF.credential_frame(ssid, password)
        deadline = time.monotonic() + 45
        while self.line(deadline) != b"PRESENCE_COMMAND_READY force=0":
            pass
        self.snapshots = snapshots
        self.trace = trace
        self.calibrated = calibrated
        self.numeric_failures = numeric_failures
        self.diagnostic_pairs = diagnostic_pairs
        command = (
            "PRESENCE_MOTION_AP" if motion_case == "temporal-motion-ap-v1" else
            "PRESENCE_MOTION"
            if motion_case == "temporal-motion-v1"
            else (
                "PRESENCE_RECORD"
                if record_sink is not None
                else (
                    "PRESENCE_FAILURES"
                    if numeric_failures
                    else (
                        "PRESENCE_DIAGNOSTIC"
                        if diagnostic_pairs
                        else (
                            "PRESENCE_CALIBRATED"
                            if calibrated
                            else (
                                "PRESENCE_TRACE"
                                if trace
                                else ("PRESENCE_CAPTURE" if snapshots else "PRESENCE")
                            )
                        )
                    )
                )
            )
        )
        IDF.RADIO.write_console(
            self.console, f"{command} {seconds} {amplitude}\n".encode()
        )
        expected = (
            f"PRESENCE_OPTIONS seconds={seconds} force=0 engine=espressif_full amplitude={amplitude}"
            + (
                " snapshots=0 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap"
                if recording
                else (
                    " snapshots=1 frozen_inputs=1 rf_trace=1 enqueue_frozen=1 gain_warmup=1 fault_site=1 peer_scope=associated_ap"
                    if trace
                    else (" snapshots=1 frozen_inputs=1" if snapshots else "")
                )
            )
            + (" calibration=post_training_v2" if calibrated else "")
            + (" capture=calibration_pairs_v2" if diagnostic_pairs else "")
            + (
                " numeric_failures=source_v1 other_inputs=frozen_enqueue"
                if numeric_failures
                else ""
            )
            + (
                (
                    " record=csi_lab_v8 model_owner=control_v1 motion=temporal_motion_v1 source_admission=associated_ap_v1"
                    if motion_case == "temporal-motion-ap-v1" else
                    " record=csi_lab_v7 model_owner=control_v1 motion=temporal_motion_v1"
                    if motion_case == "temporal-motion-v1"
                    else " record=csi_lab_v4 model_owner=control_v1"
                )
                if record_sink is not None
                else ""
            )
        ).encode()
        matched = False
        while True:
            line = self.line(deadline)
            if line.startswith(b"PRESENCE_OPTIONS "):
                if matched or line != expected:
                    raise RuntimeError(
                        "presence options mismatch; credentials withheld"
                    )
                matched = True
            if line == b"PRESENCE_CREDENTIALS_READY":
                if not matched:
                    raise RuntimeError("presence options missing; credentials withheld")
                break
        IDF.RADIO.write_console(self.console, frame)
        deadline = time.monotonic() + 35
        expected_begin = re.compile(
            rb"PRESENCE_BEGIN seconds="
            + str(seconds).encode()
            + rb" channel=(?:[1-9]|1[0-4]) calibration=required rf_freshness=not_exported"
        )
        while True:
            line = self.line(deadline)
            if line.startswith(b"PRESENCE_BEGIN "):
                if not expected_begin.fullmatch(line):
                    raise RuntimeError("presence begin mismatch")
                return

    def command(self, value):
        if (
            self.motion_case != "historical"
            and value == "TRAIN_START"
            and self.motion_training_closed
        ):
            raise ValueError("motion trial cannot retrain after stop")
        if self.record_sink is not None and value in (
            "LABEL_EMPTY",
            "LABEL_WALKING",
            "LABEL_SEATED",
            "LABEL_UNKNOWN",
        ):
            IDF.RADIO.write_console(self.console, (value + "\n").encode())
            return
        if value not in ("TRAIN_START", "TRAIN_STOP", "END") and not (
            self.calibrated
            and self.motion_case == "historical"
            and value
            in ("CALIBRATE_BACKGROUND", "CALIBRATE_VALIDATE", "CALIBRATE_ACCEPT")
        ):
            raise ValueError("unsupported reference control")
        if self.motion_case != "historical" and value == "TRAIN_STOP":
            self.motion_training_closed = True
        IDF.RADIO.write_console(self.console, (value + "\n").encode())

    def arm_snapshot(self, tag):
        if not self.snapshots or type(tag) is not int or not 1 <= tag <= 16:
            raise ValueError("snapshot mode/tag required")
        if self.diagnostic_pairs and tag >= 14:
            raise ValueError("reserved automatic diagnostic tag")
        IDF.RADIO.write_console(self.console, f"SNAPSHOT {tag}\n".encode())

    def read(self, deadline):
        line = self.line(deadline)
        if line.startswith((b"PRESENCE_MOTION ", b"PRESENCE_MOTION_UNAVAILABLE ")):
            if self.motion_case not in ("temporal-motion-v1", "temporal-motion-ap-v1") or self.record_sink is None:
                raise ValueError("unexpected motion trial report")
            result = motion_status(line)
            now = result["time_us"]
            if self.last_motion_us is not None and now <= self.last_motion_us:
                raise ValueError("motion report clock reversed or duplicate")
            self.last_motion_us = now
            return result
        if line.startswith(b"PRESENCE_MODEL_OWNER "):
            m = re.fullmatch(
                rb"PRESENCE_MODEL_OWNER v=1 slots=64 received=(\d+) applied=(\d+) pending=(\d+)",
                line,
            )
            if self.record_sink is None or not m:
                raise ValueError("model owner status schema")
            received, applied, pending = map(int, m.groups())
            if (
                not 0 <= applied <= received <= 0x7FFFFFFF
                or pending != received - applied
                or pending > 64
            ):
                raise ValueError("model owner counters")
            return dict(
                kind="model_owner_status",
                slots=64,
                received=received,
                applied=applied,
                pending=pending,
            )
        if line.startswith(b"PRESENCE_SOURCE "):
            m = re.fullmatch(rb"PRESENCE_SOURCE v=1 state=2 admitted=(\d+) rejected=(\d+) startup=(\d+)", line)
            if self.motion_case != "temporal-motion-ap-v1" or not m:
                raise ValueError("source admission status/case")
            counts = tuple(map(int, m.groups()))
            if any(v > 0xFFFFFFFF for v in counts) or (
                self.source_counts is not None and any(a < b for a, b in zip(counts, self.source_counts))
            ):
                raise ValueError("source admission counter reversal/overflow")
            self.source_counts = counts
            return dict(kind="source_admission_status", admitted=counts[0], rejected=counts[1], startup=counts[2])
        if line.startswith(b"PRESENCE_LAB_STATUS "):
            m = re.fullmatch(
                rb"PRESENCE_LAB_STATUS v=1 losses=(\d+) slots=128 rf_stack=(\d+) gain_stack=(\d+) compute_stack=(\d+)",
                line,
            )
            if (
                self.record_sink is None
                or not m
                or any(int(v) > 0xFFFFFFFF for v in m.groups())
            ):
                raise ValueError("continuous recorder status schema")
            values = list(map(int, m.groups()))
            if any(v != 0xFFFFFFFF and v > 65536 for v in values[1:]):
                raise ValueError("continuous recorder stack metric")
            return dict(
                kind="lab_recorder_status",
                losses=values[0],
                slots=128,
                **{
                    key: (None if v == 0xFFFFFFFF else v)
                    for key, v in zip(
                        ("rf_stack", "gain_stack", "compute_stack"), values[1:]
                    )
                },
            )
        if line.startswith(b"PRESENCE_NUMERIC_FAILURE "):
            if not self.numeric_failures:
                raise ValueError("unexpected numeric failure mode")
            result = FAILURES.read(line, lambda: self.line(deadline))
            source = result["header"]["source"]
            if source in self.failure_sources:
                raise ValueError("duplicate first numeric failure")
            self.failure_sources.add(source)
            return result
        if line.startswith(b"PRESENCE_FAILURE_QUEUE "):
            if not self.numeric_failures or line != b"PRESENCE_FAILURE_QUEUE losses=0":
                raise ValueError("other-peer enqueue capture loss")
            return dict(kind="numeric_failure_queue", losses=0)
        if line.startswith(b"PRESENCE_FAILURE_STATUS "):
            match = re.fullmatch(
                rb"PRESENCE_FAILURE_STATUS source=(selected|other) calls=(\d+) bad_input=(\d+) bad_output=(\d+) native_errors=(\d+) captured=([01]) losses=(\d+)",
                line,
            )
            if not self.numeric_failures or not match:
                raise ValueError("numeric failure status schema")
            source = match[1].decode()
            calls, bad_input, bad_output, errors, captured, losses = map(
                int, match.groups()[1:]
            )
            if (
                losses
                or any(v > calls for v in (bad_input, bad_output, errors))
                or bad_output + errors > calls
                or calls < self.failure_calls.get(source, 0)
                or (bool(captured) != (source in self.failure_sources))
            ):
                raise ValueError("numeric failure capture loss/counters")
            self.failure_calls[source] = calls
            return dict(
                kind="numeric_failure_status",
                source=source,
                calls=calls,
                bad_input=bad_input,
                bad_output=bad_output,
                native_errors=errors,
                captured=bool(captured),
                losses=losses,
            )
        if line.startswith(b"PRESENCE_PAIR "):
            if not self.diagnostic_pairs:
                raise ValueError("unexpected automatic calibration pair")
            result = PAIRS.read(line, lambda: self.line(deadline))
            if result["header"]["v"] != 2:
                raise ValueError("legacy pair lacks rejection attribution")
            tag = result["header"]["tag"]
            if tag in self.pair_tags:
                raise ValueError("duplicate calibration event")
            self.pair_tags.add(tag)
            return result
        if line.startswith(b"PRESENCE_PAIR_STATUS "):
            match = re.fullmatch(rb"PRESENCE_PAIR_STATUS failures=(\d+)", line)
            if not self.diagnostic_pairs or not match or int(match[1]):
                raise ValueError("automatic calibration capture loss/contention")
            return dict(kind="calibration_pair_status", failures=0)
        if line.startswith(b"PRESENCE_CORRECTED_UNAVAILABLE "):
            match = re.fullmatch(
                rb"PRESENCE_CORRECTED_UNAVAILABLE v=1 ms=([0-9]{1,10}) state=unknown reason=runtime_busy advisory=1",
                line,
            )
            if not self.calibrated or not match:
                raise ValueError("corrected unavailable schema/status")
            ms = int(match[1])
            if self.last_corrected_ms is not None and ms <= self.last_corrected_ms:
                raise ValueError("duplicate or reversed corrected report clock")
            self.last_corrected_ms = ms
            return dict(
                kind="corrected_unavailable",
                ms=ms,
                state="unknown",
                reason="runtime_busy",
                advisory=1,
            )
        if line.startswith(b"PRESENCE_CORRECTED "):
            if not self.calibrated:
                raise ValueError("unexpected corrected calibration case")
            parts = [part.split("=", 1) for part in line.decode("ascii").split()[1:]]
            keys = "v ms phase state rejection features average background_min background_max enter exit advisory".split()
            if len(parts) != len(keys) or any(len(p) != 2 for p in parts):
                raise ValueError("corrected calibration schema")
            value = dict(parts)
            if set(value) != set(keys):
                raise ValueError("corrected calibration fields")
            for key in ("v", "ms", "features", "advisory"):
                if not re.fullmatch(r"[0-9]{1,10}", value[key]):
                    raise ValueError("corrected calibration counter")
                value[key] = int(value[key])
            for key in ("average", "background_min", "background_max", "enter", "exit"):
                value[key] = float(value[key])
                if not math.isfinite(value[key]) or not 0 <= value[key] <= (
                    3 if key == "enter" else 1
                ):
                    raise ValueError("corrected calibration numerical value")
            if (
                value["v"] != 2
                or value["advisory"] != 1
                or value["phase"]
                not in ("uncalibrated", "background", "holdout", "ready", "rejected")
                or value["state"] not in ("unknown", "not_detected", "presence_hint")
            ):
                raise ValueError("corrected calibration status")
            if value["rejection"] not in PAIRS.REJECTIONS or (
                value["rejection"] != "none"
            ) != (value["phase"] == "rejected"):
                raise ValueError("corrected rejection attribution")
            if value["phase"] != "ready" and value["state"] != "unknown":
                raise ValueError("unqualified calibration treated as detection")
            if (
                value["phase"] in ("holdout", "ready")
                and not value["enter"] > value["exit"] >= value["background_max"]
            ):
                raise ValueError("threshold below calibrated background")
            if (
                self.last_corrected_ms is not None
                and value["ms"] <= self.last_corrected_ms
            ):
                raise ValueError("duplicate or reversed corrected report clock")
            self.last_corrected_ms = value["ms"]
            return dict(kind="corrected_calibration", **value)
        if line.startswith(b"PRESENCE_CALIBRATION_CONTROL "):
            if not self.calibrated or not re.fullmatch(
                rb"PRESENCE_CALIBRATION_CONTROL phase=(background|holdout|ready)", line
            ):
                raise ValueError("corrected calibration acknowledgement")
            return line.decode("ascii")
        if line.startswith(b"PRESENCE_TRACE "):
            if not self.trace:
                raise ValueError("unexpected private RF trace")
            result = TRACE.read(line, lambda: self.line(deadline))
            h = result["header"]
            key = (h["token"], h["window"], h["tag"], h["status"] == 0)
            if (
                any(
                    a == key[0] or b == key[1] or (success and key[3] and tag == key[2])
                    for a, b, tag, success in self.trace_ids
                )
                or len(self.trace_ids) >= 17
            ):
                raise ValueError("duplicate or excessive RF traces")
            self.trace_ids.add(key)
            return result
        if line.startswith(b"PRESENCE_TRACE_STATUS "):
            match = re.fullmatch(
                rb"PRESENCE_TRACE_STATUS faults=(\d+) first_fault_line=(\d+) rf_evictions=(\d+) gain_unavailable_rows=(\d+) gain_ready=([01]) other_peer_frames=(\d+) static_bytes=(\d+)",
                line,
            )
            if not self.trace or not match:
                raise ValueError("RF trace status schema")
            faults, first_site, evictions, unknown, ready, other_peers, size = map(
                int, match.groups()
            )
            if faults or first_site or not 0 < size <= 131072:
                raise ValueError("RF trace lost provenance or exceeded storage")
            return dict(
                kind="rf_trace_status",
                faults=faults,
                first_fault_line=first_site,
                rf_evictions=evictions,
                gain_unavailable_rows=unknown,
                gain_ready=bool(ready),
                other_peer_frames=other_peers,
                static_bytes=size,
            )
        if line.startswith(b"PRESENCE_SNAPSHOT "):
            if not self.snapshots:
                raise ValueError("unexpected private snapshot")
            result = SNAPSHOTS.read(line, lambda: self.line(deadline))
            key = (result["header"]["kind"], result["header"]["tag"])
            if key in self.snapshot_ids:
                raise ValueError("duplicate private snapshot")
            self.snapshot_ids.add(key)
            return result
        if line.startswith(b"PRESENCE_CAPTURE_STATUS "):
            match = re.fullmatch(
                rb"PRESENCE_CAPTURE_STATUS skipped=(\d+) foreign=(\d+) invalid=(\d+) static_bytes=(\d+)",
                line,
            )
            if not self.snapshots or not match:
                raise ValueError("snapshot qualification status")
            values = list(map(int, match.groups()))
            if any(values[:3]) or not 0 < values[3] <= 32768:
                raise ValueError(
                    "snapshot contention, pairing or storage qualification failed"
                )
            return dict(
                kind="capture_status",
                skipped=values[0],
                foreign=values[1],
                invalid=values[2],
                static_bytes=values[3],
            )
        if line.startswith(b"PRESENCE_CALIBRATION "):
            match = re.fullmatch(
                rb"PRESENCE_CALIBRATION ms=(\d+) status=(\d+) action=(\d+) background_average=([0-9.]+) basis=([0-9.]+) baseline=([0-9.]+)",
                line,
            )
            if not self.snapshots or not match:
                raise ValueError("calibration detail schema")
            fields = match.groups()
            numbers = list(map(float, fields[3:]))
            if any(not math.isfinite(x) or x < 0 for x in numbers):
                raise ValueError("calibration detail numerical value")
            return dict(
                kind="calibration_detail",
                ms=int(fields[0]),
                status=int(fields[1]),
                action=int(fields[2]),
                background_average=numbers[0],
                basis=numbers[1],
                baseline=numbers[2],
            )
        if line.startswith(b"PRESENCE_SNAPSHOT_ARMED "):
            if not self.snapshots or not re.fullmatch(
                rb"PRESENCE_SNAPSHOT_ARMED tag=(?:[1-9]|1[0-6])", line
            ):
                raise ValueError("snapshot arm acknowledgement")
            return line.decode("ascii")
        if line.startswith(b"PRESENCE_PROBE "):
            return probe(line)
        if line.startswith(b"PRESENCE_SAMPLE "):
            value = sample(line)
            if self.last_ms is not None and value["ms"] <= self.last_ms:
                raise ValueError("duplicate or reversed report clock")
            self.last_ms = value["ms"]
            return value
        if line.startswith(b"PRESENCE_END ") and self.motion_case == "temporal-motion-ap-v1":
            if self.source_counts is None or self.source_counts[0] == 0:
                raise ValueError("missing admitted AP source evidence")
        if line.startswith(
            (b"PRESENCE_TRAIN_START ", b"PRESENCE_TRAIN_STOP ", b"PRESENCE_END ")
        ):
            return line.decode("ascii")
        return None
