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
