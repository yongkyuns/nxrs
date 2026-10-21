#!/usr/bin/env python3
"""Binary presence accuracy, uncertainty, latency, optional TFLite equivalence.

Static images cannot measure temporal alert recall, security incident detection,
false alarms/hour, night performance or object-localization AP. Keep these
classification metrics distinct from physical camera/inference throughput.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics


def wilson(successes, count):
    if not count:
        return None
    z = 1.959963984540054
    p = successes / count
    center = (p + z*z/(2*count)) / (1 + z*z/count)
    half = z * math.sqrt(p*(1-p)/count + z*z/(4*count*count)) / (1 + z*z/count)
    return [center - half, center + half]


def metrics(labels, scores, threshold):
    tp = sum(y == 1 and s >= threshold for y, s in zip(labels, scores))
    tn = sum(y == 0 and s < threshold for y, s in zip(labels, scores))
    fp = sum(y == 0 and s >= threshold for y, s in zip(labels, scores))
    fn = sum(y == 1 and s < threshold for y, s in zip(labels, scores))
    ratio = lambda n, d: n/d if d else None
    return {'threshold_permille': threshold, 'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn,
            'accuracy': ratio(tp+tn, len(labels)), 'accuracy_wilson_95': wilson(tp+tn, len(labels)),
            'precision': ratio(tp, tp+fp), 'precision_wilson_95': wilson(tp, tp+fp),
            'recall': ratio(tp, tp+fn), 'recall_wilson_95': wilson(tp, tp+fn),
            'specificity': ratio(tn, tn+fp), 'f1': ratio(2*tp, 2*tp+fp+fn)}


def latency(values):
    values = sorted(values)
    return {'count': len(values), 'mean_us': statistics.mean(values), 'median_us': statistics.median(values),
            'p95_us': values[math.ceil(len(values)*0.95)-1], 'max_us': max(values)}


def reference_agreement(scores, reference):
    """Bound the floating-point runtime's deviation from integer TFLite.

    This is an approximation check, not a claim of bit-exact TFLite kernels.
    Limits are explicit so subsequent model/runtime changes cannot silently
    accept a large semantic drift such as the original SAME-padding error.
    """
    if not scores or len(scores) != len(reference):
        raise ValueError('missing reference scores')
    differences = [abs(a-b) for a, b in zip(scores, reference)]
    disagreements = sum((a >= 500) != (b >= 500) for a, b in zip(scores, reference))
    return {'mean_absolute_score_difference_permille': statistics.mean(differences),
            'max_absolute_score_difference_permille': max(differences),
            'classification_disagreements_at_500': disagreements,
            'limits': {'max_score_difference_permille': 32, 'max_disagreement_fraction': .01},
            'passed': max(differences) <= 32 and disagreements / len(scores) <= .01}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--scores', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--reference-pixels', type=Path)
    parser.add_argument('--model', type=Path)
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text())
    rows = [[int(v) for v in line.split('\t')] for line in args.scores.read_text().splitlines()]
    if len(rows) != len(data['images']) or not rows:
        raise ValueError('missing or extra evaluation rows')
    labels, scores, times, preprocessing = [], [], [], []
    for index, (row, image) in enumerate(zip(rows, data['images'])):
        if (len(row) != 5 or row[0] != index or row[1] != image['label'] or row[1] not in (0, 1)
                or not 0 <= row[2] <= 1000 or row[4] < 0 or row[3] <= 0 or row[3] < row[4]):
            raise ValueError('invalid or reordered evaluation row')
        if 'largest_person_fraction' in image and image['label'] != int(image['largest_person_fraction'] > 0.005):
            raise ValueError('dataset label disagrees with the official VWW area rule')
        labels.append(row[1]); scores.append(row[2]); times.append(row[3]); preprocessing.append(row[4])
    positive = [s for y, s in zip(labels, scores) if y]
    negative = [s for y, s in zip(labels, scores) if not y]
    if not positive or not negative:
        raise ValueError('evaluation requires both positive and negative images')
    auc = sum((p > n) + 0.5*(p == n) for p in positive for n in negative)/(len(positive)*len(negative))
    report = {'task': 'person_presence', 'dataset': data['dataset'], 'sample_count': len(rows), 'positive': sum(labels),
              'dataset_annotations_sha256': data['annotations_sha256'], 'selection': data['selection'],
              'host': {'machine': platform.machine(), 'system': platform.system()},
              'thresholds': [metrics(labels, scores, t) for t in (500, 700)], 'roc_auc': auc,
              'end_to_end_latency': latency(times), 'preprocess_latency': latency(preprocessing),
              'inference_latency': latency([a-b for a, b in zip(times, preprocessing)]),
              'limitations': ['Balanced convenience sample, not full benchmark qualification or deployment prevalence.',
                             'Pretrained model training membership cannot be independently established.',
                             'Static-image presence classification is not box localization or temporal-event accuracy.',
                             'Host latency does not establish MCU latency, memory or video FPS.']}
    if args.reference_pixels:
        if not args.model:
            parser.error('--model is required with --reference-pixels')
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
        import numpy as np
        import tensorflow as tf
        interpreter = tf.lite.Interpreter(model_path=str(args.model), num_threads=1)
        interpreter.allocate_tensors()
        input_info = interpreter.get_input_details()[0]
        output_info = interpreter.get_output_details()[0]
        shape = tuple(input_info['shape'])
        input_scale, input_zero = input_info['quantization']
        if (shape != (1, 96, 96, 3) or input_info['dtype'] != np.int8
                or input_zero != -128 or not math.isclose(input_scale, 1/255, rel_tol=1e-6)
                or tuple(output_info['shape']) != (1, 2) or output_info['dtype'] != np.int8):
            raise ValueError('reference input ABI mismatch')
        reference = []
        for index in range(len(rows)):
            pixels = (args.reference_pixels / f"{index}.rgb").read_bytes()
            if len(pixels) != 96 * 96 * 3:
                raise ValueError('invalid reference pixels')
            signed = (np.frombuffer(pixels, dtype=np.uint8).astype(np.int16)-128).astype(np.int8).reshape(shape)
            interpreter.set_tensor(input_info['index'], signed)
            interpreter.invoke()
            raw = interpreter.get_tensor(output_info['index'])[0, 1]
            scale, zero = output_info['quantization']
            reference.append(round((int(raw)-zero)*scale*1000))
        report['reference'] = {'engine': f'TensorFlow Lite {tf.__version__}', 'same_rust_preprocessed_pixels': True,
                               'model_sha256': hashlib.sha256(args.model.read_bytes()).hexdigest(),
                               'input_scale': input_scale, 'input_zero_point': input_zero,
                               **reference_agreement(scores, reference),
                               'thresholds': [metrics(labels, reference, t) for t in (500, 700)],
                               'scores_permille': reference}
    with args.out.open('x') as output:
        json.dump(report, output, indent=2); output.write('\n')
    print(json.dumps({k: v for k, v in report.items() if k in ('sample_count','thresholds','roc_auc','end_to_end_latency')}))
    if 'reference' in report and not report['reference']['passed']:
        raise RuntimeError('runtime/reference approximation limits exceeded; report saved')


if __name__ == '__main__':
    main()
