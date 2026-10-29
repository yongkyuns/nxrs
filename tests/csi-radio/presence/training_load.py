"""Unlabelled recorder/training workload; never an empty/occupied evaluation."""
import importlib.util
import math
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('journal_envelope', ROOT / 'tests/csi-lab/motion_labelled.py')
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
record_event = original.record_event

class Schedule:

    def __init__(self):
        self.phase = 'settling'
        self.since = 0.0
        self.last = 0.0
        self.gain_ready = False
        self.outcome = self.pending_label = self.frozen = None
        self.counts = {}
        self.measurements = {}
        self.windows = 0
        self.progress = 0.0

    def stop(self, reason, now):
        if self.outcome is not None:
            return []
        self.outcome = reason
        self.phase = 'ending'
        return ['END']

    def confirm(self, label, now):
        raise ValueError('no human phases in unlabelled diagnostic')

    def tick(self, event, now):
        if not math.isfinite(now) or now < self.last:
            raise ValueError('workload clock')
        self.last = now
        if self.outcome:
            return []
        if now >= 110:
            return self.stop('workload_deadline', now)
        if self.phase.startswith('wait_') and now - self.since >= 5:
            return self.stop('training_ack_timeout', now)
        if isinstance(event, dict):
            kind = event.get('kind')
            if kind == 'recorded_label' and event['label'] != 0:
                return self.stop('unexpected_occupancy_label', now)
            if kind == 'recorded_motion' and (event['label'] != 0 or event['phase'] in ('background', 'holdout', 'ready')):
                return self.stop('unexpected_detector_admission', now)
            if kind == 'lab_recorder_status' and (event['losses'] or any((event[k] is not None and event[k] < 512 for k in ('rf_stack', 'gain_stack', 'compute_stack')))):
                return self.stop('recorder_loss_or_stack_margin', now)
            if kind == 'numeric_failure':
                return self.stop('native_numerical_fault', now)
            if kind == 'numerical_probe':
                if any((event[k] for k in ('bad_input', 'bad_output', 'kernel_errors', 'uninspected', 'bad_template', 'bad_current'))):
                    return self.stop('native_numerical_fault', now)
                if event['windows'] < self.windows:
                    return self.stop('numerical_counter_reversed', now)
                if event['windows'] > self.windows:
                    self.progress = now
                self.windows = event['windows']
            if kind == 'rf_trace_status':
                if event['faults']:
                    return self.stop('rf_provenance_fault', now)
                self.gain_ready = event['gain_ready']
        if isinstance(event, str):
            if event.startswith('PRESENCE_TRAIN_START '):
                if self.phase != 'wait_train_start':
                    return self.stop('unexpected_training_ack', now)
                self.phase = 'training'
                self.since = now
            elif event.startswith('PRESENCE_TRAIN_STOP '):
                if self.phase != 'wait_train_stop' or ' status=0 ' not in event:
                    return self.stop('training_stop_failed', now)
                return self.stop('unlabelled_training_load_complete', now)
        if now >= 10 and (self.windows < 10 or now - self.progress > 2):
            return self.stop('numerical_workload_stalled', now)
        if self.phase == 'settling' and now >= 10 and self.gain_ready:
            self.phase = 'wait_train_start'
            self.since = now
            return ['TRAIN_START']
        if self.phase == 'training' and now - self.since >= 60:
            self.phase = 'wait_train_stop'
            self.since = now
            return ['TRAIN_STOP']
        return []
