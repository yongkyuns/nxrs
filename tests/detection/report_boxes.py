#!/usr/bin/env python3
"""Fixed-threshold person-box precision/recall on the frozen COCO sample.

This is not COCO mAP: one score threshold (500), IoU >= .5, max ten detections,
all non-crowd person sizes, and whole images containing person crowds excluded.
The sample was selected for balanced VWW presence and is not a full box benchmark.
"""
import argparse
import hashlib
import json
from pathlib import Path


def iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = lambda box: (box[2] - box[0]) * (box[3] - box[1])
    union = area(a) + area(b) - intersection
    return intersection / union if union > 0 else 0


def match(predictions, truth):
    """Confidence-ordered one-to-one matching; duplicates are false positives."""
    remaining = set(range(len(truth)))
    tp = 0
    for _, box in sorted(predictions, key=lambda item: item[0], reverse=True):
        index = max(remaining, key=lambda i: iou(box, truth[i]), default=None)
        if index is not None and iou(box, truth[index]) >= .5:
            tp += 1
            remaining.remove(index)
    return tp, len(predictions) - tp, len(truth) - tp


def evaluate(dataset, annotations, lines):
    images = dataset['images']
    if len(lines) != len(images):
        raise ValueError('missing or extra box rows')
    by_id = {}
    crowd = set()
    for annotation in annotations['annotations']:
        if annotation['category_id'] == 1:
            if annotation.get('iscrowd', 0):
                crowd.add(annotation['image_id'])
            else:
                by_id.setdefault(annotation['image_id'], []).append(annotation['bbox'])
    counts = [0, 0, 0]
    excluded = truncated = 0
    details = []
    for index, (image, line) in enumerate(zip(images, lines)):
        fields = line.split('\t')
        if len(fields) < 2 or int(fields[0]) != index or fields[1] not in ('0', '1') or len(fields) > 12:
            raise ValueError('invalid or reordered box row')
        predictions = []
        for field in fields[2:]:
            score, left, top, right, bottom = map(int, field.split(','))
            if not (500 <= score <= 1000 and 0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
                raise ValueError('invalid detection score or bounds')
            predictions.append((score, (left, top, right, bottom)))
        if image['id'] in crowd:
            excluded += 1
            continue
        truncated += int(fields[1])
        width, height = image['width'], image['height']
        truth = [(x / width * 1000, y / height * 1000, (x + w) / width * 1000, (y + h) / height * 1000)
                 for x, y, w, h in by_id.get(image['id'], []) if w > 0 and h > 0]
        result = match(predictions, truth)
        counts = [a + b for a, b in zip(counts, result)]
        details.append({'image_id': image['id'], 'tp': result[0], 'fp': result[1], 'fn': result[2]})
    tp, fp, fn = counts
    return {'task': 'fixed-threshold person localization', 'score_threshold_permille': 500, 'iou_threshold': .5,
            'sample_count': len(images), 'evaluated_images': len(details), 'excluded_person_crowd_images': excluded,
            'truncated_images': truncated, 'tp': tp, 'fp': fp, 'fn': fn,
            'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None,
            'f1': 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            'policy': 'all non-crowd person sizes; whole person-crowd images excluded; confidence-ordered one-to-one matching; max 10 output boxes',
            'limitations': ['Balanced VWW convenience sample, not COCO mAP or full detection benchmark.',
                            'Training membership cannot be established; no threshold tuning on this sample.',
                            'Tiny/occluded people count as misses; static boxes do not establish security alarm accuracy.'],
            'images': details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--boxes', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text())
    raw = args.annotations.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != dataset['annotations_sha256']:
        raise ValueError('annotation identity differs from the frozen dataset')
    report = evaluate(dataset, json.loads(raw), args.boxes.read_text().splitlines())
    report['annotations_sha256'] = digest
    report['boxes_sha256'] = hashlib.sha256(args.boxes.read_bytes()).hexdigest()
    with args.out.open('x') as output:
        json.dump(report, output, indent=2)
        output.write('\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'images'}))


if __name__ == '__main__':
    main()
