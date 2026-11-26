"""Ana pencerenin durum çubuğu, ses durumu ve otomatik onay kipi (DesktopWindow'dan ayrıldı).

Durum DesktopWindow'dadır; burası `window` üzerindeki widget'ları günceller. Pencerenin aynı
adlı (başında alt çizgi) yöntemleri bu işlevlere ince sarmalayıcıdır, böylece testler ve sinyal
bağlantıları örnek üzerinden çalışmaya devam eder.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox, QPushButton

from artemis.channels.desktop.chat_widgets import _VOICE_LABELS
from artemis.channels.desktop.settings_io import save_computer_mode
from artemis.channels.desktop.window_layout import fit_topbar
from artemis.channels.desktop.window_theme import STATUS_TONES, dot_style, refresh_tones
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow


def sync_auto_controls(window: DesktopWindow, on: bool) -> None:
    # İki denetim de sinyalsiz eşitlenir: düğme ve ayar kutusu aynı kipi göstermeli.
    for control in (window.settings_panel.computer_auto, window.auto_button):
        control.blockSignals(True)
        control.setChecked(on)
        control.blockSignals(False)
    window._show_auto_state(on)


def toggle_auto_mode(window: DesktopWindow, checked: bool) -> None:
    """Üst çubuktaki düğme: kip anında değişir ve config'e yazılır."""
    mode = "auto" if checked else "accept"
    if checked and not window._demo and not window._confirm_auto_mode():
        window._sync_auto_controls(False)
        return
    shown = checked
    if not window._demo:
        saved, applied = True, False
        try:
            save_computer_mode(window._config_path, mode)
        except (OSError, ValueError):
            saved = False
        # Kapatma güvenli kip (accept) içindir; disk yazılmasa bile runtime kapanır.
        if saved or not checked:
            applied = window.bridge.set_computer_mode(mode)
        if checked and (not saved or not applied):
            if saved and not applied:
                # Uygulanamayan auto diskte kalmasın; geri yazılamazsa durum yine hata gösterir.
                with contextlib.suppress(OSError, ValueError):
                    save_computer_mode(window._config_path, "accept")
            msg = t("Kip kaydedilemedi") if not saved else t("Otomatik onay uygulanamadı")
            window._set_status(msg, "error")
            shown = False
        elif not checked:
            if not saved or not applied:
                msg = (
                    t("Kip kapatıldı ama kaydedilemedi") if applied
                    else t("Kip çalışma zamanına uygulanamadı")
                )
                window._set_status(msg, "error")
            shown = not applied
        if saved and applied:
            window._append_chat(
                "Sistem", t("Bilgisayar kullanımı kipi: {mode}.").format(mode=mode)
            )
    window._sync_auto_controls(shown)


def auto_mode_box(window: DesktopWindow) -> tuple[QMessageBox, QPushButton]:
    """Uyarı kutusu ve "aç" düğmesi; ayrı kurulur ki README görüntüsü bloklamadan alınsın."""
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(t("Otomatik onayı aç?"))
    box.setText(t(
        "Otomatik onayda Artemis ekranı okur, tıklar ve yazar; hassas adımları da "
        "sana sormadan yapar. Esc her an durdurur."
    ))
    box.setInformativeText(t(
        "Yanlış anlaşılan bir komut ya da ekrandaki zararlı bir metin, istemediğin bir "
        "işlemi tetikleyebilir; web okuduktan sonra bağlantılar da sorulmadan açılır. "
        "Terminal, Artemis ve parola yöneticisi yine engelli."
    ))
    yes = box.addButton(t("Riski anlıyorum, aç"), QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(t("Vazgeç"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    return box, yes


def confirm_auto_mode(window: DesktopWindow) -> bool:
    """Otomatik kip bilinçli açılır: varsayılan cevap hayır (kural 2'nin ruhu)."""
    box, yes = window._auto_mode_box()
    box.exec()
    return box.clickedButton() is yes


def show_auto_state(window: DesktopWindow, on: bool) -> None:
    """Simge anahtarı metin taşımaz; durum erişilebilir açıklamada ve ipucunda okunur."""
    window.auto_button.setAccessibleDescription(
        t("Oto onay açık") if on else t("Oto onay kapalı")
    )
    mode = t("otomatik") if on else t("her işlemde sor")
    window.auto_button.setToolTip(t(
        "Onay: {mode}. Oto onay açıkken bilgisayar kullanımı adımları sormadan çalışır; "
        "Esc yine durdurur. Değişiklik hemen geçerli, Uygula gerekmez."
    ).format(mode=mode))


def show_continuous(window: DesktopWindow, on: bool) -> None:
    window._continuous = on
    box = window.settings_panel.continuous_mode
    box.blockSignals(True)
    box.setChecked(on)
    box.blockSignals(False)
    window._show_voice_state(window._voice_state)


def set_status(window: DesktopWindow, text: str, tone: str) -> None:
    # Durum düz metindir; ton yalnız yanındaki noktanın rengini belirler.
    window._status_full = text
    window.status_label.setText(text)
    window._status_tone = tone
    window.status_dot.setStyleSheet(dot_style(window._token(STATUS_TONES[tone])))
    window.status_pill.adjustSize()
    fit_topbar(window)


def show_voice_state(window: DesktopWindow, state: str) -> None:
    window._voice_state = state
    if state == "off":
        window._continuous = False
    text = t(_VOICE_LABELS.get(state, _VOICE_LABELS["off"]))
    if window._continuous:
        text += " · " + t("Kesintisiz")
    window.voice_state.setText(text)
    window.overlay.voice_state.setText(text)
    on = state in {"preparing", "listening", "awaiting_command", "thinking", "speaking"}
    window._describe_voice(text, on)
    refresh_tones(window)
    window.pet.set_voice(state)
    if state == "off":
        window.settings_panel.input_level.setValue(0)


def describe_voice(window: DesktopWindow, text: str, on: bool) -> None:
    """Üst çubuk ve yazma alanındaki ses anahtarı aynı durumu ve açıklamayı taşır."""
    if window._demo:
        tip = t("Demo modunda ses yok")
    elif on:
        tip = t("{text} · kapatmak için tıkla").format(text=text)
    else:
        tip = t("{text} · açmak için tıkla; mikrofonu yalnız sen açarsın").format(text=text)
    for button in (window.voice_button, window.composer_voice):
        button.setChecked(on)
        button.setAccessibleDescription(text)
        button.setToolTip(tip)


def composer_voice_clicked(window: DesktopWindow) -> None:
    # Yazma alanındaki simge üst çubuktakiyle aynı işleyiciyi çalıştırır.
    window.voice_button.click()
    window.composer_voice.setChecked(window.voice_button.isChecked())
