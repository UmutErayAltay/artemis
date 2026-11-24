# ADR 0086: Olay tetikleyicileri ve koşullu rutin öneri zincirleri

- Tarih: 2026-11-23
- Durum: Kabul edildi

**Bağlam:** ADR 0061 yalnızca klasöre yeni dosya gelmesini yoklayan (`folder`) tetikleyicileri
tanımlamıştı. Kullanıcılar bir uygulama açıldığında (`app_opened`), bir takvim toplantısına
belirli dakika kala (`calendar_before`), bilgisayar uykudan uyandığında (`system_resume`) ya da
bir rutin/tetikleyici tamamlandıktan sonra belirli bağlamsal koşullara göre bir sonraki rutini
öneren zincirlerden yararlanmak ister. Ancak bu otomasyonlar proaktif olduğu için kesin güvenlik
sınırları gerektirir: model hiçbir araçla tetikleyici ya da zincir ekleyememeli, bildirim
metinleri LLM çağrısı olmadan sabit şablonlardan üretilmeli, en çok tek bir eylem düğmesi
taşımalı, tıklama olmadan hiçbir işlem yürütmemeli ve kişisel etkinlik/uygulama/takvim verisi
asla Telegram'a sızmamalıdır.

**Karar:**

1. **Yalnız yapılandırma şeması (`artemis/config/triggers.py`, `settings.py`):**
   - `TriggerConfig`, `TriggersConfig`, `MAX_TRIGGERS` ve `SuggestionChainConfig` ayrı
     `artemis/config/triggers.py` modülüne çıkarılıp `settings.py` içinden yeniden dışa
     aktarıldı.
   - `TriggerConfig.kind`: `Literal["folder", "app_opened", "calendar_before", "system_resume"]`
     (varsayılan `"folder"` geriye dönük uyumludur). `app`, `minutes_before`, `title_filter`,
     `cooldown_minutes` alanları türüne göre doğrulanır; `one_button` doğrulayıcısı her
     tetikleyicide en çok bir düğme (`suggest` veya `routine`) olmasını zorlar.
   - `SuggestionChainConfig`: `after_routine` / `after_trigger`, `work_profiles`,
     `active_app`, `time_range` (`SS:DD-SS:DD`), `cooldown_minutes`, sabit `message` ve
     `one_button` doğrulamalı tek düğme taşır; kendi kendini doğrudan yeniden öneremez.
   - `artemis/tools/plugins/` altında tetikleyici veya öneri zinciri ekleyen, düzenleyen ya
     da silen hiçbir `@register_tool` bulunmaz.

2. **LLM'siz olay değerlendiricisi ve güç olayı bağdaştırıcısı (`artemis/proactive/event_triggers.py`, `power_events.py`):**
   - `app_opened`: Enjekte edilebilir `ActiveAppProvider` (Windows varsayılanı
     `artemis.computer.windows.foreground_process()`, Windows dışında `None`) ile ilk
     taramada taban çizgisi alır; yalnız kapalı/arka plandan ön plana geçişte kenar
     tetikler ve `cooldown_minutes` uygular.
   - `calendar_before`: Enjekte edilebilir `CalendarEventsProvider` (varsayılanı
     `calendar_mail.resolve_secret` + `fetch_ics` + `parse_events`, en az 300 sn bellek
     önbellekli) ile yaklaşan toplantıları süzer ve `(trigger.name, event_key)` +
     `ProactiveStore.claim(...)` ile tekilleştirir.
   - `system_resume`: `artemis/channels/desktop/power_events.py` içinde Windows
     `WM_POWERBROADCAST` (`PBT_APMRESUMEAUTOMATIC` / `PBT_APMRESUMESUSPEND`) yakalanıp
     `DesktopBridge.notify_system_resume()` üzerinden zamanlayıcıya iletilir; ayrıca
     enjekte edilebilir `ResumeProbe` ve duvar/monotonik saat boşluğu denetlenir,
     `cooldown_minutes` uygulanır.
   - Koşullu öneri zincirleri (`evaluate_chains`) tetikleyici ateşlendiğinde ve `routine_run`
     tamamlandığında (`routine_run_end`) yalnız deterministik koşullarla değerlendirilir.

3. **Yerel teslim ve tıklamayla yürütme (`scheduler.py`, `delivery.py`, `window_events.py`):**
   - Tüm tetikleyici ve öneri zinciri bildirimleri sabit `fill()` + `sanitize_name()`
     şablonlarıyla kurulur ve `Deliverer.deliver(..., local_only=True)` ile gönderilir;
     Telegram hedeflerine asla eklenmez.
   - `routine_bar` bildiriminde en çok 1 eylem düğmesi gösterilir; tıklanmadan hiçbir şey
     çalışmaz ve `routine` çalıştırıldığında her adım `CommandRunner` → `SafetyGate.dispatch`
     üzerinden yürür.

4. **Ayarlar UI (`trigger_dialog.py`, `chain_dialog.py`, `triggers_section.py`, `settings_io.py`):**
   - *Ayarlar > Otomasyon Kuralları > Tetikleyiciler* altında 4 tetikleyici türü ve koşullu
     öneri zincirleri eklenip düzenlenebilir; tüm kayıtlar doğrulamalı `save_triggers`
     yolundan geçer.

**Sonuç:** Uygulama açılışı, yaklaşan toplantı, sistem uyanışı ve koşullu rutin öneri
zincirleri sıfır LLM aracıyla, deterministik şablonlarla, yerel gizlilikle (`local_only=True`)
ve tıklamaya bağlı `SafetyGate` disipliniyle çalışır.
