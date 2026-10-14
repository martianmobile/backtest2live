from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def root():
    return ROOT


@pytest.fixture
def write_csv(tmp_path):
    def _write(text, name="results.csv"):
        p = tmp_path / name
        p.write_text(text.strip() + "\n", encoding="utf-8")
        return str(p)

    return _write


T = 1_711_756_800_000  # 2024-03-30T00:00:00Z in ms; synthetic fixtures use offsets from it


def abs_ms(text):
    """Turn leading 'ts' offsets in a CSV block into epoch ms."""
    out = []
    for line in text.strip().splitlines():
        cells = line.split(",")
        if cells[0].isdigit():
            cells[0] = str(T + int(cells[0]))
        out.append(",".join(cells))
    return "\n".join(out)
