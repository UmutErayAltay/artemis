"""Ayarlar paneli 2. ve 3. grup sayfaları (dosya boyutu için settings_pages.py'den ayrıldı)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from artemis.channels.desktop.about_section import AboutSection
from artemis.channels.desktop.calendar_mail_section import CalendarMailSection
from artemis.channels.desktop.file_search_section import FileSearchSection
from artemis.channels.desktop.form_kit import (
    _HeightForWidthBox,
    button_row,
    field,
    group,
    meta_label,
)
from artemis.channels.desktop.mcp_section import MCPSection
from artemis.channels.desktop.page_kit import SettingsTabWidget
from artemis.channels.desktop.routines_section import RoutinesSection
from artemis.channels.desktop.security_settings_section import build_privacy_security_group
from artemis.channels.desktop.skills_section import SkillsSection
from artemis.channels.desktop.telegram_section import TelegramSection
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.channels.desktop.update_section import UpdateSection
from artemis.channels.desktop.widgets import with_help
from artemis.config.settings import BUILTIN_AGENT
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

_HINT_MAX_WIDTH = 640


def default_repos_dir() -> str:
    return str(Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / "Projeler")


def _indent(box: QCheckBox, help: str = "") -> QHBoxLayout:  # noqa: A002
    row = QHBoxLayout()
    row.setContentsMargins(24, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(box)
    if help:
        from artemis.channels.desktop.widgets import HelpTip

        row.addWidget(HelpTip(help), 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
    return row


def _check_hint(text: str) -> QLabel:
    label = meta_label(text)
    label.setContentsMargins(24, 0, 0, 0)
    label.setMaximumWidth(_HINT_MAX_WIDTH)
    return label


def _make_subtab(tabs: QTabWidget, title: str) -> QVBoxLayout:
    page = _HeightForWidthBox()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 16, 0, 0)
    layout.setSpacing(16)
    tabs.addTab(page, title)
    return layout


def build_automation_page(panel: SettingsPanel) -> None:
    """5. sayfa: Otomasyon Kuralları (Rutinler | Tetikleyiciler | Gün özeti | Araştırma)."""
    from artemis.channels.desktop.settings_pages import add_page

    config_path = panel.config_path
    demo = panel._demo
    page = add_page(
        panel,
        t("Otomasyon Kuralları"),
        t("Rutinler, tetikleyiciler, gün özeti ve arka plan araştırmaları."),
        t("Otomasyon ve Bilgi"),
    )
    panel.proactive_enabled = QCheckBox(t("Zamanlı işler"))
    page.addWidget(panel.proactive_enabled)
    page.addWidget(
        _check_hint(
            t(
                "Hatırlatmalar, rutin saatleri, tetikleyiciler ve gün özeti "
                "bunun açık olmasını ister."
            )
        )
    )

    tabs = SettingsTabWidget()
    panel.automation_tabs = tabs
    panel._page_tabs["Otomasyon Kuralları"] = tabs
    page.addWidget(tabs)

    # 1) Rutinler
    routines_tab = _make_subtab(tabs, t("Rutinler"))
    panel.routines_enabled = QCheckBox(t("Rutinleri kullan"))
    routines_tab.addWidget(panel.routines_enabled)
    routines_tab.addWidget(
        _check_hint(
            t(
                "Sesle, yazıyla veya Otomasyon sayfasından çalıştırılır. Her adım kendi onay "
                "kurallarıyla yürür; onaylanmayan ya da başarısız ilk adımda durur."
            )
        )
    )
    panel.routines = RoutinesSection(config_path, demo=demo)
    routines_tab.addWidget(panel.routines)
    routines_tab.addStretch(1)

    # 2) Tetikleyiciler
    triggers_tab = _make_subtab(tabs, t("Tetikleyiciler"))
    panel.triggers_enabled = QCheckBox(t("Tetikleyicileri kullan"))
    triggers_tab.addWidget(panel.triggers_enabled)
    triggers_tab.addWidget(
        _check_hint(
            t(
                "Klasör yoklanır; ilk bakışta var olan dosyalar bildirilmez. Dosya yazılması "
                "bitince bir kez haber verilir. Dosya adları Telegram'a gönderilmez. "
                "Zamanlı işler açık olmalı."
            )
        )
    )
    panel.triggers = TriggersSection(config_path, demo=demo)
    triggers_tab.addWidget(panel.triggers)
    triggers_tab.addStretch(1)
    panel.triggers.saved.connect(panel._triggers_saved)
    panel.triggers.proactive_requested.connect(panel._tick_proactive)

    # 3) Gün özeti
    summary_tab = _make_subtab(tabs, t("Gün özeti"))
    panel.daily_summary_enabled = QCheckBox(t("Gün özetini kullan"))
    summary_tab.addLayout(
        with_help(
            panel.daily_summary_enabled,
            t(
                "Belirlenen saatte yalnız sayılardan (araç, dosya, iş, hatırlatma) kısa bir özet "
                "yazar; model çağrılmaz, konuşma okunmaz, Telegram'a gitmez. Vault'a yalnız "
                "bildirimdeki düğmeyle, onayla eklenir."
            ),
        )
    )
    summary_tab.addWidget(
        _check_hint(
            t(
                "Konuşma metni okunmaz; yalnız bugünün araç, dosya, iş ve hatırlatma sayıları "
                "Artemis'in veri klasöründeki günlük klasörüne yazılır. Telegram'a gitmez. Vault'a "
                "ekleme yalnız bildirimdeki düğmeyle ve onayla olur. Zamanlı işler açık olmalı."
            )
        )
    )
    panel.daily_summary_at = QLineEdit()
    panel.daily_summary_at.setMaxLength(5)
    panel.daily_summary_at.setAccessibleName(t("Özet saati"))
    panel.daily_summary_at.setPlaceholderText("22:30")
    field(summary_tab, t("Özet saati (SS:DD)"), panel.daily_summary_at)
    summary_tab.addStretch(1)

    # 4) Araştırma
    research_tab = _make_subtab(tabs, t("Araştırma"))
    panel.research_enabled = QCheckBox(t("Araştırma işlerini kullan"))
    research_tab.addLayout(
        with_help(
            panel.research_enabled,
            t(
                "Konuyu yalnız web aramasıyla araştırıp kaynaklı rapor yazar. Her araştırma "
                "başlamadan onay sorulur (varsayılan en çok 12 arama). İş sohbet geçmişini, "
                "hafızayı ve notlarını görmez."
            ),
        )
    )
    research_tab.addWidget(
        _check_hint(
            t(
                "Her araştırma başlamadan önce onay sorulur; onay en fazla 12 web araması içindir. "
                "İş sohbet geçmişini, hafızayı ve notlarını görmez. Rapor Artemis'in veri "
                "klasöründeki araştırma klasörüne yazılır, kasaya kendiliğinden eklenmez."
            )
        )
    )
    research_tab.addStretch(1)
    page.addStretch(1)


def build_knowledge_page(panel: SettingsPanel) -> None:
    """6. sayfa: Bilgi ve Beceriler (Beceriler | Dosya araması)."""
    from artemis.channels.desktop.settings_pages import add_page

    config_path = panel.config_path
    demo = panel._demo
    page = add_page(
        panel,
        t("Bilgi ve Beceriler"),
        t("Özel çalışma yöntemleri (.md beceriler) ve yerel dosya araması."),
    )
    tabs = SettingsTabWidget()
    panel.knowledge_tabs = tabs
    panel._page_tabs["Bilgi ve Beceriler"] = tabs
    page.addWidget(tabs)

    # 1) Beceriler
    skills_tab = _make_subtab(tabs, t("Beceriler"))
    panel.skills_enabled = QCheckBox(t("Becerileri kullan"))
    skills_tab.addLayout(
        with_help(
            panel.skills_enabled,
            t(
                "Elle yazdığın .md becerileri Artemis'e çalışma yöntemi olarak öğretir. Artemis "
                "kendi başına beceri yazamaz; beceri izin vermez. Beceri metni bulut modele "
                "gidebilir, kişisel veri yazma."
            ),
        )
    )
    skills_tab.addWidget(
        _check_hint(
            t(
                "Dosyalar varsayılan olarak Artemis'in kullanıcı verisi klasöründeki beceriler "
                "klasöründe durur; Klasör seç ile başka bir klasör seçebilirsin. Açılışta ve "
                "Yenile'de okunur. Beceriler Artemis içinde eklenir, düzenlenir ve silinir; "
                "dosyayı başka bir düzenleyicide açmana gerek yok. Artemis kendi başına beceri "
                "yazamaz; beceri izin de vermez, her adım normal onaydan geçer. Beceri metni bulut "
                "modele gidebilir, kişisel veri yazma. Mesajı /beceri-adı ile başlatarak da "
                "çağırabilirsin."
            )
        )
    )
    panel.skills = SkillsSection(config_path, demo=demo)
    skills_tab.addWidget(panel.skills)
    skills_tab.addStretch(1)

    # 2) Dosya araması
    files_tab = _make_subtab(tabs, t("Dosya araması"))
    panel.file_search_enabled = QCheckBox(t("Dosya aramasını kullan"))
    files_tab.addLayout(
        with_help(
            panel.file_search_enabled,
            t(
                "Seçtiğin klasörlerde dosya adı ve içeriğiyle arama yapar. Gizli görünen dosyalar "
                "(.env, anahtar, parola adları) ve bağlantılar asla dizinlenmez. Varsayılan "
                "kapalı; sonuçta yol ve kısa alıntı gelir."
            ),
        )
    )
    files_tab.addWidget(
        _check_hint(
            t(
                "Metin, kod ve docx dosyalarının içeriği; PDF'lerin yalnız adı aranır. Sonuçta "
                "yol ve kısa alıntı gelir, dosyanın tamamı gelmez. Bulut izni verilmediği sürece "
                "dosya içeriği yalnız yerel modele gider; izin Uygulamalar ve Güvenlik bölümünden "
                "açılır."
            )
        )
    )
    panel.file_search = FileSearchSection(config_path, demo=demo)
    panel.file_search_roots = panel.file_search.roots_list
    files_tab.addWidget(panel.file_search)
    files_tab.addStretch(1)
    page.addStretch(1)


def _build_telegram_tab(panel: SettingsPanel, tabs: QTabWidget) -> None:
    telegram_tab = _make_subtab(tabs, "Telegram")
    panel.telegram = TelegramSection(panel.config_path, demo=panel._demo)
    panel.telegram.changed.connect(panel.saved_notes[t("Bağlantılar")].flash)
    panel.telegram.channel_changed.connect(panel.saved_notes[t("Bağlantılar")].flash)
    telegram_tab.addWidget(panel.telegram)
    panel.telegram_voice = QCheckBox(t("Ses mesajlarını kabul et"))
    panel.telegram_photo = QCheckBox(t("Fotoğrafları kabul et"))
    panel.telegram_files = QCheckBox(t("Dosyaları kabul et"))
    telegram_tab.addLayout(
        with_help(
            panel.telegram_voice,
            t(
                "Ses, fotoğraf ve dosya kabulü varsayılan kapalıdır; açınca bu içerik bu "
                "bilgisayarda işlenir (ses yazıya, fotoğraf yerel OCR ile okunur, dosya çalışma "
                "alanına kaydedilir, açılmaz). Ses onay yerine geçmez; onay yalnız düğmeyle "
                "verilir."
            ),
        )
    )
    telegram_tab.addWidget(panel.telegram_photo)
    telegram_tab.addWidget(panel.telegram_files)
    telegram_tab.addWidget(
        _check_hint(
            t(
                "Ses yazıya çevrilir ve komut olarak çalışır "
                "(en çok 1 MB, 60 sn); fotoğraftaki metin yerel OCR ile okunur ve gizli sayılır; "
                "dosyalar çalışma alanındaki Telegram klasörüne kaydedilir, açılmaz. İletilmiş "
                "mesajlar yine çalışmaz."
            )
        )
    )
    telegram_tab.addStretch(1)


def _build_calendar_mail_tab(panel: SettingsPanel, tabs: QTabWidget) -> None:
    cal_tab = _make_subtab(tabs, t("Takvim ve posta"))
    takvim_box, takvim = group(t("Takvim"), divided=False)
    cal_tab.addWidget(takvim_box)
    panel.calendar_enabled = QCheckBox(t("Takvimi kullan"))
    takvim.addWidget(panel.calendar_enabled)
    panel.calendar_cloud = QCheckBox(t("Takvim içeriği bulut modele ve Telegram'a gidebilsin"))
    takvim.addLayout(
        _indent(
            panel.calendar_cloud,
            t(
                "Kapalıyken takvim içeriği yalnız yerel modele gider. Açıkken bulut modele ve "
                "Telegram'a da gidebilir; dışarı çıkan veri artar."
            ),
        )
    )
    panel.calendar_enabled.toggled.connect(panel.calendar_cloud.setEnabled)
    panel.calendar_cloud.setEnabled(panel.calendar_enabled.isChecked())
    posta_box, posta = group(t("Posta"))
    cal_tab.addWidget(posta_box)
    panel.mail_enabled = QCheckBox(t("Postayı kullan"))
    posta.addWidget(panel.mail_enabled)
    panel.mail_cloud = QCheckBox(t("Posta içeriği bulut modele ve Telegram'a gidebilsin"))
    posta.addLayout(
        _indent(
            panel.mail_cloud,
            t(
                "Kapalıyken posta içeriği yalnız yerel modele gider. Açıkken bulut modele ve "
                "Telegram'a da gidebilir; dışarı çıkan veri artar."
            ),
        )
    )
    panel.mail_enabled.toggled.connect(panel.mail_cloud.setEnabled)
    panel.mail_cloud.setEnabled(panel.mail_enabled.isChecked())
    posta.addWidget(
        _check_hint(
            t(
                "Kapalıyken içerik yalnız yerel modele gider. Posta okundu işaretlenmez, hiçbir "
                "ileti silinmez ya da taşınmaz. Eki indirilmez."
            )
        )
    )
    panel.mail_host = QLineEdit()
    panel.mail_host.setAccessibleName(t("IMAP sunucusu"))
    panel.mail_host.setPlaceholderText("imap.gmail.com")
    field(posta, t("IMAP sunucusu"), panel.mail_host)
    panel.mail_port = QLineEdit()
    panel.mail_port.setAccessibleName(t("IMAP portu"))
    field(posta, t("Port"), panel.mail_port)
    panel.mail_username = QLineEdit()
    panel.mail_username.setAccessibleName(t("Posta kullanıcı adı"))
    field(posta, t("Kullanıcı adı"), panel.mail_username)
    panel.mail_folder = QLineEdit()
    panel.mail_folder.setAccessibleName(t("Posta klasörü"))
    field(posta, t("Klasör"), panel.mail_folder)
    panel.calendar_mail = CalendarMailSection(
        panel.config_path, demo=panel._demo, calendar=takvim, mail=posta
    )
    cal_tab.addWidget(panel.calendar_mail)
    cal_tab.addStretch(1)


def _build_github_agents_tab(panel: SettingsPanel, tabs: QTabWidget) -> None:
    github = _make_subtab(tabs, t("GitHub ve kod ajanları"))
    account_box, account = group(t("Hesap"), divided=False)
    github.addWidget(account_box)
    panel.github_owner = QLineEdit()
    panel.github_owner.setAccessibleName(t("GitHub kullanıcı adı"))
    panel.github_owner.setPlaceholderText(t("kullanıcı-adı"))
    panel.repos_dir = QLineEdit()
    panel.repos_dir.setAccessibleName(t("Repo klasörü"))
    panel.repos_dir.setPlaceholderText(default_repos_dir())
    panel.repos_browse = QPushButton(t("Klasör seç"))
    panel.repos_browse.setMinimumWidth(88)
    panel.repos_browse.clicked.connect(panel._browse_repos_dir)
    folder_row = QHBoxLayout()
    folder_row.setSpacing(8)
    folder_row.addWidget(panel.repos_dir, 1)
    folder_row.addWidget(panel.repos_browse)
    field(account, t("Kullanıcı adı"), panel.github_owner)
    field(account, t("Repo klasörü"), folder_row)
    panel.github_auto = QCheckBox(t("Otomatik eşitle"))
    panel.github_auto.setChecked(True)
    account.addWidget(panel.github_auto)
    panel.github_button = QPushButton(t("Bağla ve repoları çek"))
    panel.github_button.clicked.connect(panel._bind_github)
    account.addLayout(button_row(panel.github_button))
    result_box, result = group(t("Eşitleme"))
    github.addWidget(result_box)
    panel.github_status = meta_label(
        t(
            "Kendi repoların (fork ve arşivlenmişler hariç) kod ajanının erişimine girer. "
            "Giriş GitHub CLI (gh) ile yapılır; Artemis anahtarını görmez."
        )
    )
    result.addWidget(panel.github_status)
    panel.github_list = QListWidget()
    panel.github_list.setMaximumHeight(144)
    panel.github_list.setAccessibleName(t("GitHub eşitleme sonuçları"))
    result.addWidget(panel.github_list)
    agents_box, agents = group(
        t("Kod ajanları"),
        t("Arka plan proje işinde çalışacak ajanlar."),
    )
    github.addWidget(agents_box)
    panel.agent_list = QListWidget()
    panel.agent_list.setMaximumHeight(144)
    panel.agent_list.setAccessibleName(t("Kod ajanları"))
    agents.addWidget(panel.agent_list)
    panel.default_agent_combo = QComboBox()
    panel.default_agent_combo.setAccessibleName(t("Varsayılan kod ajanı"))
    field(
        agents,
        t("Varsayılan ajan"),
        panel.default_agent_combo,
        t("Projede ayrıca seçilmediyse bu ajan çalışır."),
    )
    panel.add_custom_agent_button = QPushButton(t("Özel ajan ekle…"))
    panel.add_custom_agent_button.clicked.connect(panel._add_custom_agent)
    agents.addLayout(button_row(panel.add_custom_agent_button))
    panel.agent_hint = meta_label("")
    agents.addWidget(panel.agent_hint)
    panel.code_agents: dict[str, dict[str, Any]] = {}
    panel.default_agent = BUILTIN_AGENT
    panel._builtin_command = "claude"
    panel._refresh_code_agents()
    panel.default_agent_combo.activated.connect(panel._default_agent_picked)
    github.addStretch(1)


def _build_mcp_tab(panel: SettingsPanel, tabs: QTabWidget) -> None:
    mcp_tab = _make_subtab(tabs, t("MCP Sunucuları"))
    panel.mcp = MCPSection(panel.config_path, demo=panel._demo)
    panel.mcp_servers = panel.mcp
    panel.mcp.changed.connect(panel._mark_dirty)
    mcp_tab.addWidget(panel.mcp)
    mcp_tab.addStretch(1)


def build_connections_page(panel: SettingsPanel) -> None:
    """7. sayfa: Bağlantılar (Telegram | Takvim ve posta | GitHub ve kod ajanları | MCP)."""
    from artemis.channels.desktop.settings_pages import add_page

    page = add_page(
        panel,
        t("Bağlantılar"),
        t("Telegram botu, takvim/posta erişimi, GitHub kod ajanları ve MCP sunucuları."),
        t("Dış Bağlantılar ve Sistem"),
    )
    tabs = SettingsTabWidget()
    panel.connections_tabs = tabs
    panel._page_tabs["Bağlantılar"] = tabs
    page.addWidget(tabs)
    _build_telegram_tab(panel, tabs)
    _build_calendar_mail_tab(panel, tabs)
    _build_github_agents_tab(panel, tabs)
    _build_mcp_tab(panel, tabs)
    page.addStretch(1)


def build_apps_page(panel: SettingsPanel) -> None:
    """8. sayfa: Uygulamalar ve Güvenlik."""
    from artemis.channels.desktop.settings_pages import add_page

    apps = add_page(
        panel,
        t("Uygulamalar ve Güvenlik"),
        t("Açılacak uygulamalar, bilgisayar kullanımı, bulut gizliliği ve şifreleme."),
    )
    access_box, access = group(t("Erişim"), divided=False)
    apps.addWidget(access_box)
    panel.all_applications = QCheckBox(t("Bütün kurulu uygulamalara erişim"))
    panel.all_applications.toggled.connect(panel._sync_application_access)
    access.addWidget(panel.all_applications)
    panel.computer_auto = QCheckBox(t("Otomatik bilgisayar kullanımı"))
    panel.computer_auto.setToolTip(
        t(
            "Hassas adımlar ve temiz turda başlangıç sorulmaz; Esc her an durdurur. Terminal, "
            "Artemis ve parola yöneticisi engeli ile dış içerikten sonraki onay yine geçerlidir."
        )
    )
    access.addLayout(
        with_help(
            panel.computer_auto,
            t(
                "Adımları sormadan yapar; Esc her an durdurur. Terminal, Artemis ve parola "
                "yöneticisi engeli ile dış içerikten sonraki onay yine geçerlidir. Üst çubuktaki "
                "düğmeyle aynı ayar."
            ),
        )
    )
    access.addWidget(
        _check_hint(
            t(
                "Adımları sormadan yapar; Esc her an durdurur. Üst çubuktaki otomatik onay "
                "düğmesiyle aynı ayardır."
            )
        )
    )
    panel.computer_observation = QComboBox()
    panel.computer_observation.setAccessibleName(t("Bilgisayar kullanımı gözlemi"))
    for label, key in (
        (t("Ekran öğeleri (UIA) · hızlı, metin modeli de çalışır"), "uia"),
        (t("Karma (UIA, takılınca görüntü) · hızlı ve esnek"), "hybrid"),
        (t("Ekran görüntüsü · görsel model gerekir"), "screen"),
        (t("İkisi birden · en sağlam, en yavaş"), "both"),
    ):
        panel.computer_observation.addItem(label, key)
    field(
        access,
        t("Gözlem"),
        panel.computer_observation,
        help=t(
            "UIA pencerenin düğme ve alanlarını adlarıyla okur; Karma normalde UIA kullanır, "
            "liste boşsa veya takılırsa tek adımlık ekran görüntüsü alır."
        ),
    )
    list_box, listing = group(
        t("İzinli uygulamalar"),
        help=t(
            "Artemis yalnız bu listedeki uygulamaları açabilir. Uygulama ekle bir .exe seçtirir, "
            "Listeden çıkar seçili kaydı siler. Onaysız kapatma iznini aşağıdaki listeden "
            "verebilirsin."
        ),
    )
    apps.addWidget(list_box)
    panel.application_list = QListWidget()
    panel.application_list.setMaximumHeight(144)
    panel.application_list.setAccessibleName(t("İzinli uygulamalar"))
    listing.addWidget(panel.application_list)
    panel.add_application_button = QPushButton(t("Uygulama ekle"))
    panel.add_application_button.setObjectName("primary")
    panel.add_application_button.clicked.connect(panel._add_application)
    panel.remove_application_button = QPushButton(t("Listeden çıkar"))
    panel.remove_application_button.clicked.connect(panel._remove_application)
    listing.addLayout(button_row(panel.remove_application_button, panel.add_application_button))
    panel.application_hint = meta_label(t("Artemis listedeki uygulamaları açabilir."))
    listing.addWidget(panel.application_hint)

    cwc_label = QLabel(t("Onaysız kapatılabilecek uygulamalar"))
    cwc_label.setObjectName("fieldLabel")
    listing.addWidget(cwc_label)
    panel.close_without_confirmation_list = QListWidget()
    panel.close_without_confirmation_list.setMaximumHeight(120)
    panel.close_without_confirmation_list.setAccessibleName(t("Onaysız kapatılacak uygulamalar"))
    listing.addWidget(panel.close_without_confirmation_list)
    listing.addWidget(
        _check_hint(
            t(
                "İşaretli uygulamalar dış içerik okunmamış temiz turda yeniden onay sormadan "
                "kapatılabilir."
            )
        )
    )

    build_privacy_security_group(panel, apps)
    apps.addStretch(1)


def build_about_page(panel: SettingsPanel) -> None:
    """9. sayfa: Hakkında ve Güncelleme."""
    from artemis.channels.desktop.settings_pages import add_page

    about = add_page(
        panel,
        t("Hakkında ve Güncelleme"),
        t("Sürüm, lisans, bağlantılar ve güncelleme."),
    )
    panel.about_section = AboutSection(panel.config_path, parent=panel)
    about.addWidget(panel.about_section)
    box, body = group(t("Güncelleme"))
    panel.update_section = UpdateSection(panel)
    body.addWidget(panel.update_section)
    about.addWidget(box)
    about.addStretch(1)
