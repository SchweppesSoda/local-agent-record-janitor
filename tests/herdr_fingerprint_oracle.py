"""Compare synthetic Herdr fingerprints against the pinned Rust serializer."""
import copy
import json
import math
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile

from local_agent_record_janitor.herdr_cleanup_json import fingerprint, normalized
from tests.herdr_support import pane, snapshot, tab


def vectors():
    base = snapshot(Path("/synthetic/project"), (tab({0: pane(Path("/synthetic/project"))}),))
    values = [snapshot(Path("/synthetic/empty")), base]
    numbers = [0.0, -0.0, 0.1, 0.5, 1.0, 1e-5, 1e-6, 1e15, 1e16]
    rng = random.Random(930)
    numbers += [struct.unpack("<f", rng.getrandbits(32).to_bytes(4, "little"))[0] for _ in range(2048)]
    for number in numbers:
        if not math.isfinite(number):
            continue
        value = copy.deepcopy(base)
        value["sidebar_section_split"] = number
        value["collapsed_space_keys"] = ["z", "项目", "a", "z"]
        values.append(value)
    return values


def run():
    source = Path(__file__).parent / "herdr_oracle"
    with tempfile.TemporaryDirectory(prefix="janitor-herdr-oracle-") as temporary:
        root = Path(temporary)
        shutil.copytree(source, root / "oracle")
        values = vectors()
        input_path = root / "snapshots.json"
        input_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        command = ["cargo", "run", "--quiet", "--manifest-path", str(root / "oracle/Cargo.toml"), "--", str(input_path)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=300)
        if result.returncode:
            raise RuntimeError(result.stderr)
        actual = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(actual) == len(values)
        for index, (value, observed) in enumerate(zip(values, actual)):
            assert normalized(value) == observed["normalized"], ("projection", index)
            assert fingerprint(value) == observed["sha256"], ("fingerprint", index, value.get("sidebar_section_split"))
        print(f"Rust/Python fingerprint agreement: {len(values)} synthetic snapshots")


if __name__ == "__main__":
    run()
