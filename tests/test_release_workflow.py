"""Windows sürüm CI iş akışı ve Inno Setup hizalama testleri (ADR 0088)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-windows.yml"
ISS_PATH = ROOT / "packaging" / "artemis.iss"
PRIVACY_TXT_PATH = ROOT / "packaging" / "PRIVACY.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"


def test_release_windows_workflow_structure_and_signing_fallback() -> None:
    assert WORKFLOW_PATH.is_file()
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)

    # PyYAML 'on' anahtarını True veya 'on' olarak çözebilir
    triggers = data.get("on") if "on" in data else data.get(True)
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    assert "push" in triggers
    assert "v*" in triggers["push"]["tags"]

    jobs = data["jobs"]
    build_job = jobs["build"]
    assert build_job["runs-on"] == "windows-latest"

    steps = build_job["steps"]
    step_names = [step.get("name", "") for step in steps]
    joined_runs = "\n".join(str(step.get("run", "")) for step in steps)

    # Python 3.12 + PyInstaller + Inno Setup
    py_steps = [
        step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    assert py_steps and py_steps[0]["with"]["python-version"] == "3.12"
    assert "packaging/artemis.spec" in joined_runs
    assert "packaging\\artemis.iss" in joined_runs or "packaging/artemis.iss" in joined_runs
    assert "/DMyAppVersion=" in joined_runs

    # Sürüm adımı kabuk enjeksiyonunu önlemek için ref/ref_name değerlerini env üzerinden almalı
    version_steps = [s for s in steps if s.get("id") == "version"]
    assert version_steps
    version_env = version_steps[0].get("env", {})
    version_run = str(version_steps[0].get("run", ""))
    assert isinstance(version_env, dict)
    assert version_env.get("GITHUB_REF_NAME") == "${{ github.ref_name }}"
    assert "${{ github.ref_name }}" not in version_run
    assert "$env:GITHUB_REF_NAME" in version_run

    # SignPath kancası (`Resolve signing mode` çıktıları; `secrets.` yok)
    signing_steps = [s for s in steps if s.get("id") == "signing"]
    assert signing_steps
    signing_env = signing_steps[0].get("env", {})
    signing_run = str(signing_steps[0].get("run", ""))
    assert isinstance(signing_env, dict)
    for secret_name in (
        "SIGNPATH_API_TOKEN",
        "SIGNPATH_ORGANIZATION_ID",
        "SIGNPATH_PROJECT_SLUG",
        "SIGNPATH_SIGNING_POLICY_SLUG",
    ):
        assert secret_name in signing_env
    assert "GITHUB_OUTPUT" in signing_run
    assert "signpath" in signing_run
    # ADR 0088: tek imza yolu SignPath; PFX/Authenticode yolu kaldırıldı
    assert "WINDOWS_CERT_PFX" not in raw
    assert "authenticode" not in raw.casefold()

    # SignPath imzalama: imzasız kurucu yükleme adımı SignPath adımından önce gelmeli
    upload_unsigned_idx = next(
        i for i, s in enumerate(steps) if s.get("id") == "upload_unsigned"
    )
    upload_unsigned_step = steps[upload_unsigned_idx]
    assert str(upload_unsigned_step.get("uses", "")).startswith("actions/upload-artifact")
    assert "steps.signing.outputs.signpath == 'true'" in str(upload_unsigned_step.get("if", ""))
    assert upload_unsigned_step["with"]["retention-days"] == 1
    assert "Artemis-Setup-${{ env.APP_VERSION }}.exe" in str(upload_unsigned_step["with"]["path"])

    signpath_idx = next(
        i
        for i, s in enumerate(steps)
        if str(s.get("uses", "")).startswith("signpath/github-action-submit-signing-request")
    )
    assert upload_unsigned_idx < signpath_idx
    signpath_step = steps[signpath_idx]
    signpath_if = str(signpath_step.get("if", ""))
    assert "steps.signing.outputs.signpath == 'true'" in signpath_if
    signpath_with = signpath_step["with"]
    assert signpath_with["connector-url"] == "https://githubactions.signpath.io"
    assert "steps.upload_unsigned.outputs.artifact-id" in str(
        signpath_with["github-artifact-id"]
    )
    assert signpath_with["output-artifact-directory"] == "dist/installer"
    assert signpath_with["wait-for-completion"] is True
    assert "input-artifact-directory" not in signpath_with

    signing_idx = next(i for i, s in enumerate(steps) if s.get("id") == "signing")
    iscc_indices = [
        i for i, s in enumerate(steps) if "packaging\\artemis.iss" in str(s.get("run", ""))
    ]
    assert iscc_indices and all(signing_idx < i for i in iscc_indices)

    fallback_steps = [s for s in steps if "Unsigned" in s.get("name", "")]
    assert fallback_steps
    fallback_if = str(fallback_steps[0].get("if", ""))
    assert "steps.signing.outputs.signpath != 'true'" in fallback_if

    for job in jobs.values():
        for step in job.get("steps", []):
            assert "secrets." not in str(step.get("if", ""))

    assert any("SHA256SUMS.txt" in name for name in step_names)
    assert "SHA256SUMS.txt" in joined_runs
    assert any(
        str(s.get("uses", "")).startswith("actions/upload-artifact") for s in steps
    )

    release_job = jobs["release"]
    assert "refs/tags/v" in str(release_job.get("if", ""))
    assert release_job["permissions"]["contents"] == "write"
    gh_release_steps = [
        s
        for s in release_job["steps"]
        if str(s.get("uses", "")).startswith("softprops/action-gh-release")
    ]
    assert gh_release_steps
    release_body = str(gh_release_steps[0]["with"].get("body", ""))
    assert "docs/CODE_SIGNING_POLICY.md" in release_body
    assert "docs/PRIVACY.md" in release_body
    assert "CHANGELOG.md" in release_body
    assert "blob/v${{ needs.build.outputs.version }}/" in release_body
    assert "imzasız" in release_body and "Get-FileHash" in release_body


def test_iss_metadata_alignment_and_invariants() -> None:
    iss_text = ISS_PATH.read_text(encoding="utf-8")
    _, _, after_setup = iss_text.partition("[Setup]")
    setup_section, _, _ = after_setup.partition("[Languages]")
    _, _, uninstall_section = iss_text.partition("[UninstallDelete]")

    pyproject_data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    pyproject_version = pyproject_data["project"]["version"]
    assert f'#define MyAppVersion "{pyproject_version}"' in iss_text
    assert PRIVACY_TXT_PATH.is_file()
    assert PRIVACY_TXT_PATH.read_text(encoding="utf-8").strip()
    assert "uninstallable=no" not in iss_text.casefold()
    for expected in (
        "AppName=Artemis",
        "AppPublisher=Artemis",
        "AppPublisherURL=https://github.com/UmutErayAltay/artemis",
        "AppSupportURL=https://github.com/UmutErayAltay/artemis/issues",
        "AppUpdatesURL=https://github.com/UmutErayAltay/artemis/releases",
        "VersionInfoVersion={#MyAppVersion}",
        "VersionInfoProductName=Artemis",
        "VersionInfoCompany=Artemis",
        "InfoBeforeFile=PRIVACY.txt",
        "DisableDirPage=yes",
        "PrivilegesRequired=lowest",
        "OutputDir=..\\dist\\installer",
        "OutputBaseFilename=Artemis-Setup-{#MyAppVersion}",
        "#ifdef SignToolCmd",
    ):
        assert expected in setup_section, expected
    assert "{userappdata}" not in uninstall_section.casefold()
