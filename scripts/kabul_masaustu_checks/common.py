"""Masaüstü kabul denetimleri için ortak kurulum, bekleme ve yol temizleme yardımcıları."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from artemis.brain.errors import ProviderError
from artemis.brain.models import Completion, Message
from artemis.brain.streaming import TextStream
from artemis.config.settings import Settings, load_settings
from artemis.i18n import t
from artemis.tools.base import ToolDefinition
from artemis.tools.toast import ToastOutcome

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication, QWidget

    from artemis.channels.desktop.app import DesktopWindow

REPO = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO / "config" / "config.example.yaml"

DEMO_CONFIG_DIR = r"C:\Users\demo\AppData\Roaming\Artemis"
DEMO_REPOS_DIR = r"C:\Users\demo\Desktop\Projeler"
DEMO_SKILLS_DIR = r"C:\Users\demo\AppData\Roaming\Artemis\beceriler"
DEMO_DOWNLOADS_DIR = r"C:\Users\demo\Downloads"
DEMO_DOCS_DIR = r"C:\Users\demo\Documents\Belgeler"
DEMO_PROJECT_DIR = r"C:\Users\demo\Desktop\Projeler\ornek"
DEMO_TEMP_DIR = r"C:\Users\demo\Temp"


class GlobalTimeoutError(TimeoutError):
    """Küresel kabul betiği zaman aşımı dolduğunda yükseltilir."""


def check_deadline(deadline: float | None, stage: str = "") -> None:
    """Küresel bitiş zamanı geçtiyse GlobalTimeoutError yükseltir."""
    if deadline is not None and time.monotonic() >= deadline:
        detail = f" ({stage})" if stage else ""
        raise GlobalTimeoutError(f"Küresel kabul zaman aşımı doldu{detail}")


def remaining_timeout(
    deadline: float | None, local_timeout: float, stage: str = ""
) -> float:
    """Yerel bekleme süresini kalan küresel süreyle sınırlar."""
    check_deadline(deadline, stage)
    if deadline is None:
        return local_timeout
    return min(local_timeout, max(0.05, deadline - time.monotonic()))


def configure_qt_platform(*, offscreen: bool) -> None:
    """PySide6 içe aktarılmadan önce Qt platform eklentisini ayarlar."""
    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        # Offscreen eklentisi Windows yazı tiplerini kendiliğinden bulamaz.
        if sys.platform == "win32":
            os.environ.setdefault(
                "QT_QPA_FONTDIR",
                os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\Fonts",
            )
    else:
        os.environ.pop("QT_QPA_PLATFORM", None)


@dataclass(frozen=True)
class RowResult:
    """Tek bir kabul satırının sonucu, kısa gözlemi ve ürettiği ekran görüntüleri."""

    row_id: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    observation: str
    screenshots: tuple[str, ...] = ()


class RecordingProvider:
    """Ağ ve ücretli model kullanmadan çağrılan araç listesini ve iletileri kaydeden sağlayıcı."""

    def __init__(self, responses: list[Completion] | None = None) -> None:
        self.responses: list[Completion] = list(responses or [])
        self.requests: list[list[Message]] = []
        self.tool_names: list[list[str]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
        stream: TextStream | None = None,
    ) -> Completion:
        del timeout
        self.requests.append([message.model_copy(deep=True) for message in messages])
        self.tool_names.append([tool.name for tool in tools])
        if self.responses:
            completion = self.responses.pop(0).model_copy(deep=True)
        elif responses_empty_default := Completion(
            content="Tamam.",
            provider="fake",
            model="fake-acceptance-model",
        ):
            completion = responses_empty_default
        else:  # pragma: no cover - savunmacı dal
            raise ProviderError("Sahte sağlayıcı yanıt üretemedi")
        if stream is not None:
            for piece in re.findall(r"\S+\s*|\s+", completion.content):
                stream.text(piece)
            if completion.tool_calls:
                stream.tool_call()
        return completion


def _deep_merge(base: dict[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def make_temp_config(
    base: Path, *, overrides: Mapping[str, Any] | None = None
) -> tuple[Path, Path, Path]:
    """Örnek config'ten geçici dizinde ağsız ve izole bir config.yaml oluşturur."""
    cfg_dir = base / "cfg"
    workspace_dir = base / "workspace"
    state_dir = base / "state"
    for folder in (cfg_dir, workspace_dir, state_dir):
        folder.mkdir(parents=True, exist_ok=True)

    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_bytes(EXAMPLE_CONFIG.read_bytes())
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}

    raw["workspace"] = str(workspace_dir)
    raw["state_dir"] = str(state_dir)
    raw["audit_path"] = str(state_dir / "audit.jsonl")
    raw["language"] = "tr"
    raw["persist_memory"] = True
    raw["endpoints"] = {
        "sahte-yerel": {
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": False,
        }
    }
    raw["models"] = {
        "sahte-model": {
            "provider": "openai_compatible",
            "endpoint": "sahte-yerel",
            "model": "fake-acceptance-model",
            "input_per_million": "0",
            "output_per_million": "0",
        }
    }
    raw["primary_model"] = "sahte-model"

    voice = raw.get("voice")
    if not isinstance(voice, dict):
        voice = {}
    # Gerçek küresel Windows kısayollarını ele geçirmemek için boş bırakılır.
    voice["listening_hotkey"] = ""
    raw["voice"] = voice

    memory = raw.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    memory["encrypt_at_rest"] = False
    raw["memory"] = memory

    if overrides:
        raw = _deep_merge(raw, overrides)

    Settings.model_validate(raw)
    cfg_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    load_settings(cfg_path)
    return cfg_path, workspace_dir, state_dir


