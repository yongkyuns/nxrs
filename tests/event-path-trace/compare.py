#!/usr/bin/env python3
"""Compare one matched three-arm pass; never pool captures or subtract tails."""
import argparse
import json
from pathlib import Path

import capture


def compare(builds, runs):
    records = [capture.receipt(path) for path in builds]
    summaries = [capture.replay(run,build) for run,build in zip(runs,builds)]
    require = capture.require
    require([(s['mode'],s['recording']) for s in summaries] ==
            [('baseline',False),('trace',False),('trace',True)], 'requires baseline-quiet, trace-quiet, trace-record')
    for key in ('transport','policy','samples','warmup','clock_hz_configuration'):
        require(all(s[key]==summaries[0][key] for s in summaries), 'mixed workload: '+key)
    for key in ('source_sha256','kernel_revision','apps_revision','compiler_identity',
                'std_array_sha256','std_library_tree_sha256','crossbeam','build_environment'):
        require(all(r[key]==records[0][key] for r in records), 'mixed build input: '+key)
    require(records[1]['build_id']==records[2]['build_id'], 'instrumented arms must use one exact image')
    configs = [capture.builder.p2.baseline.config_values(
        capture.p2.checked_file(root,record['config_file'])) for root,record in zip(builds,records)]
    names = set().union(*configs)
    differences = {name:[c.get(name) for c in configs] for name in sorted(names)
                   if len({c.get(name) for c in configs})>1}
    # Enabling the parent also makes these two disabled note drivers visible.
    # Absence versus explicit n is equivalent; enabling either is not admitted.
    require(all(name.startswith(('CONFIG_SCHED_INSTRUMENTATION','CONFIG_DRIVERS_NOTE')) or
                (name in {'CONFIG_NOTE_RTT','CONFIG_SEGGER_SYSVIEW'} and
                 all(value in (None,'n') for value in values))
                for name,values in differences.items()), 'unrelated configuration differences')
    return {'schema':'event-path-trace-comparison-v1','matched':True,'pooled':False,
            'arms':summaries,'configuration_differences':differences,
            'perturbation_quantile_ratios':{
                metric:{quantile:{'hooks_over_baseline':summaries[1][metric][quantile]/summaries[0][metric][quantile],
                                  'record_over_hooks':summaries[2][metric][quantile]/summaries[1][metric][quantile]}
                        for quantile in ('p50','p95','p99','max')}
                for metric in ('admission_ticks','delivery_ticks')},
            'interpretation':'Ratios compare matched elapsed distributions, not additive stages, CPU time or pure switch cost.',
            'physical_timing_qualified':False,'priority_progress_qualified':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-build',type=Path,required=True)
    parser.add_argument('--trace-build',type=Path,required=True)
    parser.add_argument('runs',nargs=3,type=Path,help='baseline quiet, trace quiet, trace record (one pass/case)')
    args=parser.parse_args()
    print(json.dumps(compare([args.baseline_build,args.trace_build,args.trace_build],args.runs),indent=2))
