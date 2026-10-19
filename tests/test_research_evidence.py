"""Bound research evidence cannot authorize edits or survive identity drift."""

import hashlib
import json
from dataclasses import replace

import pytest

from stagemesh import research_context as research
from stagemesh.contracts import BoundChangeContract, ChangeContract, canonical_contract_json


@pytest.fixture
def context(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src/service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "src/private.py").write_text("PRIVATE = 1\n", encoding="utf-8")
    contract = ChangeContract(
        objective="Update service", allowed_files=("src/**",),
        forbidden_files=("src/private.py",),
    )
    canonical = canonical_contract_json(contract)
    bound = BoundChangeContract(
        contract, 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical, "a" * 40,
    )
    return project, bound, tmp_path / "research.json"


def build(project, bound, **changes):
    fields = {
        "inspected_paths": ("src/service.py",),
        "findings": (research.ResearchFinding("src/service.py", "Defines VALUE."),),
        "unresolved_scope_gaps": ("Caller behavior still needs investigation.",),
        "provenance": ("Local source inspection by task worker.",),
    }
    fields.update(changes)
    return research.build_research_evidence(project, bound, **fields)


def test_round_trip_preserves_identity_findings_and_source(context):
    project, bound, destination = context
    original = (project / "src/service.py").read_bytes()
    evidence = build(project, bound)
    assert evidence.baseline_sha == "a" * 40
    assert evidence.contract_digest == bound.digest
    assert evidence.inspected_paths == ("src/service.py",)
    assert evidence.findings[0].summary == "Defines VALUE."
    assert evidence.unresolved_scope_gaps == ("Caller behavior still needs investigation.",)
    assert evidence.provenance == ("Local source inspection by task worker.",)
    assert evidence.brief == research.build_research_brief(project, bound.contract)
    assert build(project, bound) == evidence
    research.write_research_evidence(destination, evidence, project=project, bound=bound)
    assert research.read_research_evidence(destination, project=project, bound=bound) == evidence
    assert (project / "src/service.py").read_bytes() == original
    assert bound.contract.allowed_files == ("src/**",)
    assert sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()) == [
        "src/private.py", "src/service.py",
    ]


@pytest.mark.parametrize("path", [
    "other.py", "src/private.py", "../service.py", "/src/service.py",
    "C:/src/service.py", "src/../src/service.py", "src\\service.py",
    "src/missing.py", "src/*", "src/./service.py", "src//service.py",
])
def test_rejects_out_of_scope_and_noncanonical_inspected_paths(context, path):
    project, bound, _ = context
    with pytest.raises(ValueError):
        build(project, bound, inspected_paths=(path,), findings=())


@pytest.mark.parametrize("field", ["excluded", "protected"])
def test_rejects_contract_scope_exceptions(context, field):
    project, bound, _ = context
    contract = replace(bound.contract, **{
        "exclusions" if field == "excluded" else "protected_files": ("src/service.py",),
    })
    canonical = canonical_contract_json(contract)
    bound = replace(bound, contract=contract, canonical_json=canonical,
                    digest=hashlib.sha256(canonical.encode()).hexdigest())
    with pytest.raises(ValueError, match="scope"):
        build(project, bound)


def test_finding_must_reference_an_inspected_path(context):
    project, bound, _ = context
    with pytest.raises(ValueError, match="inspected"):
        build(project, bound, findings=(research.ResearchFinding("src/private.py", "Unknown"),))


@pytest.mark.parametrize("change", [
    {"baseline_sha": "b" * 40}, {"digest": "b" * 64},
    {"baseline_sha": None}, {"baseline_sha": "HEAD"}, {"version": 99},
    {"contract": ChangeContract(objective="Different contract")},
])
def test_stale_or_invalid_binding_is_rejected_on_read_and_write(context, change):
    project, bound, destination = context
    evidence = build(project, bound)
    research.write_research_evidence(destination, evidence, project=project, bound=bound)
    stale = replace(bound, **change)
    with pytest.raises(ValueError):
        research.read_research_evidence(destination, project=project, bound=stale)
    with pytest.raises(ValueError):
        research.write_research_evidence(destination.with_suffix(".new"), evidence,
                                         project=project, bound=stale)
    assert not destination.with_suffix(".new").exists()