def install_safety_guards(qsettings_dir: Path) -> None:
    """Gerçek kasa, kayıt defteri, QSettings ve Windows toast erişimini geçersiz kılar."""
    from PySide6.QtCore import QSettings

    import artemis.brain.providers as providers
    import artemis.channels.autostart as autostart_mod
    import artemis.channels.desktop.app as app_mod
    import artemis.channels.desktop.window_events as window_events_mod
    import artemis.channels.telegram.api as telegram_api
    import artemis.tools.credentials as credentials
    import artemis.tools.plugins.code_agent as code_agent_mod

    qsettings_dir.mkdir(parents=True, exist_ok=True)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(qsettings_dir))

    providers.read_secret = lambda *_a, **_kw: None  # type: ignore[assignment]
    telegram_api.read_secret = lambda *_a, **_kw: None  # type: ignore[assignment]
    credentials.read_secret = lambda *_a, **_kw: None  # type: ignore[assignment]
    code_agent_mod.resolve_executable = (  # type: ignore[assignment]
        lambda profile: None if profile.kind == "codex" else shutil.which(profile.command or "")
    )

    def _forbid_registry(*_a: object, **_kw: object) -> None:
        raise RuntimeError("Kabul betiği gerçek kayıt defterine dokunamaz.")

    autostart_mod.WindowsRunKeyStore.read = lambda *_a, **_kw: None  # type: ignore[method-assign]
    autostart_mod.WindowsRunKeyStore.write = _forbid_registry  # type: ignore[method-assign]
    autostart_mod.WindowsRunKeyStore.delete = _forbid_registry  # type: ignore[method-assign]

    def _noop_toast(_title: str, _body: str) -> ToastOutcome:
        return ToastOutcome(status="skipped", message="kabul-test")

    app_mod.show_toast = _noop_toast  # type: ignore[assignment]
    window_events_mod.show_toast = _noop_toast  # type: ignore[attr-defined]


