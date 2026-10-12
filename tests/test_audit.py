import json
from decimal import Decimal
from pathlib import Path

import pytest

from jarvis.safety.audit import AuditLog, redact


def test_nested_secrets_and_environment_values_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bu değer bir kimlik bilgisi değil, yalnız redaksiyon testinin işaretidir.
    marker = "synthetic-redaction-marker"
    monkeypatch.setenv("TEST_SERVICE_API_KEY", marker)
    path = tmp_path / "nested" / "audit.jsonl"
    audit = AuditLog(path)
    audit.emit(
        "test",
        message=f"prefix {marker} suffix",
        arguments={"authorization": "unknown-marker", "nested": [{"bot_token": marker}]},
        input_tokens=123,
        output_tokens=7,
        cost_usd=Decimal("0.01"),
    )
    raw = path.read_text(encoding="utf-8")
    assert marker not in raw
    assert "unknown-marker" not in raw
    assert marker not in repr(audit)
    record = json.loads(raw)
    assert record["input_tokens"] == 123
    assert record["output_tokens"] == 7
    assert record["cost_usd"] == "0.01"
    assert record["arguments"]["authorization"] == "[GİZLENDİ]"
    assert record["timestamp"].endswith("+00:00")


def test_environment_secrets_added_after_construction_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    monkeypatch.setenv("LATE_SECRET", "late-synthetic-marker")
    audit.emit("late", value="late-synthetic-marker")
    assert "late-synthetic-marker" not in audit.path.read_text(encoding="utf-8")


def test_audit_is_one_json_object_per_line_and_preserves_turkish(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.emit("first", value="Türkçe\nikinci satır")
    audit.emit("second", value=tmp_path)
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["value"] == "Türkçe\nikinci satır"
    assert json.loads(lines[1])["event"] == "second"


def test_unknown_object_repr_is_never_evaluated() -> None:
    class Sensitive:
        def __repr__(self) -> str:
            raise AssertionError("Repr sır içerebilir.")

    assert redact(Sensitive()) == "<Sensitive>"
    assert redact("Authorization: Bearer unknown-marker") == "Authorization: Bearer [GİZLENDİ]"


def test_audit_write_failure_is_not_swallowed(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path)
    with pytest.raises(OSError):
        audit.emit("unwritable")
