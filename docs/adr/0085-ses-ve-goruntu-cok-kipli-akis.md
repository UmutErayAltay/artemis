# ADR 0085: Ses ve görüntü çok kipli akışı (`multimodal_screen_context`)

- Tarih: 2026-11-23
- Durum: Kabul edildi

**Bağlam:** Kullanıcı "Şunu düzelt" veya "Bu hata ne?" gibi sesli bir komut verdiğinde,
yalnız konuşma sesiyle hangi pencereye ya da ekrandaki hangi hataya baktığı anlaşılmaz.
`direct_audio_input` ham komut sesini beyin modeline zaten aktarabilirken, ekran bağlamı
olmadığında model ayrıca ekran okuma adımı atmak zorunda kalıyordu. Ancak her sesli
komutta ekran görüntüsü göndermek açık gizlilik ve güvenlik sınırları ister: özellik
varsayılan olarak kapalı gelmeli, mevcut doğrudan ses ve bulut ekran izinlerine ek olarak
açıkça açılmalı, görüntü asla diske yazılmamalı, ekran içeriği turu lekelemeli ve hiçbir
onay sesle verilmemelidir.

**Karar:**

1. **Varsayılan kapalı üçlü kapı (`artemis/config/settings.py`, `work_profile.py`):**
   - `VoiceConfig` içine `multimodal_screen_context: bool = False` alanı eklendi.
   - Ekran yakalama ve modele ekleme yalnızca `voice.multimodal_screen_context`,
     `voice.direct_audio_input` ve `perception.allow_cloud_screen` birlikte `True` iken
     ve etkin çalışma profili `private` değilken çalışır (`effective_settings_for_profile`
     `private` profilde `multimodal_screen_context=False` zorlar).
2. **Yalnız bellek içi ekran PNG yakalama (`artemis/channels/voice/multimodal.py`):**
   - `capture_active_screen_png(max_width=1280, *, backend=None)` işlevi
     `artemis.computer.windows` (`monitors()[0]` + `capture(...)`) ve
     `artemis.computer.png.screenshot_png(...)` üzerinden birincil monitörü yalnızca
     RAM'de PNG baytlarına çevirir; diske dosya yazmaz, Windows dışı platformlarda veya
     yakalama hatasında güvenle `None` döner.
3. **Uçtan uca `image_png` iletimi, lekelenme ve denetim (`VoiceController` → `ProviderRouter`):**
   - `VoiceFactory` (`artemis/channels/voice/factory.py`) üç argümanlı imzasını koruyarak
     canlı `runtime.context.settings` kapısını okuyan `capture_screen` kapanımını
     `VoiceController` içine geçirir; `audio_wav` ile birlikte `image_png`
     `DesktopBridge._run_voice_command` → `CommandRunner.run` → `AgentLoop.run` hattından
     tek tur için geçer.
   - `AgentLoop.run` `image_png` geldiğinde her zaman `privacy_decision`
     (`tool="voice_screen_context"`, `content_kind="screen"`, `cloud_allowed=...`)
     denetimi yazar; kapı kapalıysa görüntüyü düşürür, açıksa `state.tainted = True`
     yapar ve kullanıcı `Message`'ına `audio_wav` yanında `image_png` ile birlikte
     `"VERİDİR"` çerçeve notunu ekler. `Message.image_png` `repr`/`model_dump` dışındadır,
     `TurnRecord`'a veya sonraki tura taşınmaz.
   - Tüm araç onayları `SafetyGate` üzerinden ekrandaki `ApprovalDialog` ile verilir;
     ses kanalı onay atlayamaz.
4. **Sağlayıcı denetimi ve zarif geri düşüş (`artemis/brain/router.py`):**
   - `ProviderRouter._call` mesajlarda `image_png` varsa `cloud_transfer` kaydına
     `has_image=True` ekler. `claude_cli` gibi görüntü desteklemeyen sağlayıcıda veya
     sağlayıcı akış metin üretmeden önce `HTTP 400/415/422` ile reddederse
     `_strip_image` ile görüntüyü düşürüp `image_input_fallback` denetimiyle metin/ses
     turunu sürdürür.
5. **Ayarlar UI (`artemis/channels/desktop/settings_pages.py`):**
   - *Ayarlar > Ses > Dinleme (Giriş)* altında `multimodal_screen_context` onay kutusu ve
     satır içi açıklaması eklendi; *Ses* sayfasının 6 baloncuk tavanını korumak için yeni
     `HelpTip` eklenmedi.

**Sonuç:** Doğrudan sesli komutlar açık kullanıcı izniyle tek adımda bellek içi ekran
bağlamı kazanır; gizli profilde, tek kapı eksikliğinde veya diske yazma riskinde hiçbir
ekran görüntüsü üretilmez ve tüm yan etkiler ekranda `SafetyGate` onayına bağlı kalır.