@contextlib.contextmanager
def fake_provider_bridge(provider_factory: Callable[[], RecordingProvider]):
    """Masaüstü köprüsünün gerçek runtime'ı sahte sağlayıcıyla kurmasını sağlar."""
    import artemis.channels.desktop.bridge as bridge_mod
    from artemis.channels.runtime import create_runtime as real_create_runtime

    original = bridge_mod.create_runtime

    def _patched_create_runtime(
        config_path: Path, approve: Any, *, on_record: Any = None, **kwargs: Any
    ) -> Any:
        kwargs.setdefault("provider", provider_factory())
        return real_create_runtime(config_path, approve, on_record=on_record, **kwargs)

    bridge_mod.create_runtime = _patched_create_runtime  # type: ignore[assignment]
    try:
        yield
    finally:
        bridge_mod.create_runtime = original


def wait_until(
    app: QApplication,
    predicate: Callable[[], bool],
    timeout: float = 15.0,
    desc: str = "",
    *,
    deadline: float | None = None,
) -> None:
    """Koşul sağlanana dek Qt olay döngüsünü işletir."""
    limit = remaining_timeout(deadline, timeout, desc)
    end = time.monotonic() + limit
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    if predicate():
        return
    check_deadline(deadline, desc)
    detail = f": {desc}" if desc else ""
    raise TimeoutError(f"GUI durumu zamanında oluşmadı{detail}")


def _window_diag(window: DesktopWindow) -> str:
    return (
        f"worker_ready={window._worker_ready}, busy={window._busy}, "
        f"restart_settings={window._restart_settings}, "
        f"config_restart_pending={window._config_restart_pending}, "
        f"bridge_stopped={window.bridge.stopped}, "
        f"status={window.status_label.text()!r}"
    )


def wait_window_ready(
    app: QApplication,
    window: DesktopWindow,
    timeout: float = 20.0,
    *,
    deadline: float | None = None,
) -> None:
    """İşçinin ve yeniden kurulumun tamamlanıp durumun Hazır olmasını bekler."""
    failed_prefix = t("Başlatılamadı")

    def _is_ready() -> bool:
        status = window.status_label.text()
        if status.startswith(failed_prefix):
            raise RuntimeError(f"DesktopWindow başlatılamadı: {_window_diag(window)}")
        if window.bridge.stopped and not window._restart_settings:
            raise RuntimeError(
                f"DesktopWindow köprüsü beklenmedik şekilde durdu: {_window_diag(window)}"
            )
        return (
            window._worker_ready
            and not window._busy
            and not window._restart_settings
            and not window._config_restart_pending
            and status == t("Hazır")
        )

    try:
        wait_until(
            app,
            _is_ready,
            timeout=timeout,
            desc="DesktopWindow hazır",
            deadline=deadline,
        )
    except GlobalTimeoutError:
        raise
    except TimeoutError as error:
        raise TimeoutError(f"{error} ({_window_diag(window)})") from error


def assert_no_leaked_windows(
    app: QApplication,
    stage: str,
    *,
    max_visible: int = 2,
    max_desktop: int = 0,
) -> None:
    """Kontroller arasında sızan görünür pencere ya da açık DesktopWindow olmadığını doğrular."""
    from artemis.channels.desktop.app import DesktopWindow

    top_levels = list(app.topLevelWidgets())
    visible = [widget for widget in top_levels if widget.isVisible()]
    desktops = [widget for widget in top_levels if isinstance(widget, DesktopWindow)]
    if len(visible) > max_visible or len(desktops) > max_desktop:
        visible_names = [type(widget).__name__ for widget in visible]
        raise AssertionError(
            f"Pencere sızıntısı ({stage}): visible={len(visible)} ({visible_names}), "
            f"desktop={len(desktops)} (sınır visible<={max_visible}, desktop<={max_desktop})"
        )


