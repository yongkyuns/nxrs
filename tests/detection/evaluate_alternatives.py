#!/usr/bin/env python3
"""Host-only reference screening of pinned pretrained alternatives.

No production HAL/provider or device firmware is changed. NanoDet uses its
upstream validation pipeline and decoder with strictly loaded weights. FOMO
uses the actual exported Edge Impulse WASM numerical/DSP/postprocessing engine.
All inputs must match the existing frozen camera-JPEG manifests.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def catalog():
    return json.loads(Path(__file__).with_name('alternatives.json').read_text())


def samples(directory):
    pin = next((p for p in catalog()['datasets'] if p['dataset_sha256'] == digest(directory / 'dataset.json')), None)
    if pin is None:
        raise ValueError('dataset differs from the frozen comparison')
    data = json.loads((directory / 'dataset.json').read_text())
    rows = (directory / 'manifest.tsv').read_text().splitlines()
    if len(rows) != len(data['images']) or len(rows) != pin['count']:
        raise ValueError('manifest length')
    for index, (image, row) in enumerate(zip(data['images'], rows)):
        label, width, height, filename = row.split('\t')
        path = Path(filename)
        if (int(label) not in (0, 1) or int(label) != image['label'] or int(width) != 320 or int(height) != 240
                or digest(path) != image['jpeg_sha256']):
            raise ValueError('frozen sample identity')
        yield index, image, path, int(width), int(height)


def fomo_pixels(raw, width, height, target_width, target_height):
    from PIL import Image
    import io
    import numpy as np
    image = Image.open(io.BytesIO(raw)).convert('RGB')
    if image.size != (width, height):
        raise ValueError('JPEG shape')
    scale = min(target_width / width, target_height / height)
    w, h = round(width * scale), round(height * scale)
    dx, dy = (target_width - w) // 2, (target_height - h) // 2
    canvas = Image.new('RGB', (target_width, target_height))
    canvas.paste(image.resize((w, h), Image.Resampling.LANCZOS), (dx, dy))
    return np.asarray(canvas), (w, h, dx, dy)


class NanoDet:
    def __init__(self, source, checkpoint):
        pin = catalog()['nanodet']
        commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        if (commit != pin['source_commit'] or digest(checkpoint) != pin['checkpoint_sha256']
                or digest(source / pin['config']) != pin['config_sha256']):
            raise ValueError('NanoDet artifact identity')
        subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', 'HEAD', '--', 'nanodet', 'config'], check=True)
        sys.path.insert(0, str(source))
        import torch
        from nanodet.data.transform import Pipeline
        from nanodet.model.arch import build_model
        from nanodet.util import cfg, load_config
        import cv2
        self.torch, self.cv = torch, cv2
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
        self.config = source / 'config/nanodet-m-0.5x.yml'
        load_config(cfg, str(self.config))
        cfg.defrost()
        cfg.model.arch.backbone.pretrain = False
        cfg.freeze()
        self.model = build_model(cfg.model).eval()
        # Old Lightning checkpoints store a callback class as a metadata key.
        # Permit an inert key only; never unpickle executable checkpoint code.
        class CallbackKey:
            pass
        with torch.serialization.safe_globals([
            (CallbackKey, 'pytorch_lightning.callbacks.model_checkpoint.ModelCheckpoint')
        ]):
            state = torch.load(checkpoint, map_location='cpu', weights_only=True)['state_dict']
        first = next(iter(state))
        prefix = next((p for p in ('model.', 'module.') if first.startswith(p)), '')
        if prefix:
            state = {k[len(prefix):]: v for k, v in state.items()}
        self.model.load_state_dict(state, strict=True)
        self.pipeline = Pipeline(cfg.data.val.pipeline, cfg.data.val.keep_ratio)
        self.size = cfg.data.val.input_size
        self.provenance = {'engine': 'upstream NanoDet / PyTorch CPU, one thread',
                           'torch_version': torch.__version__, 'model_sha256': digest(checkpoint),
                           'source_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                           'config_sha256': digest(self.config), 'parameters': sum(p.numel() for p in self.model.parameters()),
                           'geometry': 'bounding_boxes', 'threshold': 0.5,
                           'timing_scope': 'JPEG decode, upstream validation preprocessing, numerical execution and upstream postprocessing; excludes file read'}
        with torch.inference_mode():
            self.model(torch.zeros(1, 3, 320, 320))

    def detect(self, raw, width, height):
        import numpy as np
        start = time.perf_counter_ns()
        image = self.cv.imdecode(np.frombuffer(raw, np.uint8), self.cv.IMREAD_COLOR)
        if image is None or image.shape[:2] != (height, width):
            raise ValueError('JPEG shape')
        meta = {'img_info': {'id': 0, 'height': height, 'width': width}, 'raw_img': image, 'img': image}
        meta = self.pipeline(meta, self.size)
        meta['img'] = self.torch.from_numpy(meta['img'].transpose(2, 0, 1)).unsqueeze(0)
        prepared = time.perf_counter_ns()
        with self.torch.inference_mode():
            predictions = self.model(meta['img'])
            score = max(float(level[0, 0].sigmoid().max()) for level in predictions[0])
            objects = self.model.head.post_process(predictions, meta)[0][0]
        elapsed = (time.perf_counter_ns() - start) // 1000
        boxes = []
        for left, top, right, bottom, confidence in objects:
            if confidence >= .5:
                box = [round(left / width * 1000), round(top / height * 1000),
                       round(right / width * 1000), round(bottom / height * 1000)]
                box = [min(1000, max(0, value)) for value in box]
                if box[0] < box[2] and box[1] < box[3]:
                    boxes.append([round(confidence * 1000), *box])
        boxes.sort(reverse=True)
        return round(score * 1000), elapsed, (prepared - start) // 1000, boxes[:10], len(boxes) > 10


class Fomo:
    def __init__(self, node, export):
        wasm, loader = digest(export / 'node/edge-impulse-standalone.wasm'), digest(export / 'node/edge-impulse-standalone.js')
        pin = next(((model, artifact) for model in catalog()['models'] for artifact in model['wasm_exports']
                    if artifact['wasm_sha256'] == wasm and artifact['loader_sha256'] == loader), None)
        if pin is None:
            raise ValueError('unqualified WASM artifact identity')
        self.child = subprocess.Popen([str(node), str(Path(__file__).with_name('fomo_reference.cjs')), str(export)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.provenance = self.read()
        if self.provenance.get('type') != 'ready':
            raise ValueError('WASM initialization')
        model, artifact = pin
        if (self.provenance['project']['id'] != model['public_snapshot_id']
                or self.provenance['project']['deploy_version'] != artifact['deployment_version']):
            self.close()
            raise ValueError('WASM project identity')
        self.width = self.provenance['properties']['image_input_width']
        self.height = self.provenance['properties']['image_input_height']
        if (self.width, self.height) not in [(64, 64), (96, 96)]:
            raise ValueError('unexpected public model shape')
        self.provenance.update({'geometry': 'centroids', 'threshold': 0.5,
            'variant': artifact['variant'], 'project_url': model['project_url'],
            'engine': 'actual Edge Impulse exported WASM / Node CPU',
            'wasm_sha256': digest(export / 'node/edge-impulse-standalone.wasm'),
            'loader_sha256': digest(export / 'node/edge-impulse-standalone.js'),
            'resize': 'public project fit-long, centered black letterbox, Lanczos3 via Pillow',
            'timing_scope': 'JPEG decode and resize/packing in Python plus engine DSP/numerical/postprocess; excludes file/IPC/base64 I/O'})

    def read(self):
        line = self.child.stdout.readline(65537)
        if not line or len(line) > 65536:
            raise ValueError('bounded WASM reply')
        result = json.loads(line)
        if result.get('type') == 'error':
            raise ValueError(result['error'])
        return result

    def detect(self, raw, width, height):
        import numpy as np
        start = time.perf_counter_ns()
        rgb, (w, h, dx, dy) = fomo_pixels(raw, width, height, self.width, self.height)
        rgb = rgb.astype(np.uint32)
        packed = ((rgb[:, :, 0] << 16) | (rgb[:, :, 1] << 8) | rgb[:, :, 2]).astype('<u4').tobytes()
        prep = (time.perf_counter_ns() - start) // 1000
        self.child.stdin.write(json.dumps({'pixels': base64.b64encode(packed).decode()}) + '\n')
        self.child.stdin.flush()
        result = self.read()
        points = []
        for obj in result['objects']:
            # FOMO's activation rectangles locate centroids; never score them as
            # full person bounding boxes against IoU ground truth.
            x = (obj['x'] + obj['width'] / 2 - dx) / w
            y = (obj['y'] + obj['height'] / 2 - dy) / h
            if 0 <= x <= 1 and 0 <= y <= 1:
                points.append([round(obj['value'] * 1000), round(x * 1000), round(y * 1000)])
        score = max((p[0] for p in points), default=0)
        points.sort(reverse=True)
        return score, prep + result['engine_us'], prep, points[:10], len(points) > 10

    def close(self):
        self.child.stdin.close()
        try:
            if self.child.wait(timeout=5) != 0:
                raise ValueError('WASM worker failure')
        finally:
            if self.child.poll() is None:
                self.child.kill()
                self.child.wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kind', choices=['nanodet', 'fomo'], required=True)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--source', type=Path)
    p.add_argument('--checkpoint', type=Path)
    p.add_argument('--node', type=Path)
    p.add_argument('--export', type=Path)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    model = NanoDet(args.source, args.checkpoint) if args.kind == 'nanodet' else Fomo(args.node, args.export)
    try:
        rows = list(samples(args.dataset))
        model.provenance['dataset_sha256'] = digest(args.dataset / 'dataset.json')
        model.provenance['samples'] = len(rows)
        if not rows:
            raise ValueError('empty dataset')
        _, _, first, width, height = rows[0]
        for _ in range(2):
            model.detect(first.read_bytes(), width, height)
        model.provenance['warm_up_calls'] = 2
        (args.out / 'provenance.json').write_text(json.dumps(model.provenance, indent=2) + '\n')
        geometry = 'boxes.tsv' if args.kind == 'nanodet' else 'centroids.tsv'
        with (args.out / 'scores.tsv').open('x') as scores, (args.out / geometry).open('x') as objects:
            for index, image, path, width, height in rows:
                score, elapsed, prep, found, truncated = model.detect(path.read_bytes(), width, height)
                scores.write(f"{index}\t{image['label']}\t{score}\t{elapsed}\t{prep}\n")
                objects.write('\t'.join([str(index), str(int(truncated))] + [','.join(map(str, box)) for box in found]) + '\n')
                if (index + 1) % 100 == 0:
                    scores.flush(); objects.flush()
                    print(json.dumps({'completed': index + 1, 'kind': args.kind}), flush=True)
    finally:
        if hasattr(model, 'close'):
            model.close()


if __name__ == '__main__':
    main()
