"""The README's examples must stay valid."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kindle_dash_server.sources.loader import parse_source

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
YAML_BLOCKS = [b for b in re.findall(r"```yaml\n(.*?)```", README, re.S) if "type:" in b]


def test_readme_has_yaml_examples():
    assert len(YAML_BLOCKS) >= 3


@pytest.mark.parametrize("block", YAML_BLOCKS, ids=lambda b: b.split("\n")[0])
def test_readme_yaml_examples_parse(block):
    parse_source(block)


def test_readme_plugin_matches_the_example_file():
    example = (ROOT / "examples" / "plugins" / "countdown.py").read_text()
    assert 'type_name = "countdown"' in example and "class CountdownConfig" in example
    assert "class Countdown(Source)" in README