def open_real_window(
    app: QApplication,
    config_path: Path,
    *,
    width: int = 1280,
    height: int = 780,
    deadline: float | None = None,
) -> DesktopWindow:
    """Geçici config ile tek DesktopWindow açar; hata olursa pencereyi kapatıp sızdırmaz."""
    from artemis.channels.desktop.app import build_window

    check_deadline(deadline, "open_real_window")
    assert_no_leaked_windows(app, "open_real_window öncesi", max_visible=2, max_desktop=0)
    window = build_window(config_path, demo=False, voice=False, app=app)
    try:
        window.resize(width, height)
        window.show()
        # Yüzen pet penceresi ekran görüntüsünü örtmesin.
        window.pet.hide()
        app.processEvents()
        wait_window_ready(app, window, deadline=deadline)
        window.pet.hide()
        app.processEvents()
        assert_no_leaked_windows(app, "open_real_window sonrası", max_visible=2, max_desktop=1)
    except BaseException:
        close_window_cleanly(app, window, timeout=5.0)
        raise
    return window


def close_window_cleanly(
    app: QApplication, window: DesktopWindow, timeout: float = 15.0
) -> None:
    """Açık pencereyi soru diyaloğuna takılmadan ve tüm çocuk widget'larla kapatır."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QDialog

    from artemis.channels.desktop import gui_gc

    try:
        if window.settings_panel.has_unsaved:
            window.settings_panel._set_dirty(False)
        if window._approval_dialog is not None:
            window._approval_dialog.reject()
        modal = app.activeModalWidget()
        if isinstance(modal, QDialog):
            modal.reject()
        if not window.bridge.stopped:
            window._request_exit()
        with contextlib.suppress(TimeoutError):
            wait_until(
                app,
                lambda: window.bridge.stopped,
                timeout=timeout,
                desc="bridge.stopped",
            )
    finally:
        for timer in (
            window._timer,
            window._projects_timer,
            window.settings_panel.file_search._timer,
            window.pet._timer,
            window.pet._click_timer,
        ):
            with contextlib.suppress(RuntimeError):
                timer.stop()
        with contextlib.suppress(RuntimeError):
            window._telegram_service.stop()
        for attr in ("_hotkey", "_dictation_hotkey", "_selection_hotkey"):
            hotkey = getattr(window, attr, None)
            if hotkey is not None:
                with contextlib.suppress(RuntimeError):
                    hotkey.stop()
                setattr(window, attr, None)
        with contextlib.suppress(RuntimeError):
            window.settings_panel.finish_inventory()
        with contextlib.suppress(RuntimeError):
            window.settings_panel.providers.shutdown()
        if window._tray is not None:
            with contextlib.suppress(RuntimeError):
                window._tray.hide()
                window._tray.deleteLater()
            window._tray = None
        if window._temporary is not None:
            with contextlib.suppress(OSError):
                window._temporary.cleanup()
            window._temporary = None
        secondary: list[QWidget] = [
            window.pet._quick,
            window.pet,
            window.overlay,
            window.selection_popup,
        ]
        if window._approval_dialog is not None:
            secondary.append(window._approval_dialog)
        for widget in secondary:
            with contextlib.suppress(RuntimeError):
                widget.hide()
                widget.close()
                widget.deleteLater()
        with contextlib.suppress(RuntimeError):
            window._allow_close = True
            window.hide()
            window.close()
            window.deleteLater()
        for _ in range(3):
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gui_gc.collect_due()


def force_close_all_windows(app: QApplication) -> None:
    """Açık kalmış tüm üst düzey pencere ve diyalogları kapatıp bellekten düşürür."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QDialog

    from artemis.channels.desktop import gui_gc
    from artemis.channels.desktop.app import DesktopWindow

    for widget in list(app.topLevelWidgets()):
        if isinstance(widget, DesktopWindow):
            with contextlib.suppress(RuntimeError):
                close_window_cleanly(app, widget, timeout=5.0)
    for widget in list(app.topLevelWidgets()):
        with contextlib.suppress(RuntimeError):
            if isinstance(widget, QDialog):
                widget.reject()
            widget.hide()
            widget.close()
            widget.deleteLater()
    for _ in range(3):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gui_gc.collect_due()


