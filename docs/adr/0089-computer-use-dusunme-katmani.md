# ADR 0089: Bilgisayar kullanımında hibrit arka plan düşünme katmanı

- Tarih: 2026-11-24
- Durum: Kabul edildi

**Bağlam:** Mevcut `ComputerSession` döngüsü tek adımda yalnız katı JSON eylem ister ve UI öğesi hemen görünmediğinde ("tam ekran video açık, denetimler otomatik gizli", "ikinci monitör odakta değil" ya da "düğme sayfanın altında") yürütücü model kolayca `ask_user` ile *"işareti göremedim, yapamam"* diyerek pes ediyordu. Ayrıca fareyi tıklamadan götüren `hover` eylemi ve çoklu monitör/tam ekran pencere bağlamı da yoktu. Her adımda ekstra planlama çağrısı yapmak basit işleri yavaşlatacağı için yalnız tıkanma/erken pes etme anında devreye giren hibrit yapı seçildi.

**Karar:**

1. **Adım içi keşif + `hover` + pencere/monitör bağlamı (`windows.py`, `actions.py`, `strict_rules.py`):**
   - `artemis/computer/windows.py` modülüne tıklamasız imleç taşıyan `hover(x, y)` ve `EnumWindows` tabanlı salt okunur `visible_windows()` (gizli/küçültülmüş olmayan üst düzey pencereler) eklenir.
   - `Action` şemasına `"hover"` eylem türü ve isteğe bağlı `"thought"` (kısa Türkçe akıl yürütme) alanı eklenir. `hover` yasaklı süreçlerden ve Artemis penceresinden yine engellenir, ancak yalnızca imleç taşıdığı için yanlış pozitif yıkıcı-öğe engeline takılmaz.
   - `SYSTEM_PROMPT`, `SYSTEM_PROMPT_UIA` ve `STRICT_COMPUTER_RULES`: hedef görünmüyorsa `ask_user` öncesi sırayla odaklama (`Ekran N` / arka pencere), `hover` / `"f"` ile tam ekrandan çıkma (asla `escape`), kaydırma / `ctrl+f` ve URL denetimi (`ctrl+l`) keşif merdivenini uygular.

2. **Hibrit Stratejist Katmanı (`artemis/computer/planner.py`, `artemis/computer/session.py`):**
   - Normal işlerde ek model çağrısı yapılmaz (sıfır ek gecikme).
   - Her adımda yürütücüye ve stratejiste görünür pencere özetleri (`[ÖN PLANDA]`, `[TAM EKRAN]`, `Ekran N`) sırlardan maskelenerek VERİ çerçevesiyle verilir.
   - Arka plan **Stratejist** yalnız şu durumlarda en fazla `MAX_STRATEGIST_CALLS = 2` kez sessizce (`on_step` ve `on_frame` üretmeden, yalnız `computer_strategist` denetim kaydıyla) çağrılır:
     1. Yürütücü gerçek bir kullanıcı/parola/ödeme sorusu sormak yerine `ask_user` ile *"göremedim / bulamadım / yapamam"* diye erken pes ettiğinde (`is_give_up_question`).
     2. Adım tekrar veya süreç/koordinat engeline takıldığında (bayat düzen `_Stale` hariç).
     3. Ardışık 2 adımda `observed` alanı ekranın ilerlemediğini bildirdiğinde (`is_no_progress`).
   - Stratejist **tüm monitörlerin taze görüntüsünü** alır (`emit_frame=False`) ve `StrategyVerdict` (`"try"` ile 1–5 adımlı Türkçe plan + hipotez ya da gerçekten kullanıcı gerekliyse `"ask_user"`) döndürür. `"try"` kararı çıkarsa `SessionMemory.strategy` güncellenir ve `hybrid` kipte bir sonraki adım zorunlu olarak tam ekran görüntüsüyle açılır.

3. **Güçlü Stratejist Model Yönlendirmesi (`settings.py`, `tools/plugins/computer.py`):**
   - `ComputerUseConfig.planner_model` eklenir (boşsa öncelik sırası: `planner_model` -> `hard_model` -> `primary_model` -> `vision_models`).
   - Gizlilik kuralları aynıdır: `allow_cloud_screen=False` veya `work_profile="private"` iken yalnız yerel (`ollama`) adaylar seçilir; görüntü kabul edebilen modeller (`claude_cli` dışı) önceliklendirilir.

**Sonuç:** Basit görevler hız kaybetmeden mevcut akışla sürer; 2. ekranda tam ekran oynatıcı veya gizli kontroller nedeniyle görünmeyen hedeflerde ise Artemis sessizce tam ekrandan çıkma/odaklanma/kaydırma gibi yolları planlayıp tamamlayabilir; tüm güvenlik kapıları (`Esc`, yasaklı süreçler, yıkıcı öğe kapısı, parola/ödeme koruması) değişmeden korunur.
