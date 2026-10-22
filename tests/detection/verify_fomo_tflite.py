#!/usr/bin/env python3
"""Independent host screening of a public FOMO model using TensorFlow Lite.

Uses the published bare TFLite artifact, not Edge Impulse SDK code. This reports
the maximum person heatmap probability, before centroid grouping/padding-point
filtering. It is a separate reference, not a bit-equivalence claim for the SDK's
postprocessed confidence. No object-size/IoU or MCU performance is inferred.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from evaluate_alternatives import catalog, digest, fomo_pixels, samples
from report import metrics, latency


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    if digest(args.model) not in [p['bare_tflite']['model_sha256'] for p in catalog()['models']]:
        raise ValueError('TFLite artifact differs from the comparison pin')
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
    import numpy as np
    import tensorflow as tf
    args.out.mkdir(exist_ok=False)
    interpreter = tf.lite.Interpreter(model_path=str(args.model), num_threads=1,
        experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_REF)
    interpreter.allocate_tensors()
    if any(op['op_name'] == 'DELEGATE' for op in interpreter._get_ops_details()):
        raise ValueError('reference unexpectedly delegated numerical operations')
    inp, out = interpreter.get_input_details()[0], interpreter.get_output_details()[0]
    _, height, width, channels = inp['shape']
    if (inp['dtype'] != np.int8 or channels != 3 or inp['quantization'][1] != -128
            or abs(inp['quantization'][0] - 1/255) > 1e-9
            or out['dtype'] != np.int8 or out['shape'][-1] != 2):
        raise ValueError('unexpected model contract')
    labels, scores, times = [], [], []
    with (args.out / 'scores.tsv').open('x') as stream:
        for index, image, path, w, h in samples(args.dataset):
            raw = path.read_bytes()
            start = time.perf_counter_ns()
            rgb, _ = fomo_pixels(raw, w, h, width, height)
            tensor = (rgb.astype(np.int16) - 128).astype(np.int8)[None]
            interpreter.set_tensor(inp['index'], tensor)
            interpreter.invoke()
            heat = (interpreter.get_tensor(out['index']).astype(np.int16) - out['quantization'][1]) * out['quantization'][0]
            score = round(float(heat[0, :, :, 1].max()) * 1000)
            elapsed = (time.perf_counter_ns() - start) // 1000
            stream.write(f"{index}\t{image['label']}\t{score}\t{elapsed}\t0\n")
            labels.append(image['label']); scores.append(score); times.append(elapsed)
    report = {'samples': len(scores), 'primary': metrics(labels, scores, 500),
              'host_latency_us': latency(times), 'engine': 'TensorFlow Lite ' + tf.__version__ + ' BUILTIN_REF, one thread',
              'model_sha256': hashlib.sha256(args.model.read_bytes()).hexdigest(),
              'dataset_sha256': hashlib.sha256((args.dataset / 'dataset.json').read_bytes()).hexdigest(),
              'timing_scope': 'JPEG decode/resize/quantize, inference and heatmap maximum; excludes file I/O; includes first cold call',
              'geometry': 'raw_heatmap_presence',
              'limitations': ['Heatmap maximum includes padding cells and precedes SDK centroid grouping/filtering.',
                              'Scores are not asserted equivalent to postprocessed SDK confidence.',
                              'Host reference screening; no MCU latency, memory or video FPS claim.']}
    (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['primary']))


if __name__ == '__main__':
    main()