def select_settings_section(app: QApplication, window: DesktopWindow, title: str) -> None:
    """Ayarlar sekmesine geçer ve verilen bölüm adını seçer."""
    window.tabs.setCurrentWidget(window.settings_scroll)
    panel = window.settings_panel
    names = panel.section_names()
    target = t(title) if t(title) in names else title
    if target not in names:
        raise RuntimeError(f"Ayar bölümü bulunamadı: {title!r} (mevcut: {names})")
    panel.sections.setCurrentRow(names.index(target))
    app.processEvents()


def _replacement_pairs(
    real_roots: Mapping[Path, str] | Iterable[Path] = (),
) -> list[tuple[str, str]]:
    pairs: list[tuple[Path, str]] = []
    if isinstance(real_roots, Mapping):
        for root, replacement in real_roots.items():
            pairs.append((Path(root), replacement))
    else:
        for root in real_roots:
            pairs.append((Path(root), r"C:\Users\demo\kabul"))

    temp_root = Path(tempfile.gettempdir())
    pairs.append((temp_root, DEMO_TEMP_DIR))
    for env_name, replacement in (
        ("LOCALAPPDATA", r"C:\Users\demo\AppData\Local"),
        ("APPDATA", r"C:\Users\demo\AppData\Roaming"),
    ):
        env_val = os.environ.get(env_name)
        if env_val:
            pairs.append((Path(env_val), replacement))
    pairs.append((REPO, r"C:\Users\demo\Desktop\Projeler\artemis"))
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        pairs.append((Path(userprofile), r"C:\Users\demo"))
    pairs.append((Path.home(), r"C:\Users\demo"))

    expanded: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path_obj, replacement in pairs:
        variants = {str(path_obj), path_obj.as_posix()}
        with contextlib.suppress(OSError):
            resolved = path_obj.resolve()
            variants.update({str(resolved), resolved.as_posix()})
        for raw in variants:
            cleaned = raw.rstrip("\\/")
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                expanded.append((cleaned, replacement))
    expanded.sort(key=lambda pair: len(pair[0]), reverse=True)
    return expanded


def sanitize_text_paths(
    text: str, real_roots: Mapping[Path, str] | Iterable[Path] = ()
) -> str:
    """Metindeki gerçek kullanıcı/geçici/depo yollarını kurmaca demo yollarla değiştirir."""
    result = text
    for needle, replacement in _replacement_pairs(real_roots):
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        result = pattern.sub(lambda _m, rep=replacement: rep, result)
    result = re.sub(
        r"artemis[-_]kabul[-_][A-Za-z0-9_-]+",
        "artemis-demo",
        result,
        flags=re.IGNORECASE,
    )
    return result


def _forbidden_path_needles(real_roots: Mapping[Path, str] | Iterable[Path]) -> list[str]:
    paths: list[Path] = [Path.home(), Path(tempfile.gettempdir()), REPO]
    for env_name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        val = os.environ.get(env_name)
        if val:
            paths.append(Path(val))
    if isinstance(real_roots, Mapping):
        paths.extend(Path(p) for p in real_roots)
    else:
        paths.extend(Path(p) for p in real_roots)
    needles: set[str] = set()
    for path_obj in paths:
        for candidate in (str(path_obj), path_obj.as_posix()):
            trimmed = candidate.rstrip("\\/")
            if len(trimmed) >= 4:
                needles.add(trimmed.casefold())
        with contextlib.suppress(OSError):
            resolved = path_obj.resolve()
            for candidate in (str(resolved), resolved.as_posix()):
                trimmed = candidate.rstrip("\\/")
                if len(trimmed) >= 4:
                    needles.add(trimmed.casefold())
    return sorted(needles, key=len, reverse=True)


