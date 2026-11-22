"""Ana pencerenin olay, onay, işlem kartı ve rutin bildirimi işleri (DesktopWindow'dan ayrıldı).

Pencere durumu `window` üzerindedir; burası yalnız köprü olaylarını ona uygular.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from artemis.channels.desktop import themes
from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.channels.desktop.bridge import (
    Activity,
    ActivityGap,
    Approval,
    BridgeEvent,
    Failed,
    Finished,
    Proactive,
    State,
    ToolFinished,
    VoiceCommand,
)
from artemis.channels.desktop.budget_bar import refresh_budget
from artemis.channels.desktop.chat_widgets import OperationCard, StepBlock, _TurnSteps
from artemis.channels.desktop.pet import level_from_message
from artemis.channels.desktop.projects_panel import status_label
from artemis.channels.desktop.steps import step_line
from artemis.channels.desktop.text_viewer import open_text_file
from artemis.config.settings import load_settings
from artemis.config.work_profile import should_suppress_toast
from artemis.i18n import t
from artemis.proactive.daily_summary import VAULT_TEXT_LIMIT, parse_day, summary_path
from artemis.tools.plugins.memory import AppendArguments
from artemis.tools.plugins.routines import RoutineNameArguments

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow

# Adım blokları yalnız son birkaç isteğin canlı bağlantısını tutar.
_STEP_TURNS_KEPT = 8
_STEP_EVENTS_PER_TURN = 200


def handle_event(window: DesktopWindow, event: BridgeEvent) -> None:
    if isinstance(event, State):
        handle_state(window, event)
        return
    if isinstance(event, VoiceCommand):
        window._active_request = event.request_id
        window._busy = True
        window._begin_card(event.request_id, event.text)
        window._append_chat("Siz (ses)", event.text)
        window._set_status(t("Çalışıyor…"), "running")
        window._set_controls()
        window.overlay.set_busy(True)
        return
    if isinstance(event, Approval):
        show_approval(window, event)
        return
    if isinstance(event, ToolFinished):
        if event.request_id == window._active_request:
            dismiss_approval_for(window, event.request_id)
            if event.tool_name.startswith("routine_"):
                window.settings_panel.routines.accept_result(event.tool_name, event.result)
                window._append_chat("Artemis", event.result.message)
                return
            if event.tool_name == "research_cancel":
                window.automation_panel.accept_result(event.tool_name, event.result)
                window._append_chat("Artemis", event.result.message)
                return
            panel = (window.projects_panel if event.tool_name.startswith("code_agent_")
                     else window.data_panel)
            panel.accept_result(event.tool_name, event.result)
            # Silinen geçmişin balonları ekranda kalırsa kullanıcı silmenin başarısız
            # olduğunu sanır; "Yeni sohbet" ile aynı temizlik yapılır.
            if (
                event.tool_name == "conversation_history"
                and event.result.success
                and event.result.data.get("action") == "delete"
            ):
                clear_chat_view(window)
        return
    if isinstance(event, Finished):
        if event.request_id == window._active_request:
            dismiss_approval_for(window, event.request_id)
            window._append_chat(
                "Artemis", event.result.text, step_block_for(window, event.request_id)
            )
            window.pet.answer(event.result.text)
            window.overlay.status.setText(t("Yanıt hazır"))
        return
    if isinstance(event, Failed):
        if not event.request_id:
            window._set_status(
                t("Başlatılamadı · {error_type}").format(error_type=event.error_type), "error"
            )
            window._append_chat("Artemis", event.message)
        elif event.request_id == window._active_request:
            dismiss_approval_for(window, event.request_id)
            window._ops.fail(event.request_id)
            window._render_card(event.request_id)
            window._append_chat(
                "Artemis",
                t("İstek tamamlanamadı ({error_type}).").format(error_type=event.error_type),
                step_block_for(window, event.request_id),
            )
            window.pet.flash_error()
        return
    if isinstance(event, Proactive):
        # Kendiliğinden mesaj ayrı konuşmacıyla görünür; modelin geçmişine eklenmez.
        window._append_chat("Artemis (kendiliğinden)", f"{event.title}\n{event.body}")
        window.pet.answer(event.body)
        if event.routine or event.suggest or event.summary_day:
            show_routine_notice(window, 
                event.routine, event.body, event.suggest, event.summary_day
            )
        return
    if isinstance(event, ActivityGap):
        window._ops.dropped += event.dropped_count
        window.activity_notice.setText(
            t(
                "{dropped} eski etkinlik görünümden düştü; kartlar eksik olabilir."
            ).format(dropped=window._ops.dropped)
        )
        window.activity_notice.show()
        return
    if isinstance(event, Activity):
        window._pet_activity(event)
        feed_card(window, event)
        collect_step(window, event)


def _ready_model(message: str) -> str | None:
    """Açılış hazır mesajından (TR ya da etkin dil) model adını ayıklar."""
    if message.startswith("Hazır · "):
        return message.removeprefix("Hazır · ")
    prefix, sep, suffix = t("Hazır · {model}").partition("{model}")
    if sep and message.startswith(prefix) and (not suffix or message.endswith(suffix)):
        end = len(message) - len(suffix) if suffix else len(message)
        return message[len(prefix) : end]
    return None


def handle_state(window: DesktopWindow, event: State) -> None:
    if event.request_id is not None and event.request_id != window._active_request:
        return
    if event.state == "ready":
        if event.request_id is None and not window._busy:
            model_name = _ready_model(event.message)
            if model_name is not None:
                window._show_model(model_name)
        if event.request_id is not None or not window._busy:
            window._worker_ready = True
            window._set_status(t("Hazır"), "ready")
            window._busy = False
            window._active_request = None
            window._set_controls()
            window.overlay.set_busy(False, t("Hazır"))
        if window._voice_on_ready and window._worker_ready:
            window._voice_on_ready = False
            window.bridge.start_voice()
        if event.request_id is not None:
            dismiss_approval_for(window, event.request_id)
        if event.request_id is None and getattr(window, "_work_profile", "work") != "work":
            window.bridge.set_work_profile(window._work_profile)
        refresh_budget(window)
    elif event.state == "running":
        window._busy = True
        window._set_status(t("Çalışıyor…"), "running")
        window._set_controls()
        window.overlay.set_busy(True)
    elif event.state == "computer":
        # Bilgisayar kullanımı oturumu (ADR 0039): ne yaptığı ve nasıl durdurulacağı
        # hep görünür; boş metin oturum bitti demektir, normal duruma dönülür.
        if event.message:
            line = t(
                "Bilgisayarı kullanıyorum · {message} · durdurmak için Esc"
            ).format(message=event.message)
            window._set_status(line, "running")
            window.overlay.set_busy(True, line)
        elif window._busy:
            window._set_status(t("Çalışıyor…"), "running")
            window.overlay.set_busy(True)
        else:
            window._set_status(t("Hazır"), "ready")
    elif event.state.startswith("selection_"):
        window._show_selection(event.state.removeprefix("selection_"), event.message)
    elif event.state.startswith("dictation_"):
        window._show_dictation(event.state.removeprefix("dictation_"), event.message)
    elif event.state == "cleanup_pending":
        window._busy = True
        window._set_status(t("Önceki işlem temizleniyor…"), "running")
        window.overlay.set_busy(True, t("Temizleniyor…"))
        window._set_controls()
    elif event.state == "voice":
        window._show_voice_state(event.message)
    elif event.state == "voice_mode":
        window._show_continuous(event.message == "on")
    elif event.state == "voice_level":
        level = level_from_message(event.message)
        if level is not None:
            window.settings_panel.input_level.setValue(round(level * 100))
            window.pet.set_level(level)
    elif event.state == "voice_heard":
        window.settings_panel.last_heard.setText(event.message)
        window.pet.heard(event.message)
    elif event.state == "voice_live":
        # Kullanıcı konuşurken pete bakar; ayarlar paneli yalnız nihai metni gösterir.
        # Her güncelleme balon süresini yeniler, nihai `voice_heard` üzerine yazar.
        window.pet.live(event.message)
    elif event.state == "voice_live_unavailable":
        kind, _, reason = event.message.partition(":")
        if kind == "runtime":
            # Çalışırken düşen canlı yazı bir yapılandırma sorunu değildir.
            window._append_chat(
                "Artemis",
                t(
                    "Canlı yazı çalışırken hata verdi ve bu oturumda kapatıldı; "
                    "ses canlı yazısız sürüyor."
                ),
            )
        else:
            fix = (
                t('Paketleri pip install -e ".[stream]" ile kurun.')
                if kind == "deps"
                else t("Ayarlar > Ses bölümündeki canlı yazı modelini kontrol et.")
            )
            window._append_chat(
                "Artemis",
                t("Canlı yazı açılamadı: {reason}. {fix} Ses canlı yazısız sürüyor.").format(
                    reason=reason, fix=fix
                ),
            )
    elif event.state == "voice_denoise":
        if event.message.startswith("unavailable:"):
            kind, _, reason = event.message.removeprefix("unavailable:").partition(":")
            fix = (
                t('Paketleri pip install -e ".[voice]" ile kurun.')
                if kind == "deps"
                else t(
                    "Artemis'i yeniden kurun ya da Ayarlar > Ses bölümündeki "
                    "ses temizleme ayarlarını kontrol et."
                )
            )
            window._append_chat(
                "Artemis",
                t("Gürültü engelleme açılamadı: {reason}. {fix} Ses işlenmeden sürüyor.").format(
                    reason=reason, fix=fix
                ),
            )
        elif event.message == "off":
            window._append_chat(
                "Artemis",
                t(
                    "Gürültü engelleme hata verdi ve bu oturumda kapatıldı; "
                    "ses işlenmeden devam ediyor."
                ),
            )
    elif event.state == "voice_echo":
        if event.message == "off":
            window._append_chat(
                "Artemis",
                t(
                    "Yankı engelleme hata verdi ve bu oturumda kapatıldı; "
                    "ses yankı gidermeden sürüyor."
                ),
            )
    elif event.state == "voice_game":
        raw = event.message
        msg: str | None
        if raw.startswith("on:") and len(raw) > 3:
            model = raw[3:]
            msg = t(
                "Oyun modu: tam ekran uygulama algılandı; büyük tanıma modeli bırakıldı, "
                "aktif model: {model}."
            ).format(model=model)
        elif raw.startswith("off:") and len(raw) > 4:
            model = raw[4:]
            msg = t("Oyun modu bitti; geri yüklenen model: {model}.").format(model=model)
        else:
            game_messages = {
                "on": t(
                    "Oyun modu: tam ekran uygulama algılandı; büyük tanıma modeli bırakıldı, "
                    "hafif model dinliyor."
                ),
                "off": t("Oyun modu bitti; büyük tanıma modeli geri yüklendi."),
                "error": t("Oyun modu model geçişi başarısız oldu; mevcut tanıma modeli sürüyor."),
                "unavailable": t(
                    "Oyun modu açık ama hafif model (small) bulunamadı; "
                    "Ayarlar > Ses'ten Small modelini indirin."
                ),
                "cloud_unavailable": t(
                    "Oyun modu bulut hedefi seçili ama bulut tanıma sunucusu veya modeli "
                    "ayarlanmamış; Ayarlar > Ses'ten Karma / Bulut tanımayı yapılandırın."
                ),
            }
            msg = game_messages.get(raw)
        if msg:
            window._append_chat("Artemis", msg)
    elif event.state == "voice_error":
        window._append_chat("Artemis", event.message)
        window.pet.flash_error()
        window._show_voice_state("off")
    elif event.state == "context_reset":
        # Adım blokları eski balonlarla birlikte silinir; kayıt da sıfırlanır.
        # Karşılama geri gelir; işlem kartları da sıfırlanır (denetim günlüğü değişmez).
        clear_chat_view(window)
    elif event.state == "cancelling":
        window._set_status(t("İptal ediliyor…"), "running")
    elif event.state == "stopping":
        window._set_status(t("Temizleniyor…"), "running")
    elif event.state == "stopped":
        window._finish_exit()
    elif event.state == "cancelled":
        window._set_status(t("İptal edildi"), "ready")


def clear_chat_view(window: DesktopWindow) -> None:
    window._turn_steps.clear()
    window._reset_cards()
    window.chat.clear()
    if not window._busy:
        window._set_status(t("Hazır"), "ready")
    window._set_controls()


def notify_job(window: DesktopWindow, fields: dict[str, Any]) -> None:
    """Sekme açık olmasa da görünür bildirim: sohbet satırı + Windows toast.

    Tepsi balonu Windows bildirimleri kapalıyken sonuçsuz kayboluyordu; toast sonucu verir.

    Aynı iş/durum çifti bir kez bildirilir (kilit bırakma ve zaman aşımı yolları da
    `code_agent_done` yazar). Kullanıcının kendi iptali bildirim gerektirmez.
    """
    status = str(fields.get("status", ""))
    key = (str(fields.get("job_id", "")), status, str(fields.get("question_id") or ""))
    if status == "cancelled" or key in window._notified_jobs:
        return
    window._notified_jobs.add(key)
    project = str(fields.get("project") or t("kod ajanı"))
    summary = str(fields.get("summary") or "").strip()
    text = t("Kod işi · {project}: {status}").format(
        project=project, status=status_label(fields)
    ) + (f". {summary}" if summary else "")
    # Makbuz ipucu yalnız sohbet satırında; toast metni değişmez.
    hint = " " + t("Makbuz için: “bu işin makbuzunu yaz”.") if status == "done" else ""
    window._append_chat("Artemis", text + hint)
    if should_suppress_toast(getattr(window, "_work_profile", "work")):
        return
    toast = window._toast
    if toast is None:
        return

    def deliver() -> None:
        # powershell çalıştırır (15 sn'ye dek): arayüz iş parçacığını dondurmamak için ayrı
        # iş parçacığı; sonuç sinyalle ana iş parçacığına döner.
        outcome = toast("Artemis", text[:240])
        if outcome.status != "ok":
            # Toast sürerken pencere yok edilmiş olabilir; başarısızlık notuna gerek kalmaz.
            with contextlib.suppress(RuntimeError):
                window._toast_failed.emit(outcome.message)

    threading.Thread(target=deliver, name="job-toast", daemon=True).start()


def show_toast_failure(window: DesktopWindow, message: str) -> None:
    # Sohbet satırı zaten gösterildi; bildirimin neden görünmediği dürüstçe eklenir.
    window._append_chat(
        "Sistem", t("(Windows bildirimi gösterilmedi: {message})").format(message=message)
    )


def show_approval(window: DesktopWindow, item: Approval) -> None:
    if item.request_id != window._active_request:
        window.bridge.decide(item.request_id, item.turn_id, item.approval_id, False)
        return
    if window._approval_dialog is not None:
        window._approval_dialog.reject()
    window.overlay.hide()
    window._show_panel()
    dialog = ApprovalDialog(item, window._decide_approval, window)
    window._approval_dialog = dialog
    dialog.finished.connect(lambda _result, current=dialog: clear_approval(window, current))
    dialog.open()
    window.pet.set_approval(True)


def decide_approval(
    window: DesktopWindow, item: Approval, approved: bool, remember_site: bool = False
) -> bool:
    return window.bridge.decide(
        item.request_id, item.turn_id, item.approval_id, approved, remember_site
    )


def dismiss_approval_for(window: DesktopWindow, request_id: str) -> None:
    dialog = window._approval_dialog
    if dialog is not None and dialog.item.request_id == request_id:
        dialog.reject()


def clear_approval(window: DesktopWindow, dialog: ApprovalDialog) -> None:
    if window._approval_dialog is dialog:
        window._approval_dialog = None
        window.pet.set_approval(False)


def begin_card(window: DesktopWindow, request_id: str, text: str) -> None:
    window._ops.begin(request_id, text)
    window._render_card(request_id)


def feed_card(window: DesktopWindow, event: Activity) -> None:
    # Etiket tablosu işçi açılınca dolar; her olayda güncel kopya okunur.
    card = window._ops.feed(event.request_id, event.event, event.fields, window.bridge.labels())
    if card is not None:
        window._render_card(card.request_id)


def drop_card(window: DesktopWindow, widget: OperationCard) -> None:
    """Kartı hemen kaldırır. `deleteLater` tek başına yetmez: silme olay döngüsüne kalır ve
    o ana dek widget eski konumunda çizilmeye devam eder (kartın altında hayalet kopya).
    Bu yüzden önce gizlenir ve ebeveynden koparılır."""
    window.activity_cards.removeWidget(widget)
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


def render_card(window: DesktopWindow, request_id: str) -> None:
    """Kartı (yeniden) kurar; yeni kart listenin en üstüne, 20'yi aşan eskiler düşer."""
    card = window._ops.cards.get(request_id)
    if card is None:
        return
    if not window.activity_empty.isHidden():
        window.activity_empty.hide()
        window.activity.show()
    widget = OperationCard(card, themes.THEMES[window._theme])
    old = window._op_widgets.get(request_id)
    index = 0
    if old is not None:
        index = window.activity_cards.indexOf(old)
        drop_card(window, old)
    window._op_widgets[request_id] = widget
    window.activity_cards.insertWidget(index, widget)
    window._sync_activity_panel()
    for stale in [key for key in window._op_widgets if key not in window._ops.cards]:
        drop_card(window, window._op_widgets.pop(stale))


