"""Automated module-bounded progressive task context assembly for coding agents.

Assembles bounded, progressive context for task execution:
- Tier 1 (Envelope): Objective goals, task contracts, module manifests, paths, and tests.
- Tier 2 (Progressive Interfaces): AST-extracted public signatures, classes, methods, and
  docstrings of declared public contracts -- preventing context starvation without leaking internals.
- Tier 3 (Dynamic Expansion): On-demand context expansion for cross-module and unexpected dependencies.

Prevents whole-repository context saturation (>660,000 tokens) while guaranteeing
agents have complete type, interface, and invariant knowledge for safe execution.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from build_coordinator.types import ResumeContext


class ContextTier(str, Enum):
    ENVELOPE = "ENVELOPE"
    PROGRESSIVE_INTERFACES = "PROGRESSIVE_INTERFACES"
    FULL_SCOPE = "FULL_SCOPE"


@dataclass(frozen=True)
class ModuleContract:
    module_name: str
    manifest_path: str
    purpose: str = ""
    owned_paths: tuple[str, ...] = ()
    public_contracts: tuple[str, ...] = ()
    allowed_dependencies: tuple[str, ...] = ()
    forbidden_dependencies: tuple[str, ...] = ()
    primary_tests: tuple[str, ...] = ()
    raw_manifest: str = ""
    contract_signatures: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "module_name": self.module_name,
            "manifest_path": self.manifest_path,
            "purpose": self.purpose,
            "owned_paths": list(self.owned_paths),
            "public_contracts": list(self.public_contracts),
            "allowed_dependencies": list(self.allowed_dependencies),
            "forbidden_dependencies": list(self.forbidden_dependencies),
            "primary_tests": list(self.primary_tests),
            "contract_signatures": dict(self.contract_signatures),
        }


@dataclass(frozen=True)
class BoundedTaskContext:
    task_id: str
    title: str
    tier: ContextTier = ContextTier.PROGRESSIVE_INTERFACES
    objective_id: str | None = None
    objective_goal: str | None = None
    primary_module: ModuleContract | None = None
    dependency_contracts: tuple[ModuleContract, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    primary_tests: tuple[str, ...] = ()
    expanded_modules: tuple[str, ...] = ()
    estimated_context_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "tier": self.tier.value if isinstance(self.tier, ContextTier) else str(self.tier),
            "objective_id": self.objective_id,
            "objective_goal": self.objective_goal,
            "primary_module": self.primary_module.as_dict() if self.primary_module else None,
            "dependency_contracts": [dep.as_dict() for dep in self.dependency_contracts],
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "changed_files": list(self.changed_files),
            "primary_tests": list(self.primary_tests),
            "expanded_modules": list(self.expanded_modules),
            "estimated_context_tokens": self.estimated_context_tokens,
        }


def extract_contract_signatures(file_path: Path) -> str:
    """Extract public class and function signatures and docstrings from a Python source file."""
    if not file_path.is_file():
        return ""
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return ""

    stubs: list[str] = []
    module_doc = ast.get_docstring(tree)
    if module_doc:
        stubs.append(f'"""{module_doc.splitlines()[0]}"""\n')

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            base_names = [ast.unparse(b) for b in node.bases]
            bases_str = f"({', '.join(base_names)})" if base_names else ""
            class_doc = ast.get_docstring(node)
            doc_str = f'    """{class_doc.splitlines()[0]}"""\n' if class_doc else ""
            methods: list[str] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not item.name.startswith("_") or item.name == "__init__":
                        args_str = ast.unparse(item.args)
                        ret_str = f" -> {ast.unparse(item.returns)}" if item.returns else ""
                        fn_doc = ast.get_docstring(item)
                        fn_doc_str = f'        """{fn_doc.splitlines()[0]}"""\n' if fn_doc else ""
                        methods.append(f"    def {item.name}({args_str}){ret_str}:\n{fn_doc_str}        ...")
            class_body = "\n".join(methods) if methods else "    ..."
            stubs.append(f"class {node.name}{bases_str}:\n{doc_str}{class_body}\n")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                args_str = ast.unparse(node.args)
                ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                fn_doc = ast.get_docstring(node)
                fn_doc_str = f'    """{fn_doc.splitlines()[0]}"""\n' if fn_doc else ""
                stubs.append(f"def {node.name}({args_str}){ret_str}:\n{fn_doc_str}    ...")
    return "\n".join(stubs)


def resolve_contract_file(repo_root: Path, contract_name: str) -> Path | None:
    """Resolve a Python import string to its filesystem Path."""
    parts = contract_name.strip().split(".")
    if not parts:
        return None

    # Handle standard app packages
    if parts[0] == "app":
        rel = Path(*parts)
    elif parts[0] == "tooling":
        rel = Path("tooling").joinpath(*parts[1:])
    else:
        rel = Path(*parts)

    direct_py = repo_root / rel.with_suffix(".py")
    if direct_py.is_file():
        return direct_py

    init_py = repo_root / rel / "__init__.py"
    if init_py.is_file():
        return init_py

    return None


def parse_module_manifest(
    manifest_path: Path,
    repo_root: Path | None = None,
    include_signatures: bool = True,
) -> ModuleContract:
    """Parse a MODULE.md manifest file into a structured ModuleContract."""
    content = manifest_path.read_text(encoding="utf-8")
    module_name = manifest_path.parent.name
    purpose = ""
    owned_paths: list[str] = []
    public_contracts: list[str] = []
    allowed_dependencies: list[str] = []
    forbidden_dependencies: list[str] = []
    primary_tests: list[str] = []

    current_section: str | None = None

    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        if trimmed.startswith("module:"):
            module_name = trimmed.split(":", 1)[1].strip()
            current_section = None
        elif trimmed.startswith("purpose:"):
            purpose = trimmed.split(":", 1)[1].strip()
            current_section = None
        elif trimmed.startswith("owned_paths:"):
            current_section = "owned_paths"
        elif trimmed.startswith("public_contracts:"):
            current_section = "public_contracts"
        elif trimmed.startswith("allowed_dependencies:"):
            current_section = "allowed_dependencies"
        elif trimmed.startswith("forbidden_dependencies:"):
            current_section = "forbidden_dependencies"
        elif trimmed.startswith("primary_tests:"):
            current_section = "primary_tests"
        elif trimmed.endswith(":") and not trimmed.startswith("-"):
            current_section = None
        elif trimmed.startswith("- ") and current_section:
            item = trimmed[2:].strip().strip("\"'")
            if current_section == "owned_paths":
                owned_paths.append(item)
            elif current_section == "public_contracts":
                public_contracts.append(item)
            elif current_section == "allowed_dependencies":
                allowed_dependencies.append(item)
            elif current_section == "forbidden_dependencies":
                forbidden_dependencies.append(item)
            elif current_section == "primary_tests":
                primary_tests.append(item)

    signatures: dict[str, str] = {}
    if include_signatures and repo_root:
        for contract in public_contracts:
            source_file = resolve_contract_file(repo_root, contract)
            if source_file:
                sigs = extract_contract_signatures(source_file)
                if sigs:
                    signatures[contract] = sigs

    return ModuleContract(
        module_name=module_name,
        manifest_path=str(manifest_path.as_posix()),
        purpose=purpose,
        owned_paths=tuple(owned_paths),
        public_contracts=tuple(public_contracts),
        allowed_dependencies=tuple(allowed_dependencies),
        forbidden_dependencies=tuple(forbidden_dependencies),
        primary_tests=tuple(primary_tests),
        raw_manifest=content,
        contract_signatures=signatures,
    )


_KNOWN_MODULE_PATHS: dict[str, str] = {
                                                                                                                                                                            # Tooling
    "build_coordinator": "tooling/build_coordinator",
}


def find_module_manifest(repo_root: Path, module_name: str | None) -> Path | None:
    """Locate the MODULE.md manifest file for a given module identifier."""
    if not module_name:
        return None

    clean_name = module_name.strip().lower()

    if clean_name in _KNOWN_MODULE_PATHS:
        target = repo_root / _KNOWN_MODULE_PATHS[clean_name] / "MODULE.md"
        if target.is_file():
            return target

    direct = repo_root / "apps" / "api" / "app" / clean_name / "MODULE.md"
    if direct.is_file():
        return direct

    for manifest in repo_root.glob("**/MODULE.md"):
        if manifest.parent.name.lower() == clean_name:
            return manifest

    return None


def estimate_tokens(text: str) -> int:
    """Rough estimation of token count (~4 characters per token)."""
    return max(1, len(text) // 4)


def assemble_task_context(
    repo_root: Path,
    context: ResumeContext,
    *,
    tier: ContextTier = ContextTier.PROGRESSIVE_INTERFACES,
    extra: dict[str, Any] | None = None,
) -> BoundedTaskContext:
    """Assemble a bounded, progressive task context from the ResumeContext."""
    extra = extra or {}
    include_sigs = tier in (ContextTier.PROGRESSIVE_INTERFACES, ContextTier.FULL_SCOPE)

    primary_manifest = find_module_manifest(repo_root, context.primary_module)
    primary_contract = (
        parse_module_manifest(primary_manifest, repo_root=repo_root, include_signatures=include_sigs)
        if primary_manifest
        else None
    )

    dependency_contracts: list[ModuleContract] = []
    for dep_name in context.allowed_modules:
        dep_manifest = find_module_manifest(repo_root, dep_name)
        if dep_manifest and (not primary_manifest or dep_manifest != primary_manifest):
            dependency_contracts.append(
                parse_module_manifest(dep_manifest, repo_root=repo_root, include_signatures=include_sigs)
            )

    objective_data = extra.get("objective") or {}
    objective_id = objective_data.get("objective_id") if isinstance(objective_data, dict) else None
    objective_goal = (
        objective_data.get("goal") or objective_data.get("title")
        if isinstance(objective_data, dict)
        else None
    )

    approx_parts = [
        primary_contract.raw_manifest if primary_contract else "",
        "".join(dep.raw_manifest for dep in dependency_contracts),
        str(context.allowed_paths),
        str(context.primary_tests),
        str(context.files_changed),
        str(objective_goal or ""),
    ]
    if include_sigs:
        if primary_contract:
            approx_parts.extend(primary_contract.contract_signatures.values())
        for dep in dependency_contracts:
            approx_parts.extend(dep.contract_signatures.values())

    tokens = estimate_tokens("".join(approx_parts))

    return BoundedTaskContext(
        task_id=context.task_id,
        title=context.title,
        tier=tier,
        objective_id=objective_id,
        objective_goal=objective_goal,
        primary_module=primary_contract,
        dependency_contracts=tuple(dependency_contracts),
        allowed_paths=context.allowed_paths,
        forbidden_paths=context.forbidden_paths,
        changed_files=context.files_changed,
        primary_tests=context.primary_tests,
        expanded_modules=(),
        estimated_context_tokens=tokens,
    )


def expand_task_context(
    repo_root: Path,
    current_context: BoundedTaskContext,
    additional_modules: list[str],
) -> BoundedTaskContext:
    """Progressively expand the bounded context when unexpected dependencies arise."""
    existing_deps = {dep.module_name: dep for dep in current_context.dependency_contracts}
    new_deps = list(current_context.dependency_contracts)
    expanded = list(current_context.expanded_modules)
    include_sigs = current_context.tier in (ContextTier.PROGRESSIVE_INTERFACES, ContextTier.FULL_SCOPE)

    for mod_name in additional_modules:
        if mod_name in existing_deps or (
            current_context.primary_module and mod_name == current_context.primary_module.module_name
        ):
            continue
        manifest = find_module_manifest(repo_root, mod_name)
        if manifest:
            contract = parse_module_manifest(manifest, repo_root=repo_root, include_signatures=include_sigs)
            new_deps.append(contract)
            expanded.append(contract.module_name)

    # Re-estimate tokens
    approx_parts = [
        current_context.primary_module.raw_manifest if current_context.primary_module else "",
        "".join(dep.raw_manifest for dep in new_deps),
        str(current_context.allowed_paths),
        str(current_context.primary_tests),
        str(current_context.changed_files),
        str(current_context.objective_goal or ""),
    ]
    if include_sigs:
        if current_context.primary_module:
            approx_parts.extend(current_context.primary_module.contract_signatures.values())
        for dep in new_deps:
            approx_parts.extend(dep.contract_signatures.values())

    tokens = estimate_tokens("".join(approx_parts))

    return BoundedTaskContext(
        task_id=current_context.task_id,
        title=current_context.title,
        tier=current_context.tier,
        objective_id=current_context.objective_id,
        objective_goal=current_context.objective_goal,
        primary_module=current_context.primary_module,
        dependency_contracts=tuple(new_deps),
        allowed_paths=current_context.allowed_paths,
        forbidden_paths=current_context.forbidden_paths,
        changed_files=current_context.changed_files,
        primary_tests=current_context.primary_tests,
        expanded_modules=tuple(expanded),
        estimated_context_tokens=tokens,
    )
