# ADR 0034 — Ayarlar'dan model sağlayıcı bağlama

**Bağlam:** ADR 0033 uçları yalnız YAML ve ortam değişkeniyle tanımlatıyordu; teknik olmayan
kullanıcı için terminalsiz kurulum yoktu ve sağlayıcı yoksa ilk tur sebepsiz patlıyordu.

**Karar:**

- Ayarlar > "Model sağlayıcıları" (`provider_section.py`): şablondan uç ekleme, anahtar girişi
  (parola alanı), bağlantı testi (`GET {base_url}/models`, yönlendirme yok, anahtar yalnız
  gerekliyse gider), model ekleme ve birincil model seçimi. Yazım `_write_validated` ile atomik.
- Anahtar yalnız Kimlik Bilgisi Yöneticisi'ne (`Artemis/<id>`) yazılır; YAML'a, loga, mesaja
  ve geri forma girmez. Kasa yoksa (Windows dışı) `ARTEMIS_<ID>_API_KEY` yönlendirmesi gösterilir.
  Uç silinince kasadaki anahtar da silinir; modeli olan uç silinmez.
- `provider_problem(settings)` ağsız hazırlık denetimidir; masaüstü açılışında sebep dönerse
  Ayarlar bu bölümde açılır. Kayıttan sonra çalışma zamanı Uygula ile aynı yoldan
  (`_restart_runtime`) yeniden kurulur. Ücretli model `allow_paid` kapalıyken yine engellenir;
  arayüz bu ayarı değiştirmez, yalnız ipucu verir.
- Demo modunda bölüm devre dışıdır.