def reset_cards(window: DesktopWindow) -> None:
    """Yeni sohbette işlem kartları temizlenir; denetim günlüğüne dokunulmaz."""
    for widget in window._op_widgets.values():
        drop_card(window, widget)
    window._op_widgets.clear()
    window._ops.clear()
    window.activity_notice.hide()
    window.activity.hide()
    window.activity_empty.show()
    window._sync_activity_panel()


def append_chat(
    window: DesktopWindow, speaker: str, text: str, steps: StepBlock | None = None
) -> None:
    window.chat.append(speaker, text, steps)


def turn_for(window: DesktopWindow, request_id: str) -> _TurnSteps:
    turn = window._turn_steps.get(request_id)
    if turn is None:
        turn = _TurnSteps()
        window._turn_steps[request_id] = turn
        while len(window._turn_steps) > _STEP_TURNS_KEPT:
            del window._turn_steps[next(iter(window._turn_steps))]
    return turn


def collect_step(window: DesktopWindow, event: Activity) -> None:
    if event.request_id is None:
        return
    turn = turn_for(window, event.request_id)
    if len(turn.activities) >= _STEP_EVENTS_PER_TURN:
        return
    turn.activities.append(event)
    if turn.block is not None:
        window._add_step(turn.block, event)


def step_block_for(window: DesktopWindow, request_id: str) -> StepBlock:
    """Yanıt balonunun üstündeki blok; bloğa henüz satır gelmediyse gizli kalır."""
    turn = turn_for(window, request_id)
    if turn.block is None:
        block = StepBlock(themes.THEMES[window._theme])
        for activity in turn.activities:
            window._add_step(block, activity)
        turn.block = block
    return turn.block


