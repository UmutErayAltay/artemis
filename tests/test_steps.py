"""Adım satırı kuralları ve araç etiketleri Qt'siz ve sahte sağlayıcısız test edilir."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from artemis.channels.desktop.steps import StepLine, step_line
from artemis.tools.registry import discover_tools

LABELS: dict[str, tuple[str, str]] = {
    "read_file": ("Dosya okuma", "Dosya okundu"),
    "open_application": ("Uygulama açma", "Uygulama açıldı"),
}

# Gizli görünümlü argüman ve sonuç metni; hiçbir satırda geçmemeli.
SECRET = "sk-test-GIZLI-123456"
LEAKY_FIELDS: dict[str, Any] = {
    "tool": "read_file",
    "arguments": {"path": f"notlar/{SECRET}.txt", "text": SECRET},
    "result": {"status": "ok", "success": True, "message": f"içerik {SECRET}"},
}


def _line(event: str, **fields: Any) -> StepLine:
    line = step_line(event, fields, LABELS)
    assert line is not None
    return line


# Tool result: durum tablosu. ✓ yalnız status "ok" ve success True birlikteyken.
@pytest.mark.parametrize(
    ("result", "text", "kind"),
    [
        ({"status": "ok", "success": True}, "Dosya okundu ✓", "ok"),
        (
            {"status": "sent", "success": False},
            "Dosya okuma: komut gönderildi, sonucu doğrulanamadı",
            "sent",
        ),
        ({"status": "denied", "success": False}, "Dosya okuma: onay verilmedi", "denied"),
        ({"status": "error", "success": False}, "Dosya okuma başarısız ✗", "fail"),
        # Tutarsız kayıt (status ok, success False) başarı diye sunulmaz.
        ({"status": "ok", "success": False}, "Dosya okuma başarısız ✗", "fail"),
        ({"status": "ok"}, "Dosya okuma başarısız ✗", "fail"),
        ({"status": "bilinmeyen"}, "Dosya okuma başarısız ✗", "fail"),
    ],
)
def test_tool_result_status_table(result: dict[str, Any], text: str, kind: str) -> None:
    line = _line("tool_result", tool="read_file", result=result)
    assert line.text == text
    assert line.kind == kind
    assert line.internal is False


def test_tool_result_without_dict_result_is_failure() -> None:
    line = _line("tool_result", tool="read_file", result="ok")
    assert line == StepLine("Dosya okuma başarısız ✗", "fail", False)


def test_check_mark_only_for_verified_ok() -> None:
    statuses = ("ok", "sent", "denied", "error")
    for status in statuses:
        for success in (True, False):
            result = {"status": status, "success": success}
            line = _line("tool_result", tool="read_file", result=result)
            assert ("✓" in line.text) is (status == "ok" and success is True), (status, success)


def test_sent_never_shows_done_label_with_check() -> None:
    result = {"status": "sent", "success": False}
    line = _line("tool_result", tool="open_application", result=result)
    assert "Uygulama açıldı" not in line.text
    assert "✓" not in line.text
    assert line.kind == "sent"


def test_tool_interrupted_is_not_reported_as_done_or_denied() -> None:
    line = _line("tool_interrupted", tool="read_file", completion="unknown")
    assert line == StepLine("Dosya okuma: kesildi, sonucu doğrulanamadı", "fail", False)


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("completed", "Yanıt hazır"),
        ("cancelled", "İşlem iptal edildi"),
        ("timeout", "Süre sınırı doldu"),
        ("budget", "Bütçe sınırı"),
        ("max_steps", "Adım sınırı doldu"),
        ("cleanup_pending", "Önceki işlem temizleniyor"),
        ("denied_repeat", "Reddedilen işlem tekrar istendi; durduruldu"),
        ("repeated_call", "Aynı işlem tekrar istendi; durduruldu"),
        ("local_limit", "Yerel model sınırı"),
        ("provider_error", "Model yanıt veremedi"),
        ("unknown", "Tur bitti (bilinmeyen neden)"),
        ("yeni_neden", "Tur bitti (bilinmeyen neden)"),
    ],
)
def test_turn_end_reasons(reason: str, text: str) -> None:
    line = _line("turn_end", stop_reason=reason)
    assert line.text == text
    if reason == "completed":
        # Yanıt hazır yalnız ayrıntıda görünür; ana akış yanıtın kendisiyle dolar.
        assert line == StepLine("Yanıt hazır", "info", True)
    else:
        assert line.kind == "fail"
        assert line.internal is False


def test_turn_end_without_reason_is_unknown_failure() -> None:
    line = _line("turn_end")
    assert line == StepLine("Tur bitti (bilinmeyen neden)", "fail", False)


@pytest.mark.parametrize(
    ("event", "fields", "text"),
    [
        ("turn_start", {}, "İstek alındı"),
        ("model_response", {}, "Model yanıtı alındı"),
        ("privacy_decision", {}, "Gizlilik kontrolü"),
        ("context_reset", {}, "Yeni sohbet başladı"),
        ("tool_requested", {"tool": "read_file"}, "Dosya okuma istendi"),
        ("tool_dispatch", {"tool": "read_file"}, "Dosya okuma başlatıldı"),
        ("approval", {"tool": "read_file", "approved": True}, "Onay verildi: Dosya okuma"),
        ("approval", {"tool": "read_file", "approved": False}, "Onay verilmedi: Dosya okuma"),
        ("approval", {"tool": "read_file"}, "Onay verilmedi: Dosya okuma"),
        ("provider_error", {}, "provider error"),
        ("provider_fallback", {}, "provider fallback"),
    ],
)
def test_internal_events_are_internal_info(event: str, fields: dict[str, Any], text: str) -> None:
    line = _line(event, **fields)
    assert line == StepLine(text, "info", True)


def test_unknown_tool_falls_back_to_raw_name() -> None:
    line = _line("tool_result", tool="mystery_tool", result={"status": "ok", "success": True})
    assert line == StepLine("mystery_tool tamamlandı ✓", "ok", False)
    line = _line("tool_requested", tool="mystery_tool")
    assert line.text == "mystery_tool istendi"


def test_missing_tool_name_uses_generic_display() -> None:
    line = _line("tool_interrupted")
    assert line.text == "Araç: kesildi, sonucu doğrulanamadı"


def test_empty_event_yields_no_line() -> None:
    assert step_line("", {}, LABELS) is None


@pytest.mark.parametrize(
    "event",
    [
        "tool_result",
        "tool_interrupted",
        "turn_end",
        "approval",
        "tool_requested",
        "tool_dispatch",
        "turn_start",
        "provider_error",
    ],
)
def test_no_argument_or_result_text_leaks_into_lines(event: str) -> None:
    line = step_line(event, LEAKY_FIELDS, LABELS)
    assert line is not None
    assert SECRET not in line.text
    assert "notlar/" not in line.text


def test_tool_labels_are_used_for_display() -> None:
    line = _line("tool_requested", tool="read_file")
    assert line.text == "Dosya okuma istendi"


def test_every_registered_tool_declares_label_and_done_label() -> None:
    registry = discover_tools()
    definitions = registry.definitions()
    assert definitions, "Kayıtlı araç bulunamadı"
    for definition in definitions:
        tool = registry.get(definition.name)
        assert tool is not None
        assert tool.label.strip(), f"label boş: {definition.name}"
        assert tool.done_label.strip(), f"done_label boş: {definition.name}"


# Tablodaki Türkçe metinler birebir tutulur; sessiz değişiklik kullanıcı arayüzünü bozar.
EXPECTED_TOOL_LABELS: dict[str, tuple[str, str]] = {
    "open_application": ("Uygulama açma", "Uygulama açıldı"),
    "close_application": ("Uygulama kapatma", "Uygulama kapatıldı"),
    "read_clipboard": ("Panoyu okuma", "Pano okundu"),
    "write_clipboard": ("Panoya yazma", "Panoya yazıldı"),
    "open_url": ("Bağlantı açma", "Bağlantı tarayıcıya gönderildi"),
    "list_files": ("Dosyaları listeleme", "Dosyalar listelendi"),
    "read_file": ("Dosya okuma", "Dosya okundu"),
    "create_file": ("Dosya oluşturma", "Dosya oluşturuldu"),
    "move_file": ("Dosya taşıma", "Dosya taşındı"),
    "rename_file": ("Dosyayı yeniden adlandırma", "Dosya yeniden adlandırıldı"),
    "delete_file": ("Dosya silme", "Dosya çöp kutusuna taşındı"),
    "create_directory": ("Klasör oluşturma", "Klasör oluşturuldu"),
    "web_search": ("Web araması", "Web'de arandı"),
    "system_info": ("Sistem bilgisi", "Sistem bilgisi alındı"),
    "current_time": ("Saat sorgusu", "Saat alındı"),
    "list_undo_records": ("Silme günlüğü", "Silme günlüğü okundu"),
}


def test_declared_labels_match_turkish_table() -> None:
    registry = discover_tools()
    for name, (label, done_label) in EXPECTED_TOOL_LABELS.items():
        tool = registry.get(name)
        assert tool is not None, name
        assert (tool.label, tool.done_label) == (label, done_label), name


def test_steps_module_does_not_import_pyside6() -> None:
    module_path = Path(__file__).resolve().parents[1] / "artemis" / "channels" / "desktop"
    source = (module_path / "steps.py").read_text(encoding="utf-8")
    assert "PySide6" not in source
    # Kaynak metni yetmez: modülü temiz bir süreçte içe aktarıp Qt'nin yüklenmediğini
    # de doğrularız.
    probe = (
        "import sys\n"
        "import artemis.channels.desktop.steps\n"
        "loaded = [name for name in sys.modules if name.split('.')[0] == 'PySide6']\n"
        "raise SystemExit(1 if loaded else 0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
