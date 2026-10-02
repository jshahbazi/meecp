"""Exercise upstream crash regressions when the optional build group is installed."""

import importlib.util
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("torch") is None,
    reason="requires the optional build dependency group",
)


CASES = {
    "script_bare_list": """
from typing import List

try:
    @torch.jit.script
    class Invalid:
        def __init__(self):
            self.count: int = 0
            self.items: list = []
except RuntimeError:
    pass
else:
    raise AssertionError("invalid annotation accepted")

@torch.jit.script
class Valid:
    def __init__(self):
        self.items: List[int] = []

assert Valid().items == []
""",
    "lstm_cell_bad_weights": """
try:
    torch.lstm_cell(
        torch.zeros(0, 8), (torch.zeros(0, 9), torch.zeros(0, 9)),
        torch.full((1, 8), 1.251e12), torch.full((1, 9), 1.4013e-45), None, None,
    )
except RuntimeError:
    pass
else:
    raise AssertionError("invalid weights accepted")

hidden, cell = torch.lstm_cell(
    torch.zeros(2, 8), (torch.zeros(2, 9), torch.zeros(2, 9)),
    torch.zeros(36, 8), torch.zeros(36, 9), None, None,
)
assert hidden.shape == cell.shape == (2, 9)
""",
    "unpack_empty_sequence": """
from torch.nn.utils.rnn import PackedSequence, pack_sequence, unpack_sequence

try:
    unpack_sequence(PackedSequence(torch.tensor([]), torch.tensor([], dtype=torch.int64)))
except RuntimeError:
    pass
else:
    raise AssertionError("empty packed sequence accepted")

sequences = [torch.ones(3, 2), torch.ones(1, 2)]
restored = unpack_sequence(pack_sequence(sequences))
assert len(restored) == len(sequences)
assert all(torch.equal(actual, expected) for actual, expected in zip(restored, sequences))
""",
    "pad_empty_sequence": """
from torch.nn.utils.rnn import PackedSequence, pack_sequence, pad_packed_sequence

try:
    pad_packed_sequence(
        PackedSequence(torch.randn(0, 5), torch.tensor([], dtype=torch.int64)),
        batch_first=True,
    )
except RuntimeError:
    pass
else:
    raise AssertionError("empty packed sequence accepted")

padded, lengths = pad_packed_sequence(
    pack_sequence([torch.ones(3, 2), torch.ones(1, 2)]), batch_first=True,
)
assert padded.shape == (2, 3, 2)
assert lengths.tolist() == [3, 1]
""",
}


@pytest.mark.parametrize("case", CASES)
def test_torch_rejects_crash_inputs_and_preserves_valid_inputs(case, tmp_path):
    # A regression must fail one test instead of crashing the pytest process.
    script = tmp_path / f"{case}.py"
    script.write_text(
        "import resource\nresource.setrlimit(resource.RLIMIT_CORE, (0, 0))\nimport torch\n"
        + CASES[case]
    )
    result = subprocess.run(
        [sys.executable, "-P", str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"{case} failed with exit code {result.returncode}"