def add_step(window: DesktopWindow, block: StepBlock, activity: Activity) -> None:
    # Etiket tablosu işçi açılınca dolar; her satırda güncel kopya okunur.
    line = step_line(activity.event, activity.fields, window.bridge.labels())
    if line is not None:
        block.add_line(activity, line)


def show_routine_notice(
    window: DesktopWindow, name: str, text: str, suggest: str = "", summary_day: str = ""
) -> None:
    """Ana düğme: rutin "Çalıştır", tetikleyici önerisi "Yaz" (ADR 0061), gün özeti "Aç";
    özet için vault açıksa ikinci düğme "Vault'a ekle" (ADR 0066)."""
    window._routine_name = name
    window._routine_suggest = suggest
    window._summary_day = summary_day
    window.routine_run_button.setText(
        t("Aç") if summary_day else t("Yaz") if suggest else t("Çalıştır")
    )
    window.summary_vault_button.setVisible(bool(summary_day) and vault_note(window) is not None)
    window.routine_label.setText(text)
    window.routine_bar.show()


def vault_note(window: DesktopWindow) -> str | None:
    """Vault kapalıysa ya da config okunamazsa None, değilse eklenecek not adı."""
    try:
        settings = load_settings(window._config_path)
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return None
    if settings.memory.vault_path is None:
        return None
    return settings.daily_summary.vault_note


