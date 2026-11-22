# ADR 0083: Bağlamsal çalışma profilleri (`work`, `game`, `private`, `meeting`)

- Tarih: 2026-11-22
- Durum: Kabul edildi

**Bağlam:** Kullanıcı Artemis'i gün içinde farklı ortamlarda kullanıyor: normal çalışma,
tam ekran oyun, gizli/hassas çalışma (buluta veri çıkışı kapalı) ve toplantı (bildirim ve
konuşma sesi istenmiyor). Bu durumlar için kullanıcı her seferinde Ayarlar'dan tek tek
`allow_cloud_*`, `stt_mode`, `proactive.telegram`, `game_mode` veya ses çıkışını değiştirip
yeniden kurulum beklemek zorunda kalmamalıdır.

**Karar:**

1. `Settings.work_profile` alanı (`"work" | "game" | "private" | "meeting"`, varsayılan
   `"work"`) eklendi ve politika tek doğruluk kaynağı olarak `artemis/config/work_profile.py`
   içinde toplandı (`effective_settings_for_profile`, `should_suppress_toast`,
   `should_mute_tts`, `is_local_only_profile`, `is_game_profile`).
2. Masaüstü üst çubuğuna (`artemis/channels/desktop/profile_bar.py`) tek tıkla açılan
   4 öğeli hızlı seçici (`Çalışma`, `Oyun`, `Gizli`, `Toplantı`) eklendi. Seçim
   `save_work_profile` ile atomik olarak config'e yazılır ve `DesktopBridge.set_work_profile`
   üzerinden tam çalışma zamanı yeniden kurulumu gerektirmeden canlı uygulanır. Dar
   pencerelerde (`fit_topbar`) durum hapının kırpılmaması için seçici `optional_items`
   sırasında yer alır.
3. Alt sistemlerde canlı davranış:
   - **`work` (Çalışma):** Yapılandırılmış normal izin ve ses ayarları geçerlidir.
   - **`game` (Oyun):** `Deliverer` ve `window_events.notify_job` masaüstü toast
     bildirimlerini bastırır (`targets["toast"] = "suppressed"`); ses hattı kurulu hafif
     model varsa `GameModeRecognizer` üzerinden hafif STT modeline geçer.
   - **`private` (Gizli):** `ProviderRouter` (canlı `profile_getter` ile `complete`, `_call`,
     `hard_model` ve `fallback_model` dahil) bulut modellerine giden istekleri engeller;
     yerel `ollama` sohbet modeli varsa otomatik ona yönlendirir, yoksa `"cloud_blocked_by_profile"`
     denetimiyle `ProviderError` yükseltir. `AgentLoop` ve `SelectionActions` tüm bulut
     bayraklarını kapalı (`private_seen=True`, yerel seçim) işler. `VoiceController` ve
     `build_dictation_recognizer` bulut STT, doğrudan ses (`direct_audio_input`) ve uzak
     Edge/Cloud TTS aktarımını kapatıp yerel ses bileşenlerine düşer. `Deliverer` proaktif
     Telegram gönderimini durdurur (`targets["telegram"] = "withheld"`).
   - **`meeting` (Toplantı):** `Deliverer` ve `notify_job` toast bildirimlerini bastırır;
     `VoiceController.set_muted(True)` etkin TTS oynatımını anında keser ve sonraki sesli
     yanıtların hoparlöre yazılmasını engeller (yanıt metin olarak sohbete yazılmaya devam eder).

**Sonuç:** Güvenlik kapısı (`SafetyGate`), onay mekanizması ve kalıcı disk yapılandırması
bozulmadan, tek tıkla bağlama uygun gizlilik, ses ve bildirim davranışı canlı uygulanır.
