"""Üç güvenlik regresyon testini geçici kaynak kopyalarında mutasyonla sınar."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    name: str
    source: str
    test: str
    before: str
    after: str


MUTATIONS = (
    Mutation(
        name="taint_protection_removed",
        source="jarvis/safety/gate.py",
        test="tests/test_gate.py::test_external_read_taints_turn_and_blocks_unapproved_side_effect",
        before="state.tainted and risk is not Risk.READ_ONLY",
        after="False and risk is not Risk.READ_ONLY",
    ),
    Mutation(
        name="approval_denial_bypassed",
        source="jarvis/safety/gate.py",
        test="tests/test_gate.py::test_confirm_required_cannot_bypass_approval",
        before="if not approved:",
        after="if False:",
    ),
    Mutation(
        name="tool_failure_reported_as_success",
        source="jarvis/tools/plugins/filesystem.py",
        test="tests/test_tools.py::test_trash_error_does_not_destroy_file",
        before='return ToolResult(status="error", message=f"Dosya işlemi başarısız: {detail}")',
        after='return ToolResult(success=True, status="ok", message="Dosya işlemi başarılı.")',
    ),
)


def source_hashes(root: Path) -> dict[str, str]:
    """Kopyalanan Python kaynaklarını ve pyproject ayarını içerik özetiyle döndürür."""
    files = [root / "pyproject.toml"]
    for directory in (root / "jarvis", root / "tests"):
        files.extend(
            path for path in directory.rglob("*")
            if path.is_file() and path.suffix == ".py" and "__pycache__" not in path.parts
        )
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }


def copy_project(destination: Path) -> None:
    for name in ("jarvis", "tests"):
        shutil.copytree(
            ROOT / name,
            destination / name,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
    shutil.copy2(ROOT / "pyproject.toml", destination / "pyproject.toml")


def apply_mutation(project: Path, mutation: Mutation) -> None:
    path = project / mutation.source
    source = path.read_text(encoding="utf-8")
    count = source.count(mutation.before)
    if count != 1:
        raise RuntimeError(
            f"{mutation.name}: mutasyon çapası bir kez bulunmalıydı, bulunan: {count}"
        )
    path.write_text(source.replace(mutation.before, mutation.after, 1), encoding="utf-8")


def child_environment() -> dict[str, str]:
    """Test alt sürecine yalnız çalışması için gereken zararsız ortamı aktarır."""
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
    )
    return environment


def run_pytest(project: Path, test: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
        cwd=project,
        env=child_environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def is_assertion_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = result.stdout
    has_failed_summary = re.search(r"\b[1-9]\d* failed\b", output) is not None
    has_assertion = "AssertionError" in output or re.search(r"\bE\s+assert\b", output)
    has_collection_error = any(
        marker in output
        for marker in (
            "ERROR collecting",
            "ImportError while importing test module",
            "ModuleNotFoundError",
        )
    )
    return (
        result.returncode == 1
        and has_failed_summary
        and bool(has_assertion)
        and not has_collection_error
    )


def check_mutation(mutation: Mutation) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"jarvis-mutation-{mutation.name}-") as temporary:
        workspace = Path(temporary)
        project = workspace / "project"
        project.mkdir()
        copy_project(project)
        original_hashes = source_hashes(project)

        baseline = run_pytest(project, mutation.test)
        if baseline.returncode != 0:
            return {
                **asdict(mutation),
                "ok": False,
                "phase": "baseline",
                "returncode": baseline.returncode,
                "output": baseline.stdout,
            }

        apply_mutation(project, mutation)
        mutated = run_pytest(project, mutation.test)
        mutated_hashes = source_hashes(project)
        # Yalnız geçici kopyadaki mutasyon dosyasının değişmiş olmasına izin verilir.
        expected_hashes = dict(original_hashes)
        changed_source = mutation.source
        expected_hashes[changed_source] = hashlib.sha256(
            (project / changed_source).read_bytes()
        ).hexdigest()
        copy_integrity = mutated_hashes == expected_hashes
        assertion_caught = is_assertion_failure(mutated)
        return {
            **asdict(mutation),
            "ok": assertion_caught and copy_integrity,
            "baseline_returncode": baseline.returncode,
            "mutation_returncode": mutated.returncode,
            "assertion_failure": assertion_caught,
            "isolated_copy_hashes_unchanged_except_mutant": copy_integrity,
            "output": mutated.stdout,
        }


def main() -> int:
    before = source_hashes(ROOT)
    results = [check_mutation(mutation) for mutation in MUTATIONS]
    after = source_hashes(ROOT)
    original_unchanged = before == after
    report = {
        "mutations": results,
        "original_source_hashes_unchanged": original_unchanged,
        "passed": sum(result["ok"] for result in results),
        "failed": sum(not result["ok"] for result in results) + (not original_unchanged),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
