"""Araçların Türkçe görünen adları, kısa özetleri ve argüman etiketleri (rutin formu için).

Araç `description`'ı modele yazılmış teknik/İngilizce metindir; kullanıcıya gösterilmez. Yeni araç
eklenince `tests/test_tool_titles.py` bu tabloları doldurmayı zorlar. Bilinmeyen araç/argüman
(ör. MCP aracı) kimliğine düşer.
"""

# ruff: noqa: E501
from __future__ import annotations

from artemis.i18n import t

# (başlık, kısa özet)
_TOOLS: dict[str, tuple[str, str]] = {
    "app_alias_forget": ("Uygulama takma adını unut", "Söylediğin bir uygulama adı eşlemesini siler."),
    "app_alias_remember": ("Uygulama takma adı kaydet", "Söylediğin adı bir uygulamaya eşler."),
    "brightness_set": ("Parlaklığı ayarla", "Ekran parlaklığını verilen düzeye getirir."),
    "calendar_events": ("Takvim olayları", "Yaklaşan takvim olaylarını listeler."),
    "close_application": ("Uygulamayı kapat", "Çalışan bir uygulamayı kapatır."),
    "code_agent_answer": ("Kod ajanına cevap ver", "Çalışan kod işinin sorusunu yanıtlar."),
    "code_agent_cancel": ("Kod işini iptal et", "Çalışan bir kod işini durdurur."),
    "code_agent_clear_finished": ("Biten kod işlerini temizle", "Biten kod işi kayıtlarını kaldırır."),
    "code_agent_discard": ("Kod taslağını sil", "Hazırlanmış bir iş taslağını atar."),
    "code_agent_prepare": ("Kod işi taslağı hazırla", "Bir proje için iş taslağı hazırlar; başlatmaz."),
    "code_agent_project_context": ("Proje bağlamını oku", "Projenin kısa bağlam bilgisini getirir."),
    "code_agent_release": ("Kod işini serbest bırak", "Biten kod işinin çalışma alanını bırakır."),
    "code_agent_start": ("Kod işini başlat", "Onaylı taslağı arka planda çalıştırır."),
    "code_agent_status": ("Kod işi durumu", "Kod işlerinin durumunu gösterir."),
    "computer_use": ("Bilgisayarı kullan", "Bir hedef için ekranda fare ve klavyeyle çalışır."),
    "conversation_history": ("Sohbet geçmişi yönetimi", "Geçmişi sorgular ya da saklama süresini değiştirir."),
    "conversation_search": ("Sohbet geçmişinde ara", "Eski konuşmalarda metin arar."),
    "create_directory": ("Klasör oluştur", "Çalışma alanında yeni klasör açar."),
    "create_file": ("Dosya oluştur", "Çalışma alanında yeni dosya yazar."),
    "current_time": ("Saati söyle", "Şu anki tarih ve saati verir."),
    "daily_summary_read": ("Gün özetini oku", "Seçilen günün özetini getirir."),
    "delete_file": ("Dosyayı sil", "Çalışma alanındaki bir dosyayı siler."),
    "file_search": ("Dosyalarda ara", "İndekslenmiş yerel dosyalarda arar."),
    "list_files": ("Dosyaları listele", "Bir klasördeki dosyaları gösterir."),
    "list_undo_records": ("Geri alma kayıtları", "Silme günlüğündeki kayıtları listeler."),
    "mail_unread": ("Okunmamış postalar", "Okunmamış e-postaların başlıklarını listeler."),
    "memory_change_undo": ("Hafıza değişikliğini geri al", "Bir hafıza yazımını geri alır."),
    "memory_changes": ("Hafıza değişiklikleri", "Son hafıza yazımlarını listeler."),
    "memory_forget": ("Notu unut", "Kayıtlı bir hafıza notunu siler."),
    "memory_job_receipt": ("İş makbuzu yaz", "Biten bir iş için hafızaya makbuz ekler."),
    "memory_list": ("Hafıza notlarını listele", "Kayıtlı notları gösterir."),
    "memory_open_work": ("Açık işleri oku", "Companion dosyalarındaki açık işleri getirir."),
    "memory_remember": ("Hatırla", "Bir bilgiyi hafıza notu olarak kaydeder."),
    "memory_rule_add": ("Kural ekle", "Hafızaya kalıcı bir kural ekler."),
    "memory_rule_remove": ("Kural sil", "Hafızadan bir kuralı kaldırır."),
    "memory_session_update": ("Oturum notunu güncelle", "Son oturum ve işler dosyasını günceller."),
    "move_file": ("Dosyayı taşı", "Çalışma alanında bir dosyayı başka yere taşır."),
    "notify_desktop": ("Masaüstü bildirimi", "Ekranda bildirim gösterir."),
    "notify_telegram": ("Telegram'a bildir", "Sana Telegram'dan mesaj gönderir."),
    "open_application": ("Uygulama aç", "Bir uygulamayı (ve isteğe bağlı dosyayı) açar."),
    "open_url": ("Adres aç", "Bir web adresini tarayıcıda açar."),
    "power": ("Güç işlemi", "Kilitle, uyut, kapat ya da yeniden başlat."),
    "read_clipboard": ("Panoyu oku", "Panodaki metni okur."),
    "read_file": ("Dosyayı oku", "Çalışma alanındaki bir dosyayı okur."),
    "reminder_cancel": ("Hatırlatmayı iptal et", "Bekleyen bir hatırlatmayı iptal eder."),
    "reminder_create": ("Hatırlatma kur", "Belirli zamanda hatırlatma kurar."),
    "reminder_list": ("Hatırlatmaları listele", "Hatırlatmaları durumuna göre listeler."),
    "reminder_retry": ("Hatırlatmayı yeniden dene", "Başarısız hatırlatmayı yeniden kurar."),
    "rename_file": ("Dosyayı yeniden adlandır", "Bir dosyanın adını değiştirir."),
    "research_cancel": ("Araştırmayı iptal et", "Çalışan araştırma işini durdurur."),
    "research_result": ("Araştırma sonucu", "Biten araştırmanın raporunu getirir."),
    "research_status": ("Araştırma durumu", "Araştırma işinin durumunu gösterir."),
    "research_start": ("Araştırma başlat", "Konu hakkında arka planda web araştırması yapar."),
    "routine_delete": ("Rutini sil", "Kayıtlı bir rutini siler."),
    "routine_list": ("Rutinleri listele", "Kayıtlı rutinleri gösterir."),
    "routine_run": ("Rutini çalıştır", "Kayıtlı bir rutini adımlarıyla çalıştırır."),
    "routine_save": ("Rutini kaydet", "Rutin oluşturur ya da günceller."),
    "screen_read": ("Ekranı oku", "Ekrandaki metni yerel OCR ile okur."),
    "skill_read": ("Beceriyi oku", "Bir becerinin talimatını yükler."),
    "system_info": ("Sistem bilgisi", "Bilgisayarın temel durumunu verir."),
    "vault_append": ("Vault'a ekle", "Vault'taki bir nota metin ekler."),
    "vault_read_note": ("Vault notunu oku", "Vault'tan bir not okur."),
    "vault_search": ("Vault'ta ara", "Vault notlarında arar."),
    "voice_continuous": ("Sürekli dinleme", "Sürekli ses dinlemeyi açar ya da kapatır."),
    "volume_set": ("Sesi ayarla", "Ses düzeyini ayarlar ya da sessize alır."),
    "web_search": ("Web'de ara", "İnternette arama yapar."),
    "write_clipboard": ("Panoya yaz", "Verilen metni panoya kopyalar."),
    "window_control": ("Pencereyi yönet", "Bir pencereyi büyütür, küçültür, kapatır vb."),
    "window_list": ("Pencereleri listele", "Açık pencereleri gösterir."),
}

