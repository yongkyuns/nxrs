import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'detection'))
import evaluate_alternatives as reference


class AlternativeReferenceTests(unittest.TestCase):
    def test_modified_jpeg_and_manifest_cannot_relabel_frozen_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'input.jpg'; image.write_bytes(b'original frozen bytes')
            data = {'images': [{'label': 1, 'jpeg_sha256': reference.digest(image)}]}
            (root / 'dataset.json').write_text(json.dumps(data))
            (root / 'manifest.tsv').write_text(f'1\t320\t240\t{image}\n')
            pin = {'datasets': [{'dataset_sha256': reference.digest(root / 'dataset.json'), 'count': 1}]}
            with patch.object(reference, 'catalog', return_value=pin):
                self.assertEqual(len(list(reference.samples(root))), 1)
                image.write_bytes(b'changed bytes')
                with self.assertRaisesRegex(ValueError, 'sample identity'):
                    list(reference.samples(root))
                image.write_bytes(b'original frozen bytes')
                (root / 'manifest.tsv').write_text(f'0\t320\t240\t{image}\n')
                with self.assertRaisesRegex(ValueError, 'sample identity'):
                    list(reference.samples(root))

    def test_revised_dataset_and_unknown_executable_export_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dataset.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'frozen comparison'):
                list(reference.samples(root))
            (root / 'node').mkdir()
            (root / 'node/edge-impulse-standalone.js').write_text('throw Error("must not run");')
            (root / 'node/edge-impulse-standalone.wasm').write_bytes(b'unknown artifact')
            with self.assertRaisesRegex(ValueError, 'WASM artifact identity'):
                reference.Fomo(Path('/this-program-must-not-execute'), root)

    def test_non_square_camera_preserves_content_and_black_letterbox(self):
        from PIL import Image
        image = Image.new('RGB', (320, 240), (255, 0, 0))
        buffer = io.BytesIO(); image.save(buffer, 'JPEG')
        pixels, geometry = reference.fomo_pixels(buffer.getvalue(), 320, 240, 64, 64)
        self.assertEqual(geometry, (64, 48, 0, 8))
        self.assertEqual(pixels.shape, (64, 64, 3))
        self.assertEqual(int(pixels[:8].sum()), 0)
        self.assertEqual(int(pixels[56:].sum()), 0)
        self.assertGreater(int(pixels[32, 32, 0]), 250)
        with self.assertRaisesRegex(ValueError, 'JPEG shape'):
            reference.fomo_pixels(buffer.getvalue(), 240, 320, 64, 64)


if __name__ == '__main__':
    unittest.main()
