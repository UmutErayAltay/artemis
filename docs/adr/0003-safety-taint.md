# ADR 0003 — Tek kapı ve tur taint'i

**Bağlam:** Kanal başına onay mantığı kaçış yolları oluşturur; dış içerik modele
komut vermeye çalışabilir.

**Karar:** SafetyGate bütün çağrıları doğrular ve dispatch eder. Risk varsayılanı
yan etkidir. CONFIRM_REQUIRED her zaman, diğer yan etkiler taint sonrası onay
ister. Onay `(tool_name, arguments) -> bool` alır. Taint tur bitene kadar kalır.
Yol politikası ortak safe_join'dır; araç çıktısı veri olarak çerçevelenir.

**Sonuçlar:** Onay araç ve gerçek argümanları gösterir. İlgisiz dış veriyi okumak
bile tur içinde ek onay yaratabilir; güvenlik lehine bilinçli tercih edilir.