# Argüman adı -> Türkçe etiket (tüm araçlarda aynı anlam).
_ARGS: dict[str, str] = {
    "acceptance": "Kabul ölçütü",
    "action": "İşlem",
    "agent": "Ajan",
    "answer": "Cevap",
    "application": "Uygulama",
    "at": "Zaman",
    "authority_boundary": "Yetki sınırı",
    "body": "Metin",
    "change_id": "Değişiklik kimliği",
    "content": "İçerik",
    "day": "Gün",
    "days": "Gün sayısı",
    "destination": "Hedef yol",
    "enabled": "Açık",
    "file_path": "Dosya yolu",
    "goal": "Hedef",
    "in_minutes": "Kaç dakika sonra",
    "isler": "İşler",
    "job_id": "İş kimliği",
    "level": "Düzey",
    "limit": "En çok sonuç",
    "max_searches": "En çok arama",
    "mode": "Kip",
    "muted": "Sessiz",
    "name": "Ad",
    "new_name": "Yeni ad",
    "note_id": "Not kimliği",
    "path": "Yol",
    "project": "Proje",
    "query": "Arama metni",
    "question_id": "Soru kimliği",
    "reminder_id": "Hatırlatma kimliği",
    "retention_days": "Saklama (gün)",
    "scope": "Kapsam",
    "screen": "Ekran",
    "son_oturum": "Son oturum",
    "source": "Kaynak yol",
    "spec": "Taslak",
    "spec_id": "Taslak kimliği",
    "spoken": "Söylenen ad",
    "start_day": "Başlangıç günü",
    "state": "Durum",
    "steps": "Adımlar",
    "task": "Görev",
    "text": "Metin",
    "title": "Başlık",
    "topic": "Konu",
    "url": "Adres",
    "window_id": "Pencere kimliği",
    "workdir": "Çalışma klasörü",
}

# Testin ve çağıranın tek kaynağı: kayıtlı her araç burada olmalı.
TOOL_TITLES: dict[str, str] = {name: title for name, (title, _s) in _TOOLS.items()}


def tool_title(name: str) -> str:
    return t(TOOL_TITLES[name]) if name in TOOL_TITLES else name


def tool_summary(name: str) -> str | None:
    return t(_TOOLS[name][1]) if name in _TOOLS else None


def arg_label(arg: str) -> str:
    return t(_ARGS[arg]) if arg in _ARGS else arg


def tool_display(name: str) -> str:
    """Birleşik görünüm: "Web'de ara (web_search)"; bilinmeyen araç yalnız kimlik."""
    return f"{t(TOOL_TITLES[name])} ({name})" if name in TOOL_TITLES else name
