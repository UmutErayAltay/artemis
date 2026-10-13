# ADR 0017 — Seçili uygulamaları onaysız kapatma

**Bağlam:** Kullanıcı “brave i kapat” gibi günlük işlemlerde tekrar onay
istenmesini istemiyor. Varsayılan kapatma politikası tüm uygulamalarda onaylıydı.

**Karar:** `close_without_confirmation`, `applications` içinde tanımlanmış
takma adlardan oluşur; varsayılanı boştur. Araç doğrulanmış argüman ve ayara göre
`risk_for` ile SIDE_EFFECT veya CONFIRM_REQUIRED bildirir. Onay kararı yine tek
SafetyGate'te kalır. Dış içerik okunan turda SIDE_EFFECT onay ister.

**Sonuç:** Yerel Brave istisnası temiz komutta yeniden onay sormaz; diğer
uygulamalar ve silme varsayılan onay politikasını korur. İzin listesinde olmayan
istisna config doğrulamasında reddedilir. Bu ayar yeni uygulama izni vermez.
