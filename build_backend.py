from __future__ import annotations

import base64
import csv
import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

NAME = "stagemesh"
UI_ASSETS = ("index.html", "app.js", "app.css")  # keep equal to stagemesh.api_server.UI_ASSETS (tested)
VERSION = "0.4.0"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def _metadata() -> str:
    return "\n".join(
        [
            "Metadata-Version: 2.3",
            f"Name: {NAME}",
            f"Version: {VERSION}",
            "Summary: Provider-neutral control plane for autonomous software engineering lifecycles",
            "Requires-Python: >=3.11",
            "Provides-Extra: dev",
            'Requires-Dist: pytest>=8.0; extra == "dev"',
            'Requires-Dist: ruff>=0.6; extra == "dev"',
            "",
        ]
    )


def _wheel() -> str:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: stagemesh-local-backend\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )


def _build_info(root: Path) -> bytes:
    """Provenance baked into a built wheel: the checkout commit it came from (a source checkout has no such module)."""

    def git(*args: str) -> str:
        try:
            out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=20, check=False)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain")) if commit else False
    lines = [f"SOURCE_COMMIT = {commit!r}", f"SOURCE_DIRTY = {dirty!r}", f"BUILT_FROM = {str(root)!r}", ""]
    return "\n".join(lines).encode("utf-8")


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_editable(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    dist = Path(metadata_directory) / DIST_INFO
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(_metadata(), encoding="utf-8")
    (dist / "WHEEL").write_text(_wheel(), encoding="utf-8")
    (dist / "entry_points.txt").write_text("[console_scripts]\nstagemesh=stagemesh.cli:main\n", encoding="utf-8")
    (dist / "RECORD").write_text("", encoding="utf-8")
    return DIST_INFO


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    root = Path(__file__).resolve().parent
    wheel_name = f"{NAME}-{VERSION}-py3-none-any.whl"
    wheel_path = Path(wheel_directory) / wheel_name
    records: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted((root / "src" / "stagemesh").rglob("*.py")):
            arcname = path.relative_to(root / "src").as_posix()
            data = path.read_bytes()
            zf.writestr(arcname, data)
            records.append((arcname, data))
        # Offline skills, their companions, provenance, and licenses belong to
        # the independently installed runtime, not the host/source checkout.
        missing_ui = [name for name in UI_ASSETS if not (root / "src" / "stagemesh" / "ui" / name).is_file()]
        if missing_ui:
            raise FileNotFoundError(f"operator UI assets missing from the source tree: {', '.join(missing_ui)}")
        # The operator UI is exactly these static assets served by `stagemesh serve`; nothing else in ui/ ships.
        asset_paths = [
            *sorted((root / "src" / "stagemesh" / "skill_packs").rglob("*")),
            *(root / "src" / "stagemesh" / "ui" / name for name in UI_ASSETS),
        ]
        for path in asset_paths:
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".py":
                continue
            arcname = path.relative_to(root / "src").as_posix()
            data = path.read_bytes()
            zf.writestr(arcname, data)
            records.append((arcname, data))
        generated = {
            "stagemesh/_build_info.py": _build_info(root),
            f"{DIST_INFO}/METADATA": _metadata().encode("utf-8"),
            f"{DIST_INFO}/WHEEL": _wheel().encode("utf-8"),
            f"{DIST_INFO}/entry_points.txt": b"[console_scripts]\nstagemesh=stagemesh.cli:main\n",
        }
        for arcname, data in generated.items():
            zf.writestr(arcname, data)
            records.append((arcname, data))
        record_buf = io.StringIO()
        writer = csv.writer(record_buf, lineterminator="\n")
        for arcname, data in records:
            writer.writerow([arcname, f"sha256={_b64(data)}", str(len(data))])
        writer.writerow([f"{DIST_INFO}/RECORD", "", ""])
        zf.writestr(f"{DIST_INFO}/RECORD", record_buf.getvalue().encode("utf-8"))
    return wheel_name


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    root = Path(__file__).resolve().parent
    wheel_name = f"{NAME}-{VERSION}-0.editable-py3-none-any.whl"
    wheel_path = Path(wheel_directory) / wheel_name
    pth_name = f"__editable__.{NAME}.pth"
    pth_data = str(root / "src").encode("utf-8")
    generated = {
        pth_name: pth_data,
        f"{DIST_INFO}/METADATA": _metadata().encode("utf-8"),
        f"{DIST_INFO}/WHEEL": _wheel().encode("utf-8"),
        f"{DIST_INFO}/entry_points.txt": b"[console_scripts]\nstagemesh=stagemesh.cli:main\n",
    }
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in generated.items():
            zf.writestr(arcname, data)
        record_buf = io.StringIO()
        writer = csv.writer(record_buf, lineterminator="\n")
        for arcname, data in generated.items():
            writer.writerow([arcname, f"sha256={_b64(data)}", str(len(data))])
        writer.writerow([f"{DIST_INFO}/RECORD", "", ""])
        zf.writestr(f"{DIST_INFO}/RECORD", record_buf.getvalue().encode("utf-8"))
    return wheel_name
