#!/usr/bin/env python3
"""Install the pinned Silero VAD 6.2 ONNX model for dialogue ducking."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

URL = 'https://raw.githubusercontent.com/snakers4/silero-vad/v6.2/src/silero_vad/data/silero_vad.onnx'
MODEL_SHA256 = '1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3'


def default_engine_dir():
    override = os.environ.get('FILM_SUITE_SPEECH_ENGINE_DIR')
    base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    return Path(override) if override else Path(base) / 'hermes/film-suite/engines/silero-vad'


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix='.speech-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(engine_dir, data):
    digest = hashlib.sha256(data).hexdigest()
    if digest != MODEL_SHA256:
        raise ValueError('Silero model sha256 mismatch')
    engine_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(engine_dir / 'silero_vad.onnx', data)
    record = {'schema': 'studio.speech-engine/1', 'model': 'silero_vad.onnx', 'model_sha256': digest, 'source': URL}
    atomic_write(engine_dir / 'engine.json', (json.dumps(record, indent=2) + '\n').encode('utf-8'))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-dir', type=Path, default=default_engine_dir())
    args = parser.parse_args()
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read()
    install(args.engine_dir, data)
    print('Installed model in', args.engine_dir)
    print('Dialogue ducking also requires onnxruntime in the Continuo backend Python environment.')


if __name__ == '__main__':
    main()
