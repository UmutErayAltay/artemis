"""Ayarlar > Bağlantılar > MCP Sunucuları: grafiksel MCP yönetim merkezi (ADR 0028).

Sunucular bu panelde eklenir, düzenlenir, silinir ve arka planda `StdioMCPClient.list_tools`
ile test edilir. Liste boşken hiçbir alt süreç başlatılmaz; `readOnlyHint` riski düşürmez,
riski yalnız kullanıcının `read_only_tools` ve `confirm_tools` listeleri belirler.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import button_row, group, meta_label
from artemis.channels.desktop.mcp_dialog import MCPServerDialog
from artemis.channels.desktop.settings_io import dump_mcp_server
from artemis.channels.desktop.settings_lists import ListWithEmpty
from artemis.channels.desktop.thread_shutdown import shutdown_thread
from artemis.config.settings import MCPServerConfig, load_settings
from artemis.i18n import t
from artemis.mcp.bridge import default_client, resolve_risk
from artemis.mcp.client import MCPError, MCPToolSpec
from artemis.tools.base import Risk

_SHUTDOWN_WAIT_MS = 2000

ProbeFunc = Callable[[MCPServerConfig, threading.Event], list[MCPToolSpec]]

_ERROR_MESSAGES = {
    "timeout": "Bağlantı zaman aşımına uğradı.",
    "cancelled": "Bağlantı testi iptal edildi.",
    "version": "MCP protokol sürümü desteklenmiyor.",
    "process": "MCP sunucusu başlatılamadı veya beklenmeden kapandı.",
    "protocol": "MCP sunucusundan geçersiz protokol yanıtı alındı.",
    "too_large": "MCP sunucu yanıtı izin verilen boyutu aştı.",
    "remote": "MCP sunucusu hata döndürdü.",
}


def _default_probe(server: MCPServerConfig, cancel: threading.Event) -> list[MCPToolSpec]:
    return default_client(server).list_tools(cancel=cancel)


def _safe_error_message(error: MCPError) -> str:
    return t(_ERROR_MESSAGES.get(error.error_type, "MCP sunucusuyla bağlantı kurulamadı."))


def _risk_label(risk: Risk) -> str:
    if risk == Risk.READ_ONLY:
        return t("salt okunur")
    if risk == Risk.CONFIRM_REQUIRED:
        return t("onaylı")
    return t("yan etkili")


class MCPHealthTestThread(QThread):
    """Arayüzü dondurmadan arka planda `list_tools(cancel=...)` bağlantı testi çalıştırır."""

    done = Signal(str, list)
    failed = Signal(str, str)

    def __init__(
        self,
        server: MCPServerConfig,
        parent: QWidget | None = None,
        *,
        probe: ProbeFunc | None = None,
    ) -> None:
        super().__init__(parent)
        self.server = server
        self._probe = probe or _default_probe
        self._cancel = threading.Event()

    def requestInterruption(self) -> None:
        self._cancel.set()
        super().requestInterruption()

    def run(self) -> None:
        try:
            tools = self._probe(self.server, self._cancel)
        except MCPError as error:
            self.failed.emit(self.server.name, _safe_error_message(error))
            return
        except (OSError, ValueError):
            self.failed.emit(
                self.server.name, t("MCP sunucusu çalıştırılamadı veya ayar geçersiz.")
            )
            return
        if self.isInterruptionRequested() or self._cancel.is_set():
            self.failed.emit(self.server.name, t("Bağlantı testi iptal edildi."))
            return
        self.done.emit(self.server.name, tools)


class MCPSection(QWidget):
    """Tanımlı stdio MCP sunucuları listesi, ekle/düzenle/sil ve sağlık testi (ADR 0028)."""

    changed = Signal()

    def __init__(
        self,
        config_path: Path,
        *,
        demo: bool = False,
        probe: ProbeFunc | None = None,
    ) -> None:
        super().__init__()
        self._config_path = config_path
        self._demo = demo
        self._probe = probe or _default_probe
        self._servers: list[MCPServerConfig] = []
        self._test_thread: MCPHealthTestThread | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        box, listing = group(
            t("Tanımlı MCP sunucuları"),
            t("Yerel stdio MCP sunucuları yalnız çağrı anında başlar; liste boşsa süreç açılmaz."),
            divided=False,
            help=t(
                "Sunucu üçüncü taraf koddur: ağ ve dosya işlerini kendi yapar. "
                "Sunucunun readOnly beyanı riski düşürmez; onay atlamak için aracı "
                "salt okunur listesine kendin eklersin."
            ),
        )
        layout.addWidget(box)

        self.server_list = QListWidget()
        self.server_list.setAccessibleName(t("MCP sunucuları"))
        self.server_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.server_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.server_list.setWordWrap(True)
        self.server_list.currentRowChanged.connect(self._sync_buttons)
        self.server_list.itemDoubleClicked.connect(self._edit_double_clicked)

        self.server_area = ListWithEmpty(
            self.server_list,
            t("Tanımlı MCP sunucusu yok"),
            t("Liste boşken hiçbir MCP sunucu süreci başlatılmaz."),
            t("Sunucu ekle"),
            height=200,
        )
        empty_add = self.server_area.empty.action_button
        if empty_add is not None:
            empty_add.setObjectName("primary")
            empty_add.setEnabled(not demo)
            empty_add.clicked.connect(self._add)
        listing.addWidget(self.server_area)

        self.test_button = QPushButton(t("Bağlantıyı test et"))
        self.edit_button = QPushButton(t("Düzenle"))
        self.delete_button = QPushButton(t("Sil"))
        self.add_button = QPushButton(t("Sunucu ekle"))
        self.add_button.setObjectName("primary")
        self.add_button.setVisible(False)
        listing.addLayout(
            button_row(self.test_button, self.edit_button, self.delete_button, self.add_button)
        )

        self.test_status = meta_label("")
        self.notice = meta_label("")
        listing.addWidget(self.test_status)
        listing.addWidget(self.notice)

        self.test_button.clicked.connect(self._test_selected)
        self.edit_button.clicked.connect(self._edit_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.add_button.clicked.connect(self._add)

        self.refresh()

    def servers(self) -> tuple[MCPServerConfig, ...]:
        return tuple(self._servers)

    def values(self) -> list[dict[str, Any]]:
        """Kaydedilmek üzere tüm sunucuları YAML uyumlu sözlüklere çevirir."""
        return [dump_mcp_server(server) for server in self._servers]

    def refresh(self) -> None:
        self.notice.setText("")
        self.test_status.setText("")
        try:
            loaded = load_settings(self._config_path)
            self._servers = list(loaded.mcp.servers)
        except (OSError, ValueError):
            self._servers = []
            self.notice.setText(t("Ayarlar okunamadı."))
        self._render_rows()

    def _render_rows(self, select_name: str | None = None) -> None:
        self.server_list.clear()
        for server in self._servers:
            badge = (
                t("Dosya (yalnız yerel)")
                if server.content == "file"
                else t("Metin (genel)")
            )
            cmd = (
                f"{server.command} ({len(server.args)} arg)"
                if server.args
                else server.command
            )
            overrides = t("Salt okunur: {ro} · Onaylı: {confirm}").format(
                ro=len(server.read_only_tools),
                confirm=len(server.confirm_tools),
            )
            item = QListWidgetItem(f"{server.name} · [{badge}] · {cmd} · {overrides}")
            item.setData(Qt.ItemDataRole.UserRole, server.name)
            self.server_list.addItem(item)

        if self._servers:
            self.server_area.show_list()
            target_row = 0
            if select_name is not None:
                for row, server in enumerate(self._servers):
                    if server.name == select_name:
                        target_row = row
                        break
            self.server_list.setCurrentRow(target_row)
        else:
            self.server_area.show_empty(
                t("Tanımlı MCP sunucusu yok"),
                t("Liste boşken hiçbir MCP sunucu süreci başlatılmaz."),
                action=True,
            )
        self._sync_buttons()

    def _selected_server(self) -> MCPServerConfig | None:
        item = self.server_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.ItemDataRole.UserRole)
        return next((server for server in self._servers if server.name == name), None)

    def _sync_buttons(self, *_args: object) -> None:
        has_items = bool(self._servers)
        has_selection = self._selected_server() is not None
        testing = self._test_thread is not None
        editable = not self._demo

        self.add_button.setVisible(has_items)
        self.add_button.setEnabled(editable)
        empty_add = self.server_area.empty.action_button
        if empty_add is not None:
            empty_add.setEnabled(editable)
        self.edit_button.setEnabled(editable and has_selection)
        self.delete_button.setEnabled(editable and has_selection)
        self.test_button.setEnabled(editable and has_selection and not testing)

    def _add(self) -> None:
        self._open_dialog(None)

    def _edit_double_clicked(self, _item: QListWidgetItem) -> None:
        self._edit_selected()

    def _edit_selected(self) -> None:
        server = self._selected_server()
        if server is not None:
            self._open_dialog(server)

    def _open_dialog(self, existing: MCPServerConfig | None) -> None:
        if self._demo:
            return
        taken = [
            server.name
            for server in self._servers
            if existing is None or server.name != existing.name
        ]
        dialog = MCPServerDialog(existing, taken, self)
        if dialog.exec() != MCPServerDialog.DialogCode.Accepted or dialog.result_server is None:
            return
        updated = dialog.result_server
        if existing is None:
            self._servers.append(updated)
        else:
            self._servers = [
                updated if server is existing else server for server in self._servers
            ]
        self._render_rows(select_name=updated.name)
        self.test_status.setText("")
        self.notice.setText(t("Değişiklik Kaydet ile kalıcı olur."))
        self.changed.emit()

    def confirm_box(self, text: str) -> tuple[QMessageBox, QPushButton]:
        """Silme onayı: varsayılan "Vazgeç" (Enter yanlışlıkla sunucu silmesin)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("MCP sunucusu"))
        box.setText(text)
        yes = box.addButton(t("Sil"), QMessageBox.ButtonRole.DestructiveRole)
        no = box.addButton(t("Vazgeç"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        return box, yes

    def _confirm(self, text: str) -> bool:
        box, yes = self.confirm_box(text)
        box.exec()
        return box.clickedButton() is yes

    def _delete_selected(self) -> None:
        if self._demo:
            return
        server = self._selected_server()
        if server is None:
            return
        if not self._confirm(t("'{name}' MCP sunucusu silinsin mi?").format(name=server.name)):
            return
        self._servers = [item for item in self._servers if item.name != server.name]
        self._render_rows()
        self.test_status.setText("")
        self.notice.setText(t("Değişiklik Kaydet ile kalıcı olur."))
        self.changed.emit()

    def _test_selected(self) -> None:
        if self._demo:
            return
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        server = self._selected_server()
        if server is None:
            return
        thread = MCPHealthTestThread(server, self, probe=self._probe)
        thread.done.connect(self._health_done)
        thread.failed.connect(self._health_failed)
        thread.finished.connect(self._health_finished)
        self._test_thread = thread
        self.test_status.setText(t("'{name}' test ediliyor…").format(name=server.name))
        thread.start()
        self._sync_buttons()

    def _health_done(self, name: str, tools: list[MCPToolSpec]) -> None:
        server = next((item for item in self._servers if item.name == name), None)
        if not tools:
            self.test_status.setText(
                t("'{name}' bağlandı ancak geçerli araç bildirmedi.").format(name=name)
            )
            return
        if server is None:
            tool_names = ", ".join(spec.name for spec in tools)
            self.test_status.setText(
                t("'{name}' bağlandı ({count} araç): {tools}").format(
                    name=name, count=len(tools), tools=tool_names
                )
            )
            return
        described = ", ".join(
            f"{spec.name} ({_risk_label(resolve_risk(server, spec))})" for spec in tools
        )
        has_readonly_hint = any(
            spec.annotations.get("readOnlyHint") is True and spec.name not in server.read_only_tools
            for spec in tools
        )
        if has_readonly_hint:
            self.test_status.setText(
                t(
                    "'{name}' bağlandı ({count} araç): {tools}. "
                    "Not: sunucunun readOnlyHint beyanı riski düşürmez; salt okunur araçları "
                    "kendin eklersin."
                ).format(name=name, count=len(tools), tools=described)
            )
        else:
            self.test_status.setText(
                t("'{name}' bağlandı ({count} araç): {tools}").format(
                    name=name, count=len(tools), tools=described
                )
            )

    def _health_failed(self, name: str, message: str) -> None:
        self.test_status.setText(
            t("'{name}' testi başarısız: {message}").format(name=name, message=message)
        )

    def _health_finished(self) -> None:
        self._test_thread = None
        self._sync_buttons()

    def finish(self) -> None:
        if shutdown_thread(
            self._test_thread,
            ("done", self._health_done),
            ("failed", self._health_failed),
            ("finished", self._health_finished),
            wait_ms=_SHUTDOWN_WAIT_MS,
        ) or (self._test_thread is not None and not self._test_thread.isRunning()):
            self._test_thread = None
        self._sync_buttons()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish()
        super().closeEvent(event)