def summary_file(window: DesktopWindow) -> Path | None:
    day = parse_day(window._summary_day)
    if day is None:
        return None
    try:
        return summary_path(load_settings(window._config_path).state_dir, day)
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return None


def append_summary_to_vault(window: DesktopWindow) -> None:
    """Normal, onaylı `vault_append` turu: onay penceresi eklenecek metni gösterir."""
    path, note = summary_file(window), vault_note(window)
    window.routine_bar.hide()
    if path is None or note is None:
        return
    try:
        text = path.read_text(encoding="utf-8")[:VAULT_TEXT_LIMIT].strip()
    except (OSError, UnicodeError):
        window._set_status(t("Özet dosyası okunamadı."), "running")
        return
    if text:
        window._submit_routine_tool("vault_append", AppendArguments(path=note, text=text))


def run_routine_notice(window: DesktopWindow) -> None:
    window.routine_bar.hide()
    if window._summary_day:
        path = summary_file(window)
        if path is not None and path.is_file():
            open_text_file(path, t("Gün özeti"), window)
    elif window._routine_suggest:
        # Gönderilmez: metin yalnız sohbet kutusuna yazılır, kullanıcı okuyup kendisi yollar.
        window.prompt.setPlainText(window._routine_suggest)
        window.prompt.setFocus()
    elif window._routine_name:
        window._submit_routine_tool("routine_run", RoutineNameArguments(name=window._routine_name))
