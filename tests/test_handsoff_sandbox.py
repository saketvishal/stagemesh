"""The sandbox tasks must be well posed: each independent spec fails on the scaffold and passes against a reference solution."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("handsoff_sandbox", ROOT / "scripts" / "handsoff_sandbox.py")
sandbox = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sandbox)

REFERENCE = {
    "slugify": '''import re
import unicodedata


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
''',
    "wordfreq": '''import re
from collections import Counter


def word_frequencies(text: str) -> list[tuple[str, int]]:
    counts = Counter(re.findall(r"[A-Za-z0-9']+", text.lower()))
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
''',
    "palindrome": '''def is_palindrome(text: str) -> bool:
    cleaned = [ch.lower() for ch in text if ch.isalnum()]
    return cleaned == cleaned[::-1]
''',
    "roman": '''_PAIRS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def to_roman(n: int) -> str:
    if not 1 <= n <= 3999:
        raise ValueError(n)
    out = ""
    for value, symbol in _PAIRS:
        while n >= value:
            out += symbol
            n -= value
    return out


def from_roman(s: str) -> int:
    text = s.upper()
    if not text:
        raise ValueError(s)
    total, index = 0, 0
    for value, symbol in _PAIRS:
        while text.startswith(symbol, index):
            total += value
            index += len(symbol)
    if index != len(text) or to_roman(total) != text:
        raise ValueError(s)
    return total
''',
    "duration": '''import re

_PATTERN = re.compile(r"^(?:(\\d+)d)?\\s*(?:(\\d+)h)?\\s*(?:(\\d+)m)?\\s*(?:(\\d+)s)?$")


def parse_duration(text: str) -> int:
    match = _PATTERN.match(text.strip())
    if not text.strip() or match is None or not any(match.groups()):
        raise ValueError(text)
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds
''',
    "semver": '''import re

_PATTERN = re.compile(r"^(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)(?:-([0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*))?(?:\\+[0-9A-Za-z.-]+)?$")


def _parse(version: str):
    match = _PATTERN.match(version)
    if match is None:
        raise ValueError(version)
    core = tuple(int(match.group(i)) for i in (1, 2, 3))
    pre = match.group(4).split(".") if match.group(4) else None
    return core, pre


def compare_semver(a: str, b: str) -> int:
    (core_a, pre_a), (core_b, pre_b) = _parse(a), _parse(b)
    if core_a != core_b:
        return -1 if core_a < core_b else 1
    if pre_a == pre_b:
        return 0
    if pre_a is None:
        return 1
    if pre_b is None:
        return -1
    for x, y in zip(pre_a, pre_b):
        if x == y:
            continue
        x_num, y_num = x.isdigit(), y.isdigit()
        if x_num and y_num:
            return -1 if int(x) < int(y) else 1
        if x_num != y_num:
            return -1 if x_num else 1
        return -1 if x < y else 1
    return (len(pre_a) > len(pre_b)) - (len(pre_a) < len(pre_b))
''',
    "wrap": '''def wrap_text(text: str, width: int) -> list[str]:
    if width < 1:
        raise ValueError(width)
    lines: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
''',
    "lru": '''from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError(capacity)
        self.capacity = capacity
        self._data: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key, value):
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def keys(self):
        return list(self._data)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data
''',
    "toposort": '''import heapq


def topological_sort(graph: dict[str, list[str]]) -> list[str]:
    deps = {node: set(parents) for node, parents in graph.items()}
    for parents in graph.values():
        for parent in parents:
            deps.setdefault(parent, set())
    ready = [node for node, parents in deps.items() if not parents]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for other, parents in deps.items():
            if node in parents:
                parents.discard(node)
                if not parents:
                    heapq.heappush(ready, other)
    if len(order) != len(deps):
        raise ValueError("cycle")
    return order
''',
    "csvstats": '''import csv
import io


def csv_column_stats(csv_text: str, column: str) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None or column not in reader.fieldnames:
        raise KeyError(column)
    values = []
    for row in reader:
        cell = (row.get(column) or "").strip()
        if cell:
            values.append(float(cell))
    if not values:
        raise ValueError("no numeric values")
    return {"count": len(values), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}
''',
}


def run_spec(project: Path, name: str) -> int:
    return subprocess.run([sys.executable, f"spec/check_{name}.py"], cwd=project, capture_output=True, text=True, check=False).returncode


def test_every_task_has_a_reference_and_the_specs_discriminate(tmp_path: Path) -> None:
    project = tmp_path / "sandbox"
    ids = sandbox.create_sandbox(project, sys.executable)
    assert len(ids) == 10 and {task["name"] for task in sandbox.TASKS} == set(REFERENCE)
    for task in sandbox.TASKS:
        assert run_spec(project, task["name"]) != 0, f"{task['name']} spec passes on the empty scaffold"
    for name, source in REFERENCE.items():
        (project / "src" / "textkit" / f"{name}.py").write_text(source, encoding="utf-8")
        result = subprocess.run([sys.executable, f"spec/check_{name}.py"], cwd=project, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"{name}: {result.stderr[-400:]}"


def test_contracts_forbid_editing_the_specs_and_use_absolute_python(tmp_path: Path) -> None:
    import json

    project = tmp_path / "sandbox"
    ids = sandbox.create_sandbox(project, sys.executable)
    for task_id in ids:
        contract = json.loads((project / ".stagemesh" / "contracts" / f"{task_id}.json").read_text(encoding="utf-8"))
        assert not any(path.startswith("spec/") for path in contract["allowed_files"])
        assert all(test["command"][0] == sys.executable for test in contract["required_tests"])


def test_sandbox_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        sandbox.create_sandbox(tmp_path, sys.executable)
