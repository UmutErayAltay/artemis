# ADR 0087: Öğretilebilir makro rutin kaydedici

- Tarih: 2026-11-23
- Durum: Kabul edildi

**Bağlam:** Kullanıcılar her rutini JSON ya da form alanlarıyla tek tek yazmak yerine, bir işi
ekranda yaparken ya da doğal dil adımları yazarak öğretilebilir bir makro olarak kaydetmek
ister (ADR 0058 üzerine). Ancak küresel bir tuş kaydedici (`pynput` veya `WH_KEYBOARD_LL`)
kurmak parola, OTP, PIN ve kart bilgilerini yakalama riski doğurur; ayrıca kaydedilen adımların
sınırsız büyümesi, rutin içinden başka rutin çağırması (`routine_*`) ya da güvenlik kapısını
(`SafetyGate`) atlayarak doğrudan diske yazması kabul edilemez.

**Karar:**

1. **Enjekte edilebilir UIA/odak gözlemcisi (`artemis/proactive/macro_recorder.py`):**
   - `MacroObserverBackend` protokolü tanımlandı; Windows varsayılanı `WindowsMacroObserver`
     üçüncü parti tuş kancası (`pynput`) kullanmadan `artemis.computer.windows`
     (`foreground_process`, `foreground_window`, `ui_elements`) ve `GetCursorPos` /
     `GetAsyncKeyState(VK_LBUTTON)` ile yalnız uygulama odak geçişlerini, sol tıklamaları ve
     odaktaki denetimin güvenli üst verisini (`focused_control_info`) okur.
   - `focused_control_info` `ValuePattern.Value` değerini ASLA okumaz; `IsPassword` doğruysa
     denetim adını da boş bırakır. Testler sahte `MacroObserverBackend` enjekte ederek gerçek
     OS yan etkisi olmadan çalışır.

2. **Gizlilik ve sınır değişmezleri:**
   - `BLOCKED_PROCESSES` (`artemis.computer.actions.process_block_reason`: terminaller, parola
     yöneticileri, `code.exe`, `cursor.exe`, Artemis'in kendi PID'i) pencerelerinden gelen
     olaylar tamamen atlanır (`skipped_blocked_count`).
   - `IsPassword` denetimleri ve parola/PIN/OTP/kart/CVV/anahtar kalıpları (`is_sensitive_control`,
     `sanitize_macro_text`, `mask_screen_secrets`) ham değer saklamadan maskelenir/atlanır
     (`masked_count`); `ObservedEvent`, `MacroDraft`, adım önizlemesi ve denetim kaydına sır
     girmez.
   - Derlenen adımlar mevcut güvenli kayıtlı araçlara (`open_application`, `open_url`,
     `window_control`, `computer_use`) çevrilir; `MAX_STEPS` (`<= 12`) ile sınırlanır,
     `routine_*` araçları yasaklanır ve `validate_steps(steps, registry)` ile doğrulanır.

3. **Masaüstü makro penceresi ve onaylı kayıt/oynatma (`macro_dialog.py`, `routines_section.py`, `approval_dialog.py`):**
   - *Ayarlar > Otomasyon Kuralları > Rutinler* altına eklenen `"Makro kaydet…"` düğmesi
     `MacroDialog` penceresini açar. Kullanıcı canlı gözlemi başlatıp durdurabilir, doğal dil
     adımları yazabilir, maskelenen/atlanan öğe sayacını ve derlenen adımları canlı görür.
   - Kaydetme doğrudan `routines.json` yazmaz: `RoutinesSection.tool_requested` üzerinden
     `routine_save` çağrısı göndererek `CommandRunner` → `SafetyGate.dispatch` →
     `ApprovalDialog` onayından geçer. `ApprovalDialog` `routine_save` için numaralı adım
     önizlemesi gösterir.
   - Kaydedilen makronun oynatılması (`routine_run`) her adımı `context.gate_handle.dispatch`
     üzerinden `SafetyGate.dispatch` ile yürütür.

**Sonuç:** Öğretilebilir makro kaydı küresel tuş kancası kurmadan, parolaları ve engelli
uygulamaları sızdırmadan, `<= 12` adımla sınırlı ve hem kayıtta hem oynatmada `SafetyGate`
denetimine bağlı çalışır.
