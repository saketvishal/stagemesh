"""Create the isolated development project used for the first real unattended StageMesh tasks.

The project is a tiny stdlib-only Python package (`textkit`) in its own git repository. Ten tasks ask a coding provider to add
one function each. Every task has an independent oracle, `spec/check_<name>.py`, that the provider is NOT allowed to edit, so a
task is only DONE when real code passes checks the provider did not write. Nothing here touches any other project.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TASKS: list[dict[str, str]] = [
    {
        "name": "slugify",
        "title": "Add textkit.slugify",
        "objective": "Add `slugify(text: str) -> str` in src/textkit/slugify.py: lowercase ASCII letters and digits only, every other run of characters becomes one '-', no leading or trailing '-'. Non-ASCII letters are dropped after unicode normalisation (NFKD), so 'Crème Brûlée' becomes 'creme-brulee'. Empty or symbol-only input returns ''.",
        "spec": '''from textkit.slugify import slugify
assert slugify("Hello, World!") == "hello-world"
assert slugify("  Many   spaces--and__symbols ") == "many-spaces-and-symbols"
assert slugify("Crème Brûlée") == "creme-brulee"
assert slugify("") == "" and slugify("!!!") == ""
assert slugify("A1 b2") == "a1-b2"
''',
    },
    {
        "name": "wordfreq",
        "title": "Add textkit.word_frequencies",
        "objective": "Add `word_frequencies(text: str) -> list[tuple[str, int]]` in src/textkit/wordfreq.py. Words are runs of letters, digits or apostrophes, compared case-insensitively. Return (word, count) sorted by count descending, then word ascending.",
        "spec": '''from textkit.wordfreq import word_frequencies
assert word_frequencies("the cat The dog") == [("the", 2), ("cat", 1), ("dog", 1)]
assert word_frequencies("Don't stop, don't") == [("don't", 2), ("stop", 1)]
assert word_frequencies("") == []
assert word_frequencies("b a b a c") == [("a", 2), ("b", 2), ("c", 1)]
''',
    },
    {
        "name": "palindrome",
        "title": "Add textkit.is_palindrome",
        "objective": "Add `is_palindrome(text: str) -> bool` in src/textkit/palindrome.py. Ignore case and every non-alphanumeric character. The empty string and strings with no alphanumerics are palindromes.",
        "spec": '''from textkit.palindrome import is_palindrome
assert is_palindrome("A man, a plan, a canal: Panama") is True
assert is_palindrome("race a car") is False
assert is_palindrome("") is True and is_palindrome("?!") is True
assert is_palindrome("No 'x' in Nixon") is True
''',
    },
    {
        "name": "roman",
        "title": "Add textkit.to_roman and from_roman",
        "objective": "Add `to_roman(n: int) -> str` and `from_roman(s: str) -> int` in src/textkit/roman.py for 1..3999 using standard subtractive notation. to_roman raises ValueError outside 1..3999; from_roman accepts upper or lower case and raises ValueError for anything that is not the canonical numeral (for example 'IIII', 'VX', '').",
        "spec": '''from textkit.roman import from_roman, to_roman
assert to_roman(1994) == "MCMXCIV" and to_roman(4) == "IV" and to_roman(3999) == "MMMCMXCIX"
assert from_roman("mcmxciv") == 1994 and from_roman("XLII") == 42
for n in (1, 9, 14, 40, 90, 400, 2024, 3888):
    assert from_roman(to_roman(n)) == n
for bad in (0, 4000, -3):
    try:
        to_roman(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
for bad in ("IIII", "VX", "", "ABC", "IC"):
    try:
        from_roman(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
''',
    },
    {
        "name": "duration",
        "title": "Add textkit.parse_duration",
        "objective": "Add `parse_duration(text: str) -> int` in src/textkit/duration.py returning whole seconds for strings built from d, h, m and s parts in that order, such as '1d2h3m4s', '90m', '45s', or '1h 30m' (spaces allowed between parts). Each unit may appear at most once. Raise ValueError for empty input, unknown units, repeated or out-of-order units, or a number without a unit.",
        "spec": '''from textkit.duration import parse_duration
assert parse_duration("45s") == 45 and parse_duration("90m") == 5400
assert parse_duration("1d2h3m4s") == 86400 + 7200 + 180 + 4
assert parse_duration("1h 30m") == 5400
for bad in ("", "5", "5x", "1m1h", "1h1h", "h", "1.5h", "-3s"):
    try:
        parse_duration(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
''',
    },
    {
        "name": "semver",
        "title": "Add textkit.compare_semver",
        "objective": "Add `compare_semver(a: str, b: str) -> int` in src/textkit/semver.py returning -1, 0 or 1 under semantic-versioning precedence: compare major.minor.patch numerically; a version with a pre-release (after '-') is lower than the same version without one; pre-release identifiers compare left to right, numeric identifiers numerically and lower than alphanumeric ones, alphanumeric ones by ASCII order, and a shorter list is lower when all earlier identifiers are equal. Ignore build metadata after '+'. Raise ValueError for strings that are not of the form MAJOR.MINOR.PATCH with optional -prerelease and +build.",
        "spec": '''from textkit.semver import compare_semver as c
assert c("1.2.3", "1.2.3") == 0 and c("1.2.3+build5", "1.2.3+other") == 0
assert c("1.2.3", "1.10.0") == -1 and c("2.0.0", "1.99.99") == 1
assert c("1.0.0-alpha", "1.0.0") == -1
chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
for low, high in zip(chain, chain[1:]):
    assert c(low, high) == -1 and c(high, low) == 1, (low, high)
for bad in ("1.2", "1.2.x", "", "1.2.3-", "01.2.3", "1.2.3.4"):
    try:
        c(bad, "1.0.0")
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
''',
    },
    {
        "name": "wrap",
        "title": "Add textkit.wrap_text",
        "objective": "Add `wrap_text(text: str, width: int) -> list[str]` in src/textkit/wrap.py. Greedy word wrap: words are separated by any whitespace, joined by single spaces, no line longer than width. A word longer than width is split into width-sized pieces. Empty input returns []. Raise ValueError when width < 1.",
        "spec": '''from textkit.wrap import wrap_text
assert wrap_text("the quick brown fox", 9) == ["the quick", "brown fox"]
assert wrap_text("a  b\\n c", 10) == ["a b c"]
assert wrap_text("abcdefghij", 4) == ["abcd", "efgh", "ij"]
assert wrap_text("hi abcdefghij", 4) == ["hi", "abcd", "efgh", "ij"]
assert wrap_text("", 5) == [] and wrap_text("   ", 5) == []
assert all(len(line) <= 7 for line in wrap_text("lorem ipsum dolor sit amet consectetur", 7))
try:
    wrap_text("x", 0)
except ValueError:
    pass
else:
    raise AssertionError("width")
''',
    },
    {
        "name": "lru",
        "title": "Add textkit.LRUCache",
        "objective": "Add class `LRUCache` in src/textkit/lru.py: `LRUCache(capacity: int)`, `get(key, default=None)`, `put(key, value)`, `__len__`, `__contains__`, and `keys()` returning keys from least to most recently used. get and put both mark a key most recently used; `__contains__` does not. Putting beyond capacity evicts the least recently used key. capacity < 1 raises ValueError.",
        "spec": '''from textkit.lru import LRUCache
c = LRUCache(2)
c.put("a", 1); c.put("b", 2)
assert c.get("a") == 1 and c.keys() == ["b", "a"]
c.put("c", 3)
assert "b" not in c and "a" in c and len(c) == 2 and c.keys() == ["a", "c"]
assert c.get("zzz", 42) == 42
c.put("a", 10)
assert c.get("a") == 10 and c.keys() == ["c", "a"]
assert ("c" in c) and c.keys() == ["c", "a"]
try:
    LRUCache(0)
except ValueError:
    pass
else:
    raise AssertionError("capacity")
''',
    },
    {
        "name": "toposort",
        "title": "Add textkit.topological_sort",
        "objective": "Add `topological_sort(graph: dict[str, list[str]]) -> list[str]` in src/textkit/toposort.py. graph maps a node to the nodes it depends on; dependencies come first in the result. Nodes that appear only as dependencies are included. Among nodes that are ready at the same time, choose alphabetically so the result is deterministic. Raise ValueError('cycle') when there is a cycle.",
        "spec": '''from textkit.toposort import topological_sort as t
assert t({"app": ["lib", "util"], "lib": ["util"], "util": []}) == ["util", "lib", "app"]
assert t({"b": [], "a": []}) == ["a", "b"]
assert t({"x": ["y"]}) == ["y", "x"]
assert t({}) == []
assert t({"d": ["b", "c"], "b": ["a"], "c": ["a"]}) == ["a", "b", "c", "d"]
try:
    t({"a": ["b"], "b": ["a"]})
except ValueError as exc:
    assert "cycle" in str(exc)
else:
    raise AssertionError("cycle")
''',
    },
    {
        "name": "csvstats",
        "title": "Add textkit.csv_column_stats",
        "objective": "Add `csv_column_stats(csv_text: str, column: str) -> dict` in src/textkit/csvstats.py using only the csv module. The first row is the header. Return {'count': int, 'min': float, 'max': float, 'mean': float} over the numeric values of the named column; blank cells are skipped. Raise KeyError if the column does not exist, and ValueError if a non-blank cell is not a number or there are no numeric values.",
        "spec": '''from textkit.csvstats import csv_column_stats as s
data = "name,score\\nann,3\\nbob,\\ncy,4.5\\ndee,1\\n"
r = s(data, "score")
assert r == {"count": 3, "min": 1.0, "max": 4.5, "mean": (3 + 4.5 + 1) / 3}, r
assert s('a,b\\n"1,5",2\\n', "b")["count"] == 1
for text, column, exc in (("a\\n1\\n", "zzz", KeyError), ("a\\nx\\n", "a", ValueError), ("a\\n\\n", "a", ValueError), ("", "a", (KeyError, ValueError))):
    try:
        s(text, column)
    except exc:
        pass
    else:
        raise AssertionError((text, column))
''',
    },
    {
        "name": 'caesar',
        "title": 'Add textkit.caesar',
        "objective": 'Add `caesar(text: str, shift: int) -> str` in src/textkit/caesar.py. Shift ASCII letters by `shift` positions (negative and large values allowed, wrapping A-Z and a-z separately), keeping case. All other characters are unchanged.',
        "spec": 'from textkit.caesar import caesar as c\nassert c("abc xyz", 3) == "def abc"\nassert c("Hello, World!", 13) == "Uryyb, Jbeyq!"\nassert c("abc", -1) == "zab"\nassert c("abc", 29) == "def"\nassert c("", 5) == "" and c("123", 4) == "123"\n',
    },
    {
        "name": 'anagram',
        "title": 'Add textkit.group_anagrams',
        "objective": 'Add `group_anagrams(words: list[str]) -> list[list[str]]` in src/textkit/anagram.py. Words are anagrams when their lowercase letters sorted are equal. Each group keeps input order; groups are ordered by their first member alphabetically (case-insensitive).',
        "spec": 'from textkit.anagram import group_anagrams as g\nassert g(["listen", "silent", "enlist", "google"]) == [["google"], ["listen", "silent", "enlist"]]\nassert g([]) == []\nassert g(["Bat", "tab", "cat"]) == [["Bat", "tab"], ["cat"]]\nassert g(["a"]) == [["a"]]\n',
    },
    {
        "name": 'brackets',
        "title": 'Add textkit.is_balanced',
        "objective": 'Add `is_balanced(text: str) -> bool` in src/textkit/brackets.py. True when every (), [] and {} is correctly nested and closed; other characters are ignored; the empty string is balanced.',
        "spec": 'from textkit.brackets import is_balanced as b\nassert b("") and b("a(b)[c]{d}") and b("{[()]}")\nassert not b("(") and not b(")") and not b("([)]") and not b("{[}")\nassert b("no brackets at all")\n',
    },
    {
        "name": 'snake',
        "title": 'Add textkit.to_snake_case',
        "objective": "Add `to_snake_case(text: str) -> str` in src/textkit/snake.py. Convert camelCase, PascalCase, kebab-case and space separated text to lower snake_case. Acronyms stay together: 'HTTPServer' becomes 'http_server'. Repeated separators collapse and the result has no leading or trailing underscore.",
        "spec": 'from textkit.snake import to_snake_case as s\nassert s("camelCase") == "camel_case"\nassert s("PascalCase") == "pascal_case"\nassert s("HTTPServer") == "http_server"\nassert s("kebab-case-here") == "kebab_case_here"\nassert s("  two  words ") == "two_words"\nassert s("already_snake") == "already_snake"\nassert s("") == ""\n',
    },
    {
        "name": 'camel',
        "title": 'Add textkit.to_camel_case',
        "objective": "Add `to_camel_case(text: str) -> str` in src/textkit/camel.py. Convert snake_case, kebab-case or space separated text to lowerCamelCase: the first word lowercase, later words capitalised, other letters lowercased. Empty input returns ''.",
        "spec": 'from textkit.camel import to_camel_case as c\nassert c("snake_case_name") == "snakeCaseName"\nassert c("kebab-case") == "kebabCase"\nassert c("Two Words") == "twoWords"\nassert c("ALL_CAPS") == "allCaps"\nassert c("") == "" and c("_") == ""\nassert c("one") == "one"\n',
    },
    {
        "name": 'rle',
        "title": 'Add textkit.rle_encode',
        "objective": "Add `rle_encode(text: str) -> str` in src/textkit/rle.py. Run-length encode as count followed by the character, for example 'aaabcc' becomes '3a1b2c'. The empty string encodes to ''. Runs longer than nine are written with the full count ('a'*12 becomes '12a').",
        "spec": 'from textkit.rle import rle_encode as r\nassert r("aaabcc") == "3a1b2c"\nassert r("") == ""\nassert r("a" * 12) == "12a"\nassert r("abc") == "1a1b1c"\nassert r("zzzzzzzzzzzzy") == "12z1y"\n',
    },
    {
        "name": 'truncate',
        "title": 'Add textkit.truncate',
        "objective": "Add `truncate(text: str, width: int, suffix: str = '...') -> str` in src/textkit/truncate.py. If len(text) <= width return text unchanged. Otherwise cut at a word boundary so that the result including the suffix is at most width characters; when no space fits, hard cut. Raise ValueError if width is smaller than len(suffix).",
        "spec": 'from textkit.truncate import truncate as t\nassert t("short", 10) == "short"\nassert t("the quick brown fox", 12) == "the quick..."\nassert t("abcdefghijklmnop", 8) == "abcde..."\nassert t("hello world", 11) == "hello world"\nassert len(t("one two three four five", 14)) <= 14\ntry:\n    t("hello world", 2)\nexcept ValueError:\n    pass\nelse:\n    raise AssertionError("width")\n',
    },
    {
        "name": 'median',
        "title": 'Add textkit.median',
        "objective": 'Add `median(values: list[float]) -> float` in src/textkit/median.py. The middle value of the sorted list, or the mean of the two middle values for an even count. Raise ValueError on an empty list. The input list must not be modified.',
        "spec": 'from textkit.median import median as m\nassert m([3, 1, 2]) == 2\nassert m([4, 1, 3, 2]) == 2.5\nassert m([5]) == 5\nassert m([-1, -3]) == -2\nv = [3, 1, 2]\nm(v)\nassert v == [3, 1, 2]\ntry:\n    m([])\nexcept ValueError:\n    pass\nelse:\n    raise AssertionError("empty")\n',
    },
    {
        "name": 'chunk',
        "title": 'Add textkit.chunked',
        "objective": 'Add `chunked(items: list, size: int) -> list[list]` in src/textkit/chunk.py. Split into consecutive lists of `size`; the last may be shorter. Raise ValueError if size < 1. The input is not modified.',
        "spec": 'from textkit.chunk import chunked as c\nassert c([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\nassert c([], 3) == []\nassert c([1, 2], 5) == [[1, 2]]\nassert c(list("abcd"), 4) == [list("abcd")]\ntry:\n    c([1], 0)\nexcept ValueError:\n    pass\nelse:\n    raise AssertionError("size")\n',
    },
    {
        "name": 'flatten',
        "title": 'Add textkit.flatten',
        "objective": 'Add `flatten(nested) -> list` in src/textkit/flatten.py. Flatten arbitrarily nested lists and tuples into one list, preserving order. Strings and bytes are NOT flattened. Other iterables such as dicts and sets are treated as single items.',
        "spec": 'from textkit.flatten import flatten as f\nassert f([1, [2, [3, (4, 5)]], 6]) == [1, 2, 3, 4, 5, 6]\nassert f([]) == [] and f([[], [[]]]) == []\nassert f(["ab", ["cd"]]) == ["ab", "cd"]\nassert f([{"a": 1}, [{2}]]) == [{"a": 1}, {2}]\nassert f([b"xy", [1]]) == [b"xy", 1]\n',
    },
]

PYPROJECT = """[project]
name = "textkit"
version = "0.0.0"
requires-python = ">=3.11"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
"""
SPEC_HEADER = "import pathlib\nimport sys\n\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / \"src\"))\n"
INIT = '"""textkit: a small stdlib-only text toolkit (StageMesh hands-off sandbox)."""\n'
README = (
    "# textkit sandbox\n\nIsolated project for StageMesh unattended development trials. Each task adds one module under "
    "`src/textkit/` plus its own tests under `tests/`. `spec/check_*.py` are independent oracles providers must not edit.\n"
)


def _git(project: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True).stdout.strip()


def _register_tasks(project: Path, python: str, store, numbered: list[tuple[int, dict[str, str]]]) -> list[str]:
    contracts = project / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for number, task in numbered:
        task_id = store.upsert_task(task["title"], source_id=f"SANDBOX-{number:02d}", project=str(project))
        name = task["name"]
        module = f"src/textkit/{name}.py"
        tests = f"tests/test_{name}.py"
        (contracts / f"{task_id}.json").write_text(json.dumps({
            "objective": task["objective"],
            "allowed_files": [module, tests, "src/textkit/__init__.py"],
            "required_tests": [
                {"name": f"{name}-own-tests", "command": [python, "-m", "pytest", "-q", tests]},
                {"name": f"{name}-independent-spec", "command": [python, f"spec/check_{name}.py"]},
            ],
        }, indent=2), encoding="utf-8")
        ids.append(task_id)
    return ids


def create_sandbox(project: Path, python: str = sys.executable) -> list[str]:
    """Create the repo, tasks and contracts. Returns the task ids. Refuses to touch a non-empty directory."""
    project = Path(project)
    if project.exists() and any(project.iterdir()):
        raise SystemExit(f"refusing to reuse a non-empty directory: {project}")
    project.mkdir(parents=True, exist_ok=True)
    _git(project, "init", "-q", "-b", "main")
    (project / "src" / "textkit").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "spec").mkdir()
    (project / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (project / "src" / "textkit" / "__init__.py").write_text(INIT, encoding="utf-8")
    (project / "tests" / ".gitkeep").write_text("", encoding="utf-8")
    (project / "README.md").write_text(README, encoding="utf-8")
    (project / ".gitignore").write_text(".stagemesh/\n__pycache__/\n*.pyc\n.venv*/\n", encoding="utf-8")
    for task in TASKS:
        (project / "spec" / f"check_{task['name']}.py").write_text(SPEC_HEADER + task["spec"], encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "Scaffold the textkit sandbox and its independent specs")
    subprocess.run([python, "-m", "stagemesh.cli", "--project", str(project), "init"], check=True, capture_output=True)
    from stagemesh.persistence import Store

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    try:
        ids = _register_tasks(project, python, store, list(enumerate(TASKS, start=1)))
    finally:
        store.close()
    return ids


def extend_sandbox(project: Path, python: str = sys.executable) -> list[str]:
    """Add the tasks (and their independent specs) that an existing sandbox does not have yet, without touching existing ledger rows."""
    project = Path(project)
    if not (project / ".git").exists() or not (project / ".stagemesh").exists():
        raise SystemExit(f"not a sandbox project: {project}")
    new = [(number, task) for number, task in enumerate(TASKS, start=1) if not (project / "spec" / f"check_{task['name']}.py").exists()]
    if not new:
        return []
    for _number, task in new:
        (project / "spec" / f"check_{task['name']}.py").write_text(SPEC_HEADER + task["spec"], encoding="utf-8")
    _git(project, "add", "spec")
    _git(project, "commit", "-q", "-m", "Add independent specs for the next sandbox tasks")
    from stagemesh.persistence import Store

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    try:
        return _register_tasks(project, python, store, new)
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--extend", action="store_true", help="add the tasks an existing sandbox does not have yet")
    arguments = parser.parse_args()
    action = extend_sandbox if arguments.extend else create_sandbox
    print(json.dumps(action(Path(arguments.project)), indent=2))