@pytest.mark.parametrize("field,value", [
    ("inspected_paths", ["src/private.py"]), ("baseline_sha", "b" * 40),
    ("contract_digest", "b" * 64), ("provenance", []), ("version", True),
    ("findings", [{"path": "src/private.py", "summary": "Invented"}]),
    ("unknown", "unexpected"), ("unresolved_scope_gaps", "not a list"),
])
def test_untrusted_persisted_payload_is_validated(context, field, value):
    project, bound, destination = context
    research.write_research_evidence(destination, build(project, bound), project=project, bound=bound)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload[field] = value
    destination.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        research.read_research_evidence(destination, project=project, bound=bound)


def test_persistence_never_overwrites_or_writes_inside_project(context):
    project, bound, destination = context
    evidence = build(project, bound)
    source = project / "src/service.py"
    for target in (source, project / "new.json"):
        with pytest.raises(ValueError, match="outside"):
            research.write_research_evidence(target, evidence, project=project, bound=bound)
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    research.write_research_evidence(destination, evidence, project=project, bound=bound)
    with pytest.raises(FileExistsError):
        research.write_research_evidence(destination, evidence, project=project, bound=bound)


def test_bounded_fields_and_payload(context):
    project, bound, destination = context
    for changes in (
        {"inspected_paths": ("src/service.py",) * 81},
        {"findings": (research.ResearchFinding("src/service.py", "x"),) * 81},
        {"unresolved_scope_gaps": ("gap",) * 81},
        {"provenance": ("source",) * 81},
        {"findings": (research.ResearchFinding("src/service.py", "x" * 4097),)},
        {"provenance": ()},
    ):
        with pytest.raises(ValueError):
            build(project, bound, **changes)
    destination.write_bytes(b" " * (research.MAX_RESEARCH_EVIDENCE_BYTES + 1))
    with pytest.raises(ValueError, match="size"):
        research.read_research_evidence(destination, project=project, bound=bound)


def test_symlink_cannot_escape_scope_on_construction_or_read(context):
    project, bound, destination = context
    evidence = build(project, bound)
    research.write_research_evidence(destination, evidence, project=project, bound=bound)
    source = project / "src/service.py"
    source.unlink()
    try:
        source.symlink_to(project / "src/private.py")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="scope"):
        build(project, bound)
    with pytest.raises(ValueError, match="scope"):
        research.read_research_evidence(destination, project=project, bound=bound)


def test_empty_inspection_can_record_unresolved_work_without_claiming_findings(context):
    project, bound, destination = context
    evidence = build(project, bound, inspected_paths=(), findings=())
    research.write_research_evidence(destination, evidence, project=project, bound=bound)
    loaded = research.read_research_evidence(destination, project=project, bound=bound)
    assert loaded.inspected_paths == ()
    assert loaded.findings == ()
    assert loaded.unresolved_scope_gaps == ("Caller behavior still needs investigation.",)


def test_combined_size_limit_rejects_individually_valid_fields_before_writing(context):
    project, bound, destination = context
    evidence = replace(build(project, bound), unresolved_scope_gaps=("x" * 4096,) * 80)
    with pytest.raises(ValueError, match="size"):
        research.write_research_evidence(destination, evidence, project=project, bound=bound)
    assert not destination.exists()


@pytest.mark.parametrize("raw", [b"{", b"null", b"[]", b"\xff"])
def test_malformed_evidence_is_rejected(context, raw):
    project, bound, destination = context
    destination.write_bytes(raw)
    with pytest.raises(ValueError):
        research.read_research_evidence(destination, project=project, bound=bound)
    assert destination.read_bytes() == raw


def test_valid_rebound_contract_invalidates_prior_evidence(context):
    project, bound, destination = context
    research.write_research_evidence(destination, build(project, bound), project=project, bound=bound)
    contract = replace(bound.contract, acceptance_criteria=("New acceptance criterion",))
    canonical = canonical_contract_json(contract)
    rebound = replace(bound, contract=contract, canonical_json=canonical,
                      digest=hashlib.sha256(canonical.encode()).hexdigest())
    with pytest.raises(ValueError, match="stale"):
        research.read_research_evidence(destination, project=project, bound=rebound)
