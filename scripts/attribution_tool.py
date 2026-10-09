"""Audit and (locally, on a throwaway clone) correct wrong Git contributor attribution.

    python scripts/attribution_tool.py audit  [--repo PATH] REV...            # report offending commits; exit 1 if any
    python scripts/attribution_tool.py rewrite --source PATH --work PATH \
        --owner-name NAME --owner-email EMAIL --owner-login LOGIN --ref REF... # rewrite REFs in a fresh --work clone of --source

Offences (the only things the narrow correction changes):
  * placeholder GitHub noreply ids (e.g. 12345678+login@users.noreply.github.com, a different real account), including addresses
    with the name glued in front, as author, committer or Co-authored-by;
  * Co-authored-by trailers naming an AI provider (anthropic.com, openai.com, x.ai).
Synthetic StageMesh identities are reported as informational only; they do not map to GitHub accounts.

`rewrite` never touches --source: it mirror-clones it and corrects the requested refs there. A commit is changed only if it is an
offender or descends from one (its parents changed); every other commit keeps its exact object, hash and signature. Trees, authors,
dates and subjects are never altered. A commit map is written so the caller can prove all of it. This tool never pushes.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.git_identity import PLACEHOLDER_NOREPLY_IDS, is_ai_or_placeholder_email  # noqa: E402

_NOREPLY_ANYWHERE = re.compile(r"(\d+)\+([A-Za-z0-9-]+)@users\.noreply\.github\.com", re.IGNORECASE)
_COAUTHOR = re.compile(r"^\s*co-authored-by\s*:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_EMAIL = re.compile(r"<([^<>]*)>")
_SEP = "\x1f"
_END = "\x1e"


@dataclass
class Finding:
    sha: str
    kind: str
    detail: str


@dataclass
class AuditReport:
    offences: list[Finding] = field(default_factory=list)
    synthetic: int = 0
    commits: int = 0

    @property
    def ok(self) -> bool:
        return not self.offences


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, encoding="utf-8", capture_output=True, check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def placeholder_email(email: str) -> bool:
    match = _NOREPLY_ANYWHERE.search(email)
    return bool(match and match.group(1) in PLACEHOLDER_NOREPLY_IDS)


def audit(repo: Path, revs: list[str]) -> AuditReport:
    fmt = _SEP.join(["%H", "%an", "%ae", "%cn", "%ce", "%B"]) + _END
    raw = _git(repo, "log", f"--format={fmt}", *revs)
    report = AuditReport()
    for entry in raw.split(_END):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha, _an, ae, _cn, ce, body = entry.split(_SEP, 5)
        report.commits += 1
        for role, email in (("author", ae), ("committer", ce)):
            if placeholder_email(email):
                report.offences.append(Finding(sha, f"placeholder-{role}", email))
            elif email.endswith(".invalid"):
                report.synthetic += 1
        for line in _COAUTHOR.findall(body):
            emails = _EMAIL.findall(line)
            if any(is_ai_or_placeholder_email(e) or placeholder_email(e) for e in emails):
                report.offences.append(Finding(sha, "coauthor-trailer", line))
    return report


_AI_DOMAINS = {"anthropic.com", "openai.com", "x.ai"}
_IDENT = re.compile(rb"^(author|committer) (.*?) <(.*)> (\d+ [+-]\d{4})$")
_NOREPLY_B = re.compile(rb"(\d+)\+([A-Za-z0-9-]+)@users\.noreply\.github\.com", re.IGNORECASE)
_COAUTHOR_B = re.compile(rb"^[ \t]*co-authored-by[ \t]*:(.*)$", re.IGNORECASE)


def _bgit(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, input=stdin, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def _split(raw: bytes) -> tuple[list[bytes], bytes]:
    head, _, body = raw.partition(b"\n\n")
    return head.split(b"\n"), body


def _fix_email(email: bytes, owner_email: bytes, owner_login: str) -> bytes:
    match = _NOREPLY_B.search(email)
    if match and match.group(1).decode() in PLACEHOLDER_NOREPLY_IDS and match.group(2).decode().lower() == owner_login.lower():
        return owner_email
    return email


def _bad_trailer(line: bytes) -> bool:
    match = _COAUTHOR_B.match(line)
    if not match:
        return False
    for email in re.findall(rb"<([^<>]*)>", match.group(1)):
        noreply = _NOREPLY_B.search(email)
        domain = email.decode("utf-8", "replace").strip().lower().rsplit("@", 1)[-1]
        if (noreply and noreply.group(1).decode() in PLACEHOLDER_NOREPLY_IDS) or domain in _AI_DOMAINS:
            return True
    return False


def _rewrite_commit(raw: bytes, parents: dict[bytes, bytes], owner_email: bytes, owner_login: str) -> bytes | None:
    """The corrected commit text, or None when the commit needs no change at all (and so keeps its hash and signature)."""
    lines, body = _split(raw)
    out: list[bytes] = []
    changed = False
    skipping_signature = False
    for line in lines:
        if skipping_signature:
            if line.startswith(b" "):
                continue
            skipping_signature = False
        if line.startswith(b"parent "):
            old = line[7:]
            if parents.get(old, old) != old:
                changed = True
            out.append(b"parent " + parents.get(old, old))
            continue
        ident = _IDENT.match(line)
        if ident:
            fixed = _fix_email(ident.group(3), owner_email, owner_login)
            if fixed != ident.group(3):
                changed = True
            out.append(ident.group(1) + b" " + ident.group(2) + b" <" + fixed + b"> " + ident.group(4))
            continue
        if line.startswith(b"gpgsig "):
            skipping_signature = True  # dropped below only if the commit actually changes
            out.append(line)
            continue
        out.append(line)
    body_lines = body.split(b"\n")
    kept = [ln for ln in body_lines if not _bad_trailer(ln)]
    if len(kept) != len(body_lines):
        changed = True
        body = b"\n".join(kept).rstrip(b"\n") + b"\n"
    if not changed:
        return None
    # the signature covered the old content, so it cannot survive any change; unchanged commits keep theirs
    cleaned: list[bytes] = []
    dropping = False
    for line in out:
        if line.startswith(b"gpgsig "):
            dropping = True
            continue
        if dropping and line.startswith(b" "):
            continue
        dropping = False
        cleaned.append(line)
    return b"\n".join(cleaned) + b"\n\n" + body


def rewrite(source: Path, work: Path, owner_name: str, owner_email: str, owner_login: str, refs: list[str]) -> Path:
    """Correct `refs` in a fresh mirror clone of `source`. Only offending commits and their descendants get new hashes."""
    if work.exists() and any(work.iterdir()):
        raise SystemExit(f"--work {work} must not exist or must be empty")
    work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--mirror", "--no-hardlinks", str(source), str(work)], check=True, capture_output=True, text=True)
    commits = _bgit(work, "rev-list", "--topo-order", "--reverse", *refs).split()
    mapping: dict[bytes, bytes] = {}
    for sha in commits:
        raw = _bgit(work, "cat-file", "commit", sha.decode())
        new_raw = _rewrite_commit(raw, mapping, owner_email.encode(), owner_login)
        if new_raw is None:
            mapping[sha] = sha
        else:
            mapping[sha] = _bgit(work, "hash-object", "-t", "commit", "-w", "--stdin", stdin=new_raw).strip()
    for ref in refs:
        old = _bgit(work, "rev-parse", f"{ref}^{{commit}}").strip()
        if mapping[old] != old:
            _bgit(work, "update-ref", ref, mapping[old].decode(), old.decode())
    commit_map = work / "commit-map"
    commit_map.write_text("old new\n" + "\n".join(f"{o.decode()} {n.decode()}" for o, n in mapping.items()) + "\n", encoding="utf-8")
    return commit_map


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--repo", type=Path, default=Path.cwd())
    a.add_argument("revs", nargs="+")
    r = sub.add_parser("rewrite")
    r.add_argument("--source", type=Path, required=True)
    r.add_argument("--work", type=Path, required=True)
    r.add_argument("--owner-name", required=True)
    r.add_argument("--owner-email", required=True)
    r.add_argument("--owner-login", required=True)
    r.add_argument("--ref", action="append", required=True, dest="refs")
    args = parser.parse_args(argv)
    if args.cmd == "audit":
        report = audit(args.repo, args.revs)
        for finding in report.offences:
            print(f"{finding.sha[:12]} {finding.kind}: {finding.detail}")
        print(f"{report.commits} commits audited, {len(report.offences)} offences, {report.synthetic} synthetic-identity mentions (informational)")
        return 0 if report.ok else 1
    commit_map = rewrite(args.source, args.work, args.owner_name, args.owner_email, args.owner_login, args.refs)
    print(f"rewritten in {args.work}; commit map: {commit_map}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
