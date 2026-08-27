import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def dependency_lines(filename):
    return [
        line.strip() for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r "))
    ]


def test_direct_python_dependencies_use_exact_versions():
    lines = dependency_lines("requirements.txt")
    assert lines
    assert all("==" in line for line in lines)


def test_live_sdks_are_pinned_to_release_or_commit():
    lines = dependency_lines("requirements-live.txt")
    assert any(line == "hyperliquid-python-sdk==0.24.0" for line in lines)
    assert any(line == "eth-account==0.13.7" for line in lines)
    lighter = next(line for line in lines if line.startswith("lighter-sdk @"))
    assert re.search(r"lighter-python\.git@[0-9a-f]{40}$", lighter)
