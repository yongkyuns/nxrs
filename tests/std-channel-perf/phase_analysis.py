"""Validate a versioned setup-delay schedule without calling it load or power data."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(row, key, lower=0, upper=None):
    value = row.get(key)
    require(type(value) is int and value >= lower and (upper is None or value <= upper),
            'invalid ' + key)
    return value


def next_delay(state):
    state ^= (state << 13) & 0xffffffff
    state ^= state >> 17
    state ^= (state << 5) & 0xffffffff
    return state, state % 2000 + 1


def analyze_phase(metadata, samples, result):
    profile = result.get('release_profile')
    fields = ('pacing_requested_ticks', 'pacing_start_ticks', 'pacing_return_ticks',
              'pacing_elapsed_upper_ns')
    if profile is None:
        require('release_fixed_delay_us' not in result, 'fixed delay without release profile')
        require(not any(field in sample for sample in samples for field in fields),
                'pacing records without release profile')
        return {'profile': 'historical-unrecorded', 'phase_coverage_qualified': False}
    require(profile in ('tight-v1', 'setup-counter-delay-v1', 'setup-fixed-counter-delay-v1'),
            'unknown release profile')
    seed = integer(result, 'release_seed', 0, 0xffffffff)
    seeded = profile == 'setup-counter-delay-v1'
    fixed = profile == 'setup-fixed-counter-delay-v1'
    paced = seeded or fixed
    require(bool(seed) == seeded, 'release profile/seed mismatch')
    if fixed:
        fixed_delay_us = integer(result, 'release_fixed_delay_us', 1, 2000)
    else:
        fixed_delay_us = (integer(result, 'release_fixed_delay_us', 0, 0)
                          if 'release_fixed_delay_us' in result else 0)
    count = integer(metadata, 'samples', 1, 4096)
    warmup = integer(metadata, 'warmup', 0, 1024)
    require(len(samples) == count, 'phase sample count mismatch')
    if paced:
        require(count <= 1024 and warmup <= 128, 'phase count exceeds watchdog budget')
        require(metadata.get('counter_validated') is True, 'phase needs validated counter')
    frequency = integer(metadata, 'clock_hz', 1)
    resolution = integer(metadata, 'monotonic_resolution_ns', 1)
    duration = integer(result, 'retained_window_ns', 0, 15_000_000_000)
    require(integer(result, 'counter_read_probe_count', 1) == 64, 'unknown counter read probe')
    probe_min = integer(result, 'counter_read_min_ticks', 0, (1 << 31) - 1)
    probe_max = integer(result, 'counter_read_max_ticks', probe_min, (1 << 31) - 1)
    state = seed
    for _ in range(warmup):
        if seeded:
            state, _ = next_delay(state)
    requested_total = 0
    for sample in samples:
        requested = integer(sample, fields[0], 0, (1 << 31) - 1)
        start = integer(sample, fields[1], 0, 0xffffffff)
        end = integer(sample, fields[2], 0, 0xffffffff)
        wall_upper = integer(sample, fields[3])
        if not paced:
            require((requested, start, end, wall_upper) == (0, 0, 0, 0),
                    'tight profile contains injected pacing')
            continue
        if seeded:
            state, delay_us = next_delay(state)
        else:
            delay_us = fixed_delay_us
        expected_ticks = (frequency * delay_us + 999_999) // 1_000_000
        require(requested == expected_ticks, 'pacing differs from declared schedule')
        observed = (end - start) % (1 << 32)
        require(requested <= observed < (1 << 31), 'pacing interval short, reversed or ambiguous')
        require(0 < wall_upper and wall_upper * frequency < (1 << 31) * 1_000_000_000,
                'pacing wall bound permits counter ambiguity')
        require(observed * 1_000_000_000 <= wall_upper * frequency,
                'pacing counter exceeds wall upper bound')
        requested_total += requested
    require(requested_total * 1_000_000_000 <= (duration + 2 * resolution) * frequency,
            'retained window shorter than requested pacing')
    send_values = [(sample['send_return_ticks'] - sample['send_start_ticks']) % (1 << 32)
                   for sample in samples]
    receive_values = [(sample['receive_return_ticks'] - sample['send_start_ticks']) % (1 << 32)
                      for sample in samples]
    return {
        'profile': profile, 'seed': seed, 'fixed_delay_us': fixed_delay_us,
        'requested_pacing_ticks': requested_total,
        'retained_window_ns': duration,
        'counter_read_probe': {'count': 64, 'min_ticks': probe_min, 'max_ticks': probe_max,
                               'subtracted_from_samples': False},
        'max_send_sample_indices': [i for i, value in enumerate(send_values) if value == max(send_values)],
        'max_receive_sample_indices': [i for i, value in enumerate(receive_values) if value == max(receive_values)],
        'phase_coverage_qualified': False,
        'scope': ('Requested setup-delay diversity and window duration only; no IRQ phase mapping, '
                  'open-loop offered-load, CPU-utilization, idle or power qualification'),
    }
