"""Windows sürüm CI iş akışı ve Inno Setup hizalama testleri (ADR 0088)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-windows.yml"
ISS_PATH = ROOT / "packaging" / "artemis.iss"


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

    # SignPath + Authenticode kancası (`Resolve signing mode` çıktıları; `secrets.` yok)
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
        "WINDOWS_CERT_PFX_BASE64",
        "WINDOWS_CERT_PASSWORD",
    ):
        assert secret_name in signing_env
    assert "GITHUB_OUTPUT" in signing_run
    assert "signpath" in signing_run and "authenticode" in signing_run

    signpath_steps = [s for s in steps if "SignPath" in s.get("name", "")]
    assert signpath_steps
    signpath_if = str(signpath_steps[0].get("if", ""))
    assert "steps.signing.outputs.signpath == 'true'" in signpath_if

    auth_steps = [s for s in steps if "Authenticode" in s.get("name", "")]
    assert auth_steps
    auth_if = str(auth_steps[0].get("if", ""))
    assert "steps.signing.outputs.authenticode == 'true'" in auth_if

    fallback_steps = [s for s in steps if "Unsigned" in s.get("name", "")]
    assert fallback_steps
    fallback_if = str(fallback_steps[0].get("if", ""))
    assert "steps.signing.outputs.signpath != 'true'" in fallback_if
    assert "steps.signing.outputs.authenticode != 'true'" in fallback_if

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
    assert any(
        str(s.get("uses", "")).startswith("softprops/action-gh-release")
        for s in release_job["steps"]
    )


def test_iss_metadata_alignment_and_invariants() -> None:
    iss_text = ISS_PATH.read_text(encoding="utf-8")
    _, _, after_setup = iss_text.partition("[Setup]")
    setup_section, _, _ = after_setup.partition("[Languages]")
    _, _, uninstall_section = iss_text.partition("[UninstallDelete]")

    assert '#define MyAppVersion "0.2.3"' in iss_text
    for expected in (
        "AppName=Artemis",
        "AppPublisher=Artemis",
        "AppPublisherURL=https://github.com/UmutErayAltay/artemis",
        "AppSupportURL=https://github.com/UmutErayAltay/artemis/issues",
        "AppUpdatesURL=https://github.com/UmutErayAltay/artemis/releases",
        "VersionInfoVersion={#MyAppVersion}",
        "VersionInfoProductName=Artemis",
        "VersionInfoCompany=Artemis",
        "DisableDirPage=yes",
        "PrivilegesRequired=lowest",
        "OutputDir=..\\dist\\installer",
        "OutputBaseFilename=Artemis-Setup-{#MyAppVersion}",
        "#ifdef SignToolCmd",
    ):
        assert expected in setup_section, expected
    assert "{userappdata}" not in uninstall_section.casefold()
