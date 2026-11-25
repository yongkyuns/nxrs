"""Replay the bounded alarm protocol; never turn missing ISR entries into zero."""
HZ, PERIOD, CAPACITY, MASK = 16_000_000, 1_600, 256, (1 << 52) - 1
ADDRESS_FIELDS = {'mp_irq_probe_isr': 'isr_address', 'mp_irq_probe_now': 'read_address',
                  'mp_irq_probe_arm': 'arm_address', 'mpsc_irq_probe_entry': 'callback_address'}


def analyze(rows, metadata, expected=None):
    from metrics import integer, require, stats
    observer = [r for r in rows if r['record'] == 'irq-observer']
    phases = [r for r in rows if r['record'] == 'irq-phase']
    samples = [r for r in rows if r['record'] == 'irq-sample']
    if metadata['observer'] != 'irq':
        require(not observer and not phases and not samples and expected is None, 'unexpected IRQ observer')
        return None
    require(metadata['native'] is True and metadata['clock_hz'] == 240000000, 'IRQ target scope')
    require(len(observer) == 1, 'IRQ observer census')
    info = observer[0]
    require(info.get('scope') == 'hardware-alarm-to-probe-entry' and
            info.get('counter_hz') == HZ and info.get('target') == 1 and
            info.get('unit') == 0 and info.get('priority') == 1 and info.get('core') == 0 and
            info.get('source') == 58 and info.get('roots_internal') is True and
            info.get('cleaned_up') is True and info.get('external_clock_calibrated') is False and
            info.get('sample_capacity') == CAPACITY and
            info.get('sample_policy') == 'first-samples-plus-all-event-max', 'IRQ observer configuration/cleanup')
    require(info.get('sample_storage_bytes') == 2 * CAPACITY * 24, 'IRQ sample storage')
    integer(info.get('rust_state_bytes'), info['sample_storage_bytes'], 20 * 1024)
    cpuint = integer(info.get('cpu_interrupt'), 0, 31)
    require((1 << cpuint) & 0x637ff != 0, 'IRQ priority configuration')
    for key in ADDRESS_FIELDS.values():
        integer(info.get(key), 0x40370000, 0x403dffff)
    if expected is not None:
        require(all(info[key] == expected[key] for key in ADDRESS_FIELDS.values()), 'IRQ linked root mismatch')
    require(len(phases) == 2 and {p['phase'] for p in phases} == {'idle', 'loaded'}, 'IRQ phase census')
    require(all(s.get('phase') in ('idle', 'loaded') for s in samples), 'IRQ sample phase')
    output = {}
    for phase in phases:
        name = phase['phase']
        start = integer(phase['start'], 0, MASK)
        end = integer(phase['end'], 0, MASK)
        elapsed = (end - start) & MASK
        require(elapsed < 1 << 31 and phase['period_ticks'] == PERIOD and phase['error'] == 0,
                'IRQ observation window/protocol')
        due = elapsed // PERIOD
        observed = integer(phase['observed'], 0, due)
        missed = integer(phase['missed_periods'], 0, due)
        trailing = integer(phase['trailing_periods'], 0, due)
        stored = integer(phase['stored'], 0, CAPACITY)
        dropped = integer(phase['dropped'], 0, due)
        maximum = integer(phase['maximum_lateness_ticks'], 0, elapsed)
        require(type(phase['pending_at_stop']) is bool and
                (not phase['pending_at_stop'] or trailing > 0), 'IRQ pending accounting')
        require(observed + missed + trailing == due and stored == min(observed, CAPACITY) and
                dropped == observed - stored, 'IRQ alarm/storage accounting')
        retained = sorted([s for s in samples if s['phase'] == name], key=lambda s: s['index'])
        require(len(retained) == stored and [s['index'] for s in retained] == list(range(stored)),
                'IRQ sample census')
        next_deadline = (start + PERIOD) & MASK
        last_entered, skipped, lateness = 0, 0, []
        for sample in retained:
            deadline = integer(sample['deadline'], 0, MASK)
            entered = integer(sample['entered'], 0, MASK)
            late = integer(sample['lateness_ticks'], 0, elapsed)
            skip = integer(sample['skipped_periods'], 0, due)
            offset = (entered - start) & MASK
            require(deadline == next_deadline and late == (entered - deadline) & MASK and
                    skip == late // PERIOD and last_entered <= offset <= elapsed, 'IRQ sample cadence')
            next_deadline = (deadline + (skip + 1) * PERIOD) & MASK
            last_entered = offset
            skipped += skip
            lateness.append(late)
        require(skipped <= missed and max(lateness, default=0) <= maximum, 'IRQ sample bounds')
        if not dropped:
            require(skipped == missed and max(lateness, default=0) == maximum, 'IRQ complete sample summary')
        if not observed:
            require(maximum == 0 and missed == 0, 'IRQ unavailable maximum')
        if name == 'idle':
            require(observed > 0 and 90_000_000 <= elapsed * 1_000_000_000 // HZ <= 500_000_000,
                    'IRQ idle delivery control')
        window_ns = elapsed * 1_000_000_000 // HZ
        workload_ns = (metadata['idle_ticks'] * 1_000_000_000 // metadata['clock_hz']
                       if name == 'idle' else metadata['wall_ns'])
        require(abs(window_ns - workload_ns) <= 30_000_000, 'IRQ/workload window mismatch')
        output[name] = dict(window_ticks=elapsed, period_ticks=PERIOD, scheduled_periods=due,
                            observed=observed, missed_periods=missed, trailing_periods=trailing,
                            stored=stored, dropped=dropped, pending_at_stop=phase['pending_at_stop'],
                            maximum_serviced_lateness_ticks=maximum if observed else None,
                            retained_first_sample_lateness_ticks=stats(lateness),
                            full_sample_distribution_available=dropped == 0 and missed == 0 and trailing == 0)
    return dict(**{k: v for k, v in info.items() if k != 'record'}, phases=output,
                probe_path_placement_qualified=False, physical_timing_qualified=False,
                interpretation='normal maskable alarm deadline to first probe counter read, including masking and dispatch; '
                               'retained-first-sample percentiles and all-serviced-event maximum; '
                               'trailing deadlines have no observed ISR-entry latency')
