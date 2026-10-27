"""Read-only adapter regressions, runnable with unittest or pytest."""
import json
import tempfile
import unittest
from pathlib import Path

from stagemesh.spec_integrity import (
    CustomSpecAdapter, OpenSpecAdapter, structural_impact_manifest,
)
from stagemesh.spec_refs import SpecAdapterError


class SpecAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def discover(self):
        return OpenSpecAdapter().discover(self.root)

    def impact(self, before, after, paths):
        return structural_impact_manifest(
            before, after, paths, base_revision="base-123", candidate_revision="candidate-456"
        )

    def test_discovery_separates_canonical_history_and_pending_without_writes(self):
        active = self.write("openspec/specs/auth/spec.md", "### Requirement: Login\nMUST log in.\n")
        self.write("openspec/changes/archive/old/specs/auth/spec.md",
                   "### Requirement: Login\nOld login.\n")
        self.write("openspec/changes/pending/specs/auth/spec.md",
                   "### Requirement: Future\nNot canonical.\n")
        original = active.read_bytes()
        snapshot = self.discover()
        self.assertEqual([(o.status, o.canonical) for o in snapshot.owners],
                         [("history", False), ("active", True)])
        owner = snapshot.owners[1]
        self.assertEqual(owner.id, "openspec/specs/auth/spec.md")
        self.assertEqual(owner.requirements[0].id,
                         "openspec/specs/auth/spec.md#requirement-login")
        self.assertEqual(active.read_bytes(), original)
        self.assertEqual(snapshot, self.discover())

    def test_manifest_classifies_changes_and_preserves_revision_identity(self):
        path = "openspec/specs/auth/spec.md"
        self.write(path, "### Requirement: Login\nOld.\n### Requirement: Retire\nGone.\n")
        before = self.discover()
        self.write(path, "### Requirement: Login\nNew.\n### Requirement: Add\nAdded.\n")
        result = self.impact(before, self.discover(), [path, "src/unknown.py", path])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["assessment"], "structural-only")
        self.assertEqual(result["base_revision"], "base-123")
        self.assertEqual(result["candidate_revision"], "candidate-456")
        self.assertEqual([(r["change"], r["requirement_id"].split("#")[1])
                          for r in result["requirements"]],
                         [("added", "requirement-add"), ("modified", "requirement-login"),
                          ("removed", "requirement-retire")])
        self.assertEqual(result["unmapped_files"], ["src/unknown.py"])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_removed_file_and_title_rename_are_not_semantic_matches(self):
        path = "openspec/specs/a/spec.md"
        target = self.write(path, "### Requirement: Old\nSame.\n")
        before = self.discover()
        self.write(path, "### Requirement: New\nSame.\n")
        self.assertEqual({r["change"] for r in self.impact(before, self.discover(), [path])
                          ["requirements"]}, {"added", "removed"})
        target.unlink()
        result = self.impact(before, self.discover(), [path])
        self.assertEqual([r["change"] for r in result["requirements"]], ["removed"])
        self.assertEqual(result["unmapped_files"], [])

    def test_explicit_links_resolve_only_to_active_owners_and_requirements(self):
        path = "openspec/specs/a/spec.md"
        self.write(path, "### Requirement: A\nSee [B](../b/spec.md#requirement-b) "
                   "and [missing](../missing/spec.md) and [bad](../b/spec.md#absent).\n")
        self.write("openspec/specs/b/spec.md", "### Requirement: B\nB.\n")
        snapshot = self.discover()
        unresolved = self.impact(snapshot, snapshot, [path])["unresolved_references"]
        self.assertEqual({r["target"] for r in unresolved},
                         {"openspec/specs/missing/spec.md", "openspec/specs/b/spec.md#absent"})
        self.assertEqual(len(snapshot.owners[0].requirements[0].dependencies), 3)
        (self.root / "openspec/specs/b/spec.md").unlink()
        self.assertEqual(len(self.impact(snapshot, self.discover(),
                                         ["openspec/specs/b/spec.md"])
                             ["unresolved_references"]), 3)

    def test_fenced_examples_do_not_create_requirements_or_dependencies(self):
        self.write("openspec/specs/a/spec.md", "~~~md\n### Requirement: Fake\n"
                   "[fake](../missing/spec.md)\n~~~\n### Requirement: Real\nOK.\n")
        owner = self.discover().owners[0]
        self.assertEqual(len(owner.requirements), 1)
        self.assertTrue(owner.requirements[0].id.endswith("#requirement-real"))
        self.assertEqual(owner.dependencies, ())

    def test_duplicate_heading_anchor_is_rejected(self):
        self.write("openspec/specs/a/spec.md",
                   "### Requirement: Same\nA\n### Requirement: Same!\nB\n")
        with self.assertRaises(SpecAdapterError):
            self.discover()

    def custom(self, **changes):
        data = {"schema_version": 1, "owners": [{"id": "policy", "path": "policy.txt",
                "status": "active", "dependencies": [], "requirements": [
                    {"id": "POL-1", "text": "Must check", "dependencies": ["MISSING"]}]}]}
        data.update(changes)
        self.write("adapter.json", json.dumps(data))
        return CustomSpecAdapter("adapter.json").discover(self.root)

    def test_custom_contract_retains_ids_dependencies_and_history(self):
        before = self.custom()
        owner = {"id": "policy", "path": "policy.txt", "status": "history",
                 "dependencies": [], "requirements": [
                     {"id": "POL-1", "text": "Changed", "dependencies": ["MISSING"]}]}
        after = self.custom(owners=[owner])
        result = self.impact(before, after, ["adapter.json"])
        self.assertEqual(result["requirements"][0]["requirement_id"], "POL-1")
        self.assertEqual(result["requirements"][0]["change"], "modified")
        self.assertEqual(result["owners"][0]["status"], "history")
        self.assertFalse(after.owners[0].canonical)
        self.assertEqual(result["unresolved_references"][0]["target"], "MISSING")

    def test_custom_contract_rejects_bad_schema_ids_status_and_paths(self):
        for changes in [{"schema_version": 2}, {"schema_version": True}, {"owners": "bad"},
                        {"owners": [{"id": "p", "path": "../outside", "status": "active",
                                     "requirements": []}]},
                        {"owners": [{"id": "p", "path": "p", "status": "draft",
                                     "requirements": []}]}]:
            with self.subTest(changes=changes), self.assertRaises(SpecAdapterError):
                self.custom(**changes)
        owner = {"id": "p", "path": "p", "status": "active", "requirements": [
            {"id": "DUP", "text": "A"}, {"id": "DUP", "text": "B"}]}
        with self.assertRaises(SpecAdapterError):
            self.custom(owners=[owner])
        with self.assertRaises(SpecAdapterError):
            CustomSpecAdapter("../adapter.json").discover(self.root)

    def test_rejects_unsafe_changed_paths_and_mixed_adapter_versions(self):
        empty = self.discover()
        for path in ["../outside", "C:/outside", "/outside", "a/../b"]:
            with self.subTest(path=path), self.assertRaises(SpecAdapterError):
                self.impact(empty, empty, [path])
        with self.assertRaises(SpecAdapterError):
            self.impact(empty, self.custom(), [])


    def test_history_does_not_satisfy_active_dependencies(self):
        owners = [
            {"id": "current", "path": "current.txt", "status": "active",
             "dependencies": ["OLD", "current"], "requirements": []},
            {"id": "archive", "path": "old.txt", "status": "history",
             "requirements": [{"id": "OLD", "text": "Old requirement"}]},
        ]
        snapshot = self.custom(owners=owners)
        result = self.impact(snapshot, snapshot, ["current.txt"])
        self.assertEqual(result["unresolved_references"], [
            {"source_id": "current", "target": "OLD", "reason": "no-active-target"}])
        self.assertEqual(result["requirements"], [])

    def test_new_file_and_unchanged_requirement_survive_unrelated_prose(self):
        empty = self.discover()
        path = "openspec/specs/new/spec.md"
        self.write(path, "# Purpose\nInitial.\n### Requirement: Stable\nMust stay.\n")
        before = self.discover()
        self.assertEqual(self.impact(empty, before, [path])["requirements"][0]["change"],
                         "added")
        self.write(path, "# Purpose\nUpdated.\n### Requirement: Stable\nMust stay.\n")
        self.assertEqual(self.impact(before, self.discover(), [path])["requirements"], [])
        self.assertEqual(self.impact(empty, before, ["unrelated.py"])["requirements"], [])

    def test_malformed_custom_contract_is_rejected_without_execution(self):
        for text in ['{bad', '{"schema_version": 1, "owners": [], "command": "ignored"}',
                     '{"schema_version": 2, "schema_version": 1, "owners": []}']:
            with self.subTest(text=text):
                self.write("adapter.json", text)
                with self.assertRaises(SpecAdapterError):
                    CustomSpecAdapter("adapter.json").discover(self.root)
        for field, value in [("status", []), ("id", ""), ("dependencies", [3]),
                             ("dependencies", "target"), ("path", "C:/outside")]:
            owner = {"id": "p", "path": "p", "status": "active", "requirements": []}
            owner[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SpecAdapterError):
                self.custom(owners=[owner])

    def test_project_escape_via_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "spec.md"
            target.write_text("### Requirement: Outside\nPrivate.\n", encoding="utf-8")
            link = self.root / "openspec/specs/escape"
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaises(SpecAdapterError):
                self.discover()
            self.assertEqual(target.read_text(encoding="utf-8"),
                             "### Requirement: Outside\nPrivate.\n")

    def test_external_and_escaping_references_remain_unresolved(self):
        path = "openspec/specs/a/spec.md"
        self.write(path, "### Requirement: A\n[external](https://example.invalid/spec) "
                   "[escape](../../../../outside.md)\n")
        snapshot = self.discover()
        self.assertEqual({ref["target"] for ref in self.impact(snapshot, snapshot, [path])
                          ["unresolved_references"]},
                         {"https://example.invalid/spec", "../outside.md"})


if __name__ == "__main__":
    unittest.main()