def _visible_widget_texts(root: QWidget) -> list[str]:
    from PySide6.QtWidgets import (
        QLabel,
        QLineEdit,
        QListWidget,
        QPlainTextEdit,
        QTextBrowser,
    )

    texts: list[str] = []
    for widget in [root, *root.findChildren(QLabel)]:
        if isinstance(widget, QLabel) and widget.isVisible():
            texts.append(widget.text())
    for widget in root.findChildren(QLineEdit):
        if widget.isVisible():
            texts.append(widget.text())
            texts.append(widget.placeholderText())
    for widget in root.findChildren(QPlainTextEdit):
        if widget.isVisible():
            texts.append(widget.toPlainText())
    for widget in root.findChildren(QTextBrowser):
        if widget.isVisible():
            texts.append(widget.toPlainText())
    for widget in root.findChildren(QListWidget):
        if widget.isVisible():
            for row in range(widget.count()):
                item = widget.item(row)
                if item is not None:
                    texts.append(item.text())
    return texts


def grab_neutralized(
    app: QApplication,
    widget: QWidget,
    out_dir: Path,
    filename: str,
    *,
    real_roots: Mapping[Path, str] | Iterable[Path] = (),
    overrides: Mapping[Any, str] | None = None,
) -> Path:
    """Widget'taki gerçek yolları geçici olarak maskeler, PNG yakalar ve metinleri geri yükler."""
    from PySide6.QtWidgets import (
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QPlainTextEdit,
        QTextBrowser,
    )

    from artemis.channels.desktop.page_kit import PathHint

    restorers: list[Callable[[], None]] = []
    try:
        for target, replacement in (overrides or {}).items():
            if isinstance(target, PathHint):
                orig_full = target.full_text
                orig_tip = target.toolTip()
                target.set_full_text(replacement, replacement)
                restorers.append(
                    lambda t_w=target, f=orig_full, tip=orig_tip: t_w.set_full_text(f, tip)
                )
            elif isinstance(target, (QLabel, QLineEdit)):
                orig = target.text()
                target.setText(replacement)
                restorers.append(lambda w=target, o=orig: w.setText(o))
            elif isinstance(target, QListWidgetItem):
                orig = target.text()
                target.setText(replacement)
                restorers.append(lambda it=target, o=orig: it.setText(o))

        # Bilinen özel yol etiketleri
        panel = getattr(widget, "settings_panel", widget)
        about = getattr(panel, "about_section", None)
        if about is not None and hasattr(about, "config_label"):
            orig_cfg = about.config_label.text()
            about.config_label.setText(t("Ayar klasörü: {path}").format(path=DEMO_CONFIG_DIR))
            restorers.append(lambda lbl=about.config_label, o=orig_cfg: lbl.setText(o))
        repos_dir = getattr(panel, "repos_dir", None)
        if isinstance(repos_dir, QLineEdit):
            orig_ph = repos_dir.placeholderText()
            repos_dir.setPlaceholderText(DEMO_REPOS_DIR)
            restorers.append(lambda ed=repos_dir, o=orig_ph: ed.setPlaceholderText(o))

        for hint_widget in widget.findChildren(PathHint):
            orig_full = hint_widget.full_text
            orig_tip = hint_widget.toolTip()
            clean_full = sanitize_text_paths(orig_full, real_roots)
            if clean_full != orig_full:
                hint_widget.set_full_text(clean_full, sanitize_text_paths(orig_tip, real_roots))
                restorers.append(
                    lambda hw=hint_widget, f=orig_full, tip=orig_tip: hw.set_full_text(f, tip)
                )

        for label in widget.findChildren(QLabel):
            orig = label.text()
            clean = sanitize_text_paths(orig, real_roots)
            if clean != orig:
                label.setText(clean)
                restorers.append(lambda lbl=label, o=orig: lbl.setText(o))

        for edit in widget.findChildren(QLineEdit):
            orig_t = edit.text()
            clean_t = sanitize_text_paths(orig_t, real_roots)
            if clean_t != orig_t:
                edit.setText(clean_t)
                restorers.append(lambda ed=edit, o=orig_t: ed.setText(o))
            orig_p = edit.placeholderText()
            clean_p = sanitize_text_paths(orig_p, real_roots)
            if clean_p != orig_p:
                edit.setPlaceholderText(clean_p)
                restorers.append(lambda ed=edit, o=orig_p: ed.setPlaceholderText(o))

        for list_widget in widget.findChildren(QListWidget):
            for row in range(list_widget.count()):
                item = list_widget.item(row)
                if item is None:
                    continue
                orig_item = item.text()
                clean_item = sanitize_text_paths(orig_item, real_roots)
                if clean_item != orig_item:
                    item.setText(clean_item)
                    restorers.append(lambda it=item, o=orig_item: it.setText(o))

        for plain in widget.findChildren(QPlainTextEdit):
            orig_plain = plain.toPlainText()
            clean_plain = sanitize_text_paths(orig_plain, real_roots)
            if clean_plain != orig_plain:
                plain.setPlainText(clean_plain)
                restorers.append(lambda pl=plain, o=orig_plain: pl.setPlainText(o))

        for browser in widget.findChildren(QTextBrowser):
            orig_html = browser.toHtml()
            clean_html = sanitize_text_paths(orig_html, real_roots)
            if clean_html != orig_html:
                browser.setHtml(clean_html)
                restorers.append(lambda br=browser, o=orig_html: br.setHtml(o))

        app.processEvents()
        forbidden = _forbidden_path_needles(real_roots)
        for visible in _visible_widget_texts(widget):
            folded = visible.casefold()
            for needle in forbidden:
                if needle in folded:
                    raise AssertionError(
                        f"Ekran görüntüsünde gerçek yol sızıntısı ({filename}): {needle!r}"
                    )

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / filename
        pixmap = widget.grab()
        if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            raise AssertionError(f"Boş ekran görüntüsü alındı: {filename}")
        if not pixmap.save(str(out_path), "PNG"):
            raise OSError(f"Ekran görüntüsü yazılamadı: {out_path}")
        return out_path
    finally:
        for restore in reversed(restorers):
            restore()
        app.processEvents()


