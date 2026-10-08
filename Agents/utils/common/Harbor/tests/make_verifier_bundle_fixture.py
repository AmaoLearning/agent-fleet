"""A structurally valid, non-executable static bundle for shell command tests."""
import io
import json
import struct
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from Agents.utils.common.Harbor.verifier_runtime import (  # noqa: E402
    python_runtime,
    swe_rebench_v2_bundle_preparer,
)


def build_fixture(output):
    with tempfile.TemporaryDirectory() as directory:
        runtime = Path(directory) / 'runtime.tar.gz'
        elf = bytearray(120)
        elf[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', elf, 18, 62)
        struct.pack_into('<Q', elf, 32, 64)
        struct.pack_into('<HH', elf, 54, 56, 1)
        payloads = {
            'bin/python3.12': python_runtime.STATIC_WRAPPER.encode(),
            'bin/python3.12.real': bytes(elf),
            'static-runtime.json': json.dumps(python_runtime.static_manifest()).encode(),
            'lib/python3.12/encodings/__init__.py': b'# fixture\n',
            'lib/python3.12/json/__init__.py': b'# fixture\n',
            'lib/python3.12/xml/etree/ElementTree.py': b'# fixture\n',
        }
        with tarfile.open(runtime, 'w:gz') as archive:
            for relative, data in payloads.items():
                member = tarfile.TarInfo(f'{python_runtime.RUNTIME_ROOT}/{relative}')
                member.size = len(data)
                member.mode = 0o755 if relative.startswith('bin/') else 0o644
                archive.addfile(member, io.BytesIO(data))
        swe_rebench_v2_bundle_preparer.build(runtime, Path(output))
        if not swe_rebench_v2_bundle_preparer.archive_ready(Path(output)):
            raise RuntimeError('invalid shell test fixture')


if __name__ == '__main__':
    build_fixture(sys.argv[1])
