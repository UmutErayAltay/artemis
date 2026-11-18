import ctypes
import json
import os
import struct
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _synthetic_png(width: int, height: int) -> bytes:
    """Büyük piksel tamponu ayırmadan geçerli PNG baytları üretir."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _write_config(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        json.dumps(
            {
                "state_dir": str(state_dir),
                "primary_model": "chat-m",
                "endpoints": {
                    "local": {"base_url": "http://127.0.0.1:1234/v1", "api_key": False}
                },
                "models": {
                    "chat-m": {
                        "provider": "openai_compatible",
                        "endpoint": "local",
                        "model": "m-chat",
                        "input_per_million": "0",
                        "output_per_million": "0",
                    },
                    "img-m": {
                        "provider": "openai_compatible",
                        "endpoint": "local",
                        "model": "m-img",
                        "kind": "image",
                        "input_per_million": "0",
                        "output_per_million": "0",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _make_pet_dir(root: Path, pet_id: str, display_name: str, description: str = "") -> Path:
    from PySide6.QtGui import QImage

    folder = root / pet_id
    folder.mkdir(parents=True, exist_ok=True)
    sheet = QImage(1536, 1872, QImage.Format.Format_ARGB32)
    sheet.fill(0xFF336699)
    assert sheet.save(str(folder / "spritesheet.png"), "PNG")
    (folder / "pet.json").write_text(
        json.dumps(
            {
                "id": pet_id,
                "displayName": display_name,
                "description": description,
                "spritesheetPath": "spritesheet.png",
            }
        ),
        encoding="utf-8",
    )
    return folder


def _pet_widget(tmp_path: Path, *, reduced_motion: bool = True):
    from PySide6.QtCore import QSettings

    from artemis.channels.desktop.pet import PetWidget

    settings = QSettings(str(tmp_path / "pet.ini"), QSettings.Format.IniFormat)
    return PetWidget(
        on_toggle_voice=lambda: None,
        on_show_panel=lambda: None,
        on_quick=lambda: None,
        on_approval=lambda: None,
        on_quit=lambda: None,
        on_submit=lambda _text: True,
        settings=settings,
        reduced_motion=reduced_motion,
        pets_dir=tmp_path / "pets",
    )


def test_item1_hotkey_peek_message_before_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from artemis.channels.desktop import hotkey

    events: list[str] = []
    gh = hotkey.GlobalHotkey("ctrl+space", lambda: None)
    orig_set = gh._ready.set

    def tracked_set() -> None:
        events.append("ready.set")
        orig_set()

    monkeypatch.setattr(gh._ready, "set", tracked_set)
    monkeypatch.setattr(hotkey.sys, "platform", "win32")

    def fake_peek(*_args: object) -> int:
        events.append("PeekMessageW")
        return 0

    def fake_get(*_args: object) -> int:
        events.append("GetMessageW")
        assert gh._ready.is_set()
        return 0

    fake_windll = SimpleNamespace(
        kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 1234),
        user32=SimpleNamespace(
            RegisterHotKey=lambda *_a: 1,
            PeekMessageW=fake_peek,
            GetMessageW=fake_get,
            UnregisterHotKey=lambda *_a: 1,
        ),
    )
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    gh._run()
    assert events == ["PeekMessageW", "ready.set", "GetMessageW"]


def test_item2_parse_hotkey_rejects_bare_key() -> None:
    from artemis.channels.desktop.hotkey import parse_hotkey

    # Yazı üreten tuşlar tek başına reddedilir; F tuşları ve Esc (durdurma kısayolu) kalır.
    for bare in ("space", "a", "7"):
        with pytest.raises(ValueError):
            parse_hotkey(bare)
    flags, vk = parse_hotkey("ctrl+space")
    assert flags != 0 and vk == 0x20


def test_item3_click_after_quick_deactivate_does_not_reopen(tmp_path: Path) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    _app()
    pet = _pet_widget(tmp_path)
    try:
        pet._quick.open()
        assert pet._quick.isVisible()

        QApplication.sendEvent(pet._quick, QEvent(QEvent.Type.WindowDeactivate))
        assert not pet._quick.isVisible()

        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(60, 60),
            QPointF(60, 60),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(60, 60),
            QPointF(60, 60),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        pet.mousePressEvent(press)
        pet.mouseReleaseEvent(release)
        pet._dispatch_clicks()
        assert not pet._quick.isVisible()

        pet.mousePressEvent(press)
        pet.mouseReleaseEvent(release)
        pet._dispatch_clicks()
        assert pet._quick.isVisible()
    finally:
        pet.close()


def test_item4_open_menu_does_not_accumulate_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QMenu

    _app()
    pet = _pet_widget(tmp_path)
    # Sınıf düzeyinde exec taklidi PySide6'da C++ çağrısını yakalamayabilir (modal menü testi
    # kilitler); bu yüzden kurulan her menünün kendi exec'i etkisizleştirilir.
    real_build = pet.build_menu

    def build_without_exec() -> QMenu:
        menu = real_build()
        menu.exec = lambda *_args: None  # type: ignore[method-assign]
        return menu

    monkeypatch.setattr(pet, "build_menu", build_without_exec)
    try:
        for _ in range(5):
            pet._open_menu(QPoint(10, 10))
        QApplication.processEvents()
        assert len(pet.findChildren(QMenu)) == 0
    finally:
        pet.close()


def test_item5_reduced_motion_static_skips_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app()
    pet = _pet_widget(tmp_path, reduced_motion=True)
    updates: list[int] = []
    monkeypatch.setattr(pet, "update", lambda: updates.append(1))
    try:
        pet._last = time.monotonic()
        for _ in range(3):
            pet._on_timer()
        assert updates == []

        pet.set_voice("idle")
        for _ in range(100):
            pet.tick(0.05)
        updates.clear()
        pet._last = time.monotonic()
        for _ in range(3):
            pet._on_timer()
        assert updates == []
    finally:
        pet.close()


def test_item7_cancelled_base_call_does_not_overwrite_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtGui import QImage

    from artemis.channels.desktop import pet_image_pipeline as pipe
    from artemis.channels.desktop.pet_creator_panel import CreatorPanel, ImageCall

    _app()
    cfg = _write_config(tmp_path)
    panel = CreatorPanel(cfg, tmp_path / "pets")
    try:
        old_base = QImage(16, 16, QImage.Format.Format_ARGB32)
        panel._base = old_base
        panel._base_png = b"old-png"

        call = ImageCall(cfg, "img-m", lambda _c, _g: None)
        call.cancel_requested = True
        panel._image_call = call
        new_base = QImage(8, 8, QImage.Format.Format_ARGB32)
        panel._on_base((new_base, b"new-png"), "")
        assert panel._base is old_base
        assert panel._base_png == b"old-png"

        captured: list[object] = []

        def fake_start(_alias, _calls, work, _on_done) -> bool:
            captured.append(work)
            return True

        monkeypatch.setattr(panel, "_start_image", fake_start)
        monkeypatch.setattr(pipe, "make_base", lambda _t, _g: (new_base, b"new-png"))
        monkeypatch.setattr(pipe, "edit_base", lambda _t, _b, _g: (new_base, b"new-png"))
        panel._send_image("tilki", "img-m")
        fake_call = SimpleNamespace(cancel_requested=True)
        with pytest.raises(pipe.Cancelled):
            captured[0](fake_call, lambda *_a: b"")  # type: ignore[index]
    finally:
        panel.close()


def test_item9_on_animated_error_stops_timer_and_clears_frames(tmp_path: Path) -> None:
    from PySide6.QtGui import QImage

    from artemis.channels.desktop.pet_creator_panel import CreatorPanel

    _app()
    cfg = _write_config(tmp_path)
    panel = CreatorPanel(cfg, tmp_path / "pets")
    try:
        panel._on_row(0, [QImage(12, 12, QImage.Format.Format_ARGB32)])
        assert panel._timer.isActive()
        assert len(panel._frames) == 1

        panel._on_animated(object(), "")
        assert not panel._timer.isActive()
        assert panel._frames == []

        panel._on_row(1, [QImage(12, 12, QImage.Format.Format_ARGB32)])
        assert panel._timer.isActive()
        panel._on_animated(None, "İptal edildi.")
        assert not panel._timer.isActive()
        assert panel._frames == []
    finally:
        panel.close()


def test_item10_decode_rejects_oversize_before_full_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtGui import QImageReader

    from artemis.channels.desktop import pet_image_pipeline as pipe

    _app()
    oversize = _synthetic_png(pipe.MAX_SIDE + 1, 1)
    read_calls: list[int] = []
    orig_read = QImageReader.read

    def guarded_read(self: QImageReader):
        read_calls.append(1)
        return orig_read(self)

    monkeypatch.setattr(QImageReader, "read", guarded_read)
    with pytest.raises(pipe.PetImageError):
        pipe.decode(oversize, pipe.BASE_SIZE)
    assert read_calls == []

    small = pipe.decode(_synthetic_png(2, 2), pipe.BASE_SIZE)
    assert (small.width(), small.height()) == (2, 2)


def test_item11_pet_labels_render_plain_text(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    from artemis.channels.desktop.pet_section import PetSection, _ElidedLabel

    _app()
    pets_dir = tmp_path / "pets"
    _make_pet_dir(pets_dir, "evil-pet", "<b>Kalin</b>", "<i>Aciklama</i>")
    section = PetSection(pets_dir, tmp_path / "codex")
    try:
        section.set_current("evil-pet")
        assert section.hero_name.textFormat() == Qt.TextFormat.PlainText
        assert section.hero_name.text() == "<b>Kalin</b>"
        assert "<i>" not in section.hero_image.toolTip()

        elided = _ElidedLabel("<b>Kalin</b>")
        elided.resize(400, 30)
        elided._elide()
        assert elided.textFormat() == Qt.TextFormat.PlainText
        assert elided.text() == "<b>Kalin</b>"
    finally:
        section.close()


def test_item12_no_redundant_refresh_or_hero_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artemis.channels.desktop.pet_section import PetSection

    _app()
    pets_dir = tmp_path / "pets"
    _make_pet_dir(pets_dir, "pet-a", "Pet A")
    _make_pet_dir(pets_dir, "pet-b", "Pet B")
    section = PetSection(pets_dir, tmp_path / "codex")
    try:
        refresh_calls = 0
        orig_refresh = section.refresh

        def tracked_refresh() -> None:
            nonlocal refresh_calls
            refresh_calls += 1
            orig_refresh()

        monkeypatch.setattr(section, "refresh", tracked_refresh)
        section._creator_installed("pet-a")
        assert refresh_calls == 1

        hero_calls = 0
        orig_hero = section._update_hero

        def tracked_hero() -> None:
            nonlocal hero_calls
            hero_calls += 1
            orig_hero()

        monkeypatch.setattr(section, "_update_hero", tracked_hero)
        section.listing.setCurrentRow(2)
        section.listing.setCurrentRow(0)
        assert hero_calls == 0
    finally:
        section.close()


def test_item14_answer_cleared_on_submit_and_selection_change() -> None:
    from artemis.channels.desktop.projects_panel import ProjectsPanel
    from artemis.tools.base import ToolResult

    _app()
    panel = ProjectsPanel(demo=False)
    try:
        status_payload = {
            "projects": ["demo"],
            "drafts": [],
            "jobs": [
                {
                    "job_id": "j1",
                    "question_id": "q1",
                    "status": "waiting",
                    "project": "demo",
                    "spec": {"project": "demo", "task": "t1", "scope": [], "acceptance": []},
                },
                {
                    "job_id": "j2",
                    "question_id": "q2",
                    "status": "waiting",
                    "project": "demo",
                    "spec": {"project": "demo", "task": "t2", "scope": [], "acceptance": []},
                },
            ],
        }
        panel.accept_result(
            "code_agent_status",
            ToolResult(success=True, status="ok", message="Tamam", data=status_payload),
        )
        panel.answer.setPlainText("cevap 1")

        # Aynı iş seçiliyken durum yoklaması yazılan cevabı silmemeli.
        updated = {**status_payload, "drafts": [{"spec_id": "d1", "project": "demo", "task": "d"}]}
        panel.accept_result(
            "code_agent_status",
            ToolResult(success=True, status="ok", message="Tamam", data=updated),
        )
        assert panel.answer.toPlainText() == "cevap 1"

        # Farklı işe geçince cevap kutusu temizlenmeli.
        panel.items.setCurrentRow(2)
        assert panel.answer.toPlainText() == ""

        panel.answer.setPlainText("cevap 2")
        panel.accept_result(
            "code_agent_answer",
            ToolResult(success=True, status="ok", message="Gönderildi"),
        )
        assert panel.answer.toPlainText() == ""
    finally:
        panel.close()


def test_items15_16_parallel_count_loads_exact_saved_values(tmp_path: Path) -> None:
    from PySide6.QtCore import QSettings

    from artemis.channels.desktop.provider_layout import PARALLEL_CHOICES
    from artemis.channels.desktop.provider_section import ProviderSection

    _app()
    cfg = _write_config(tmp_path)
    for count in PARALLEL_CHOICES:
        ini_dir = tmp_path / f"ini_{count}"
        ini_dir.mkdir()
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(ini_dir))
        writer = QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "model_test"
        )
        writer.setValue("parallel_count", count)
        writer.sync()

        section = ProviderSection(cfg, demo=False)
        try:
            assert section.parallel_count.currentData() == count
            reloaded = QSettings(
                QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "model_test"
            )
            assert int(reloaded.value("parallel_count")) == count  # type: ignore[arg-type]
        finally:
            section.close()