def subprocess_env(tmp_dir: Path, *, offscreen: bool = False) -> dict[str, str]:
    """Alt süreçler için gerçek kullanıcı klasörlerinden ve sırlardan arındırılmış ortam üretir."""
    env = dict(os.environ)
    for var in (
        "QT_QPA_PLATFORM",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "ARTEMIS_TELEGRAM_BOT_TOKEN",
        "ARTEMIS_CALENDAR_ICS_TOKEN",
        "ARTEMIS_MAIL_PASSWORD",
    ):
        env.pop(var, None)
    if offscreen:
        env["QT_QPA_PLATFORM"] = "offscreen"
        if sys.platform == "win32":
            env.setdefault(
                "QT_QPA_FONTDIR",
                os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\Fonts",
            )
    appdata = tmp_dir / "appdata"
    localappdata = tmp_dir / "localappdata"
    appdata.mkdir(parents=True, exist_ok=True)
    localappdata.mkdir(parents=True, exist_ok=True)
    env["APPDATA"] = str(appdata)
    env["LOCALAPPDATA"] = str(localappdata)
    env["ARTEMIS_CREDENTIAL_PREFIX"] = "ArtemisKabulTest"
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{existing_pp}" if existing_pp else str(REPO)
    env["PYTHONUTF8"] = "1"
    return env


def remove_tree(root: Path) -> None:
    """Windows dosya kilidi gecikmelerine karşı sınırlı yeniden denemeli klasör silme."""
    if not root.exists():
        return
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(root)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.1 * (attempt + 1))
    raise OSError(f"Geçici klasör silinemedi ({root}): {last_error}")
