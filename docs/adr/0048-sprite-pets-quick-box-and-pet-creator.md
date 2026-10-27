# ADR 0048: Sprite pet'ler, hızlı yazma kutusu ve pet üretici

- Tarih: 2026-10-27
- Durum: Kabul edildi (kullanıcı: küre varsayılan kalsın; Codex uyumlu pet'ler, ayarlarda
  seçme/yükleme, model seçilen bir sohbetle pet üretme; ücretsiz metin modeli ve ücretli resim
  modeli ikisi de; 1 tık hızlı kutu, 2 tık ses, 3 tık menü)
- ADR 0023'ü (pet yalnız QPainter, tık = ses) genişletir.

**Bağlam:** ChatGPT/Codex masaüstü pet'leri açık bir biçim kullanıyor: klasörde `pet.json`
(`id`, `displayName`, `description`, `spritesheetPath`) ve `spritesheet.webp|png`; 1536×1872,
8 sütun × 9 satır, hücre 192×208, saydam zemin. Satırlar ve kare sayıları: 0 idle 6, 1
running-right 8, 2 running-left 8, 3 waving 4, 4 jumping 5, 5 failed 8, 6 waiting 6, 7 running
(çalışma döngüsü) 6, 8 review (odaklanma) 6; kullanılmayan hücreler saydam. v2: 1536×2288, aynı
hücre, iki ek bakış satırı (yok sayılır). Hareketi azaltınca idle karesi 0 gösterilir.
Topluluk galerilerinde (codex-pets.net, petdex) çok sayıda pet var; lisansları belirsiz, bazıları
marka karakteri.

**Karar:**

1. **Görünüm iki tür:** mevcut QPainter küre (`orb`, varsayılan, depoda tek görsel) ve sprite
   pet'ler. PetWidget'ın durum, balon, konum, sürükleme, menü ve zamanlayıcısı ortaktır; yalnız
   çizim ve kare ilerletme sprite'ta ayrıdır (`if sprite … else orb`, ara arayüz yok).
2. **Durum eşlemesi:** idle→idle, off→idle (soluk, yavaş), listening→review, thinking→running,
   speaking→waving, approval→waiting, error→failed, kod işi bitti (başarılı)→jumping bir tur,
   sürükleme→running-right/left (yöne göre). Hücre BODY yüksekliğine ölçeklenir.
3. **Pet kaynakları:** `state_dir/pets/<id>/` (Artemis'in, yazılabilir) ve varsa
   `~/.codex/pets/<id>/` (salt okunur). Seçim pet'in QSettings'inde (`skin`), YAML'a girmez.
   Depoya hiçbir topluluk pet'i konmaz; kullanıcı kendi makinesine kurar.
4. **İçe aktarma (güven sınırı):** klasör ya da `.zip`. Yalnız `pet.json` + tek görsel kabul;
   her zip üyesi `safe_join(..., allow_symlinks=False)`, toplam açılmış boyut ≤ 25 MB, görsel
   ≤ 20 MB, boyut tam 1536×1872 ya da 1536×2288, `id` `^[a-z0-9][a-z0-9-]{0,63}$`, var olan id
   onaysız ezilmez. Görsel yalnız Qt `QImage` ile çözülür (Pillow yok).
5. **Tıklamalar:** 1 tık → pet'in altında hızlı yazma kutusu (ayrı çerçevesiz pencere), Enter
   `DesktopWindow._submit_text`'e gider; pet'in kendi komut/onay yolu yine yoktur. 2 tık → ses
   aç/kapa. 3 tık → menü. Onay beklerken her tık onaya odaklar (eski davranış).
6. **İş listesi:** pet kod işlerini (id, proje, durum: çalışıyor / girdi bekliyor / bitti /
   takıldı) tutar; balonda kısa liste, `code_agent_waiting` artık gösterilir.
7. **Hareketi azalt:** Windows'ta `SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION)` (platform
   kontrolünden sonra ctypes), yoksa mevcut ortam/QSettings anahtarı.
8. **Pet üretici (Ayarlar → Pet sekmesi):** küçük bir sohbet; model `settings.models`'tan
   seçilir. Çağrılar **ProviderRouter** ile yapılır (ücretli model `allow_paid` + bütçe defteri,
   `cloud_transfer`/`provider_call` denetimi). İki yol aynı animasyon motoruna gider:
   - **Metin modeli → SVG** (varsayılan, ücretsiz modelle): model tek bir karakter SVG'si
     (`viewBox="0 0 192 208"`, isteğe bağlı `id="eyes"`, `id="mouth"` grupları) döndürür.
     SVG beyaz listeyle temizlenir (DOCTYPE/ENTITY, script, foreignObject, dış href, olay
     öznitelikleri yok; ≤ 64 KB) ve `QSvgRenderer` ile çizilir.
   - **Resim modeli** (ücretli, açık onay): model tek bir karakter görseli üretir (OpenRouter
     sohbet tamamlamada görüntü çıktısı). Görsel yine `QImage` ile çözülür ve boyut sınırlanır.
   - **Animatör:** temel görselden 9 satırın karelerini prosedürel üretir (nefes/sallanma,
     zıplama yayı, yana yatma, titreme, göz kırpma için `eyes` gizleme, koşarken eğim) ve
     Codex biçiminde `spritesheet.webp` + `pet.json` yazar. Böylece üretilen pet Codex'te de
     çalışır ve tek animasyon kodu iki yolu da taşır.
   - Kullanıcı sohbetle düzeltir ("daha mavi"); önizleme kaydetmeden önce gösterilir.

**Sonuçlar:** Küre ve mevcut davranış korunur; yeni bağımlılık yok (QtSvg, WebP eklentisi
PySide6'da var). Topluluk pet lisansları kullanıcının sorumluluğundadır; README bunu yazar.
Resim modeli yolu ücretlidir ve `allow_paid` kapalıyken çalışmaz. Prosedürel animasyon el çizimi
piksel sanat kadar zengin değildir; amaç tutarlı karakter + okunur durumlardır.

## Ek (2026-10-27): resim modeli yolu

Kullanıcı geri bildirimi: SVG pet ve prosedürel animasyon kötü görünüyor; ChatGPT'nin maskot
pet'lerinde ayrıntılı çizilmiş animasyonlar var. Metin yolu (SVG + prosedürel animatör) aynen
kalır; **resim modeli yolu artık her durum için gerçek kare üretir.** Bu ek, madde 8'deki
"OpenRouter sohbet tamamlamada görüntü çıktısı" ifadesinin yerine geçer.

1. **Yapılandırma:** `ModelConfig.kind: chat|image` (varsayılan `chat`). `image` yalnız
   `openai_compatible` + uç ile geçerlidir ve birincil/yedek/bilgisayar kullanımı modeli olamaz
   (doğrulama hatası). `image_cost_usd` çağrı başına sabit üst tutardır. Doğrulanmış uç:
   kullanıcının yerel OpenAI uyumlu CLI proxy'si (abonelikten sunulur; `gpt-image-2.5`).
2. **`artemis/brain/images.py::generate_image`:** referans yoksa `POST /images/generations`
   (JSON: model, prompt, size, `background: transparent`, n=1), referans varsa `POST /images/edits`
   (multipart, dosya alanı `image`). Taban adres ve anahtar sohbet sağlayıcılarıyla aynı yardımcılardan
   gelir; `follow_redirects=False`, zaman aşımı 240 sn, yanıt ≤ 25 MB, yalnız `b64_json`
   (adresle dönerse açık hata: ikinci bir sunucuya gidilmez), PNG/WebP imzası. Ücretli kurallar:
   herhangi bir fiyat ya da `image_cost_usd` sıfırdan büyükse `allow_paid` şart (yoksa
   `BudgetExceeded`), `image_cost_usd` = 0 ise yanlış yapılandırma, tutar `BudgetLedger`'da ayrılır;
   hatada rezervasyon tutulur (sunucu işlemiş olabilir). Denetim: `cloud_transfer`
   (`content_kind="pet_image_prompt"`, `has_reference`) ve `provider_call`; görsel baytı ve anahtar
   yazılmaz. Görüntü çözme/boyut doğrulaması Qt tarafındadır (`QImage`, oran ve ≤ 4096).
3. **Hat (`pet_image_pipeline.py`):** temel görsel sabit stil şablonu (chibi maskot, tam boy,
   piksel-art çizgi, ortada, saydam zemin, yazı yok) + kullanıcı tarifi ile `generations`;
   düzeltme ("şapka ekle") mevcut temeli referans alan `edits`. Animasyon: onaylı temel referansıyla
   durum başına bir `edits` çağrısı, tam kare sayısıyla istem: idle 6, running-right 8, waving 4,
   jumping 5, failed 8, waiting 6, running 6, review 6 = **8 çağrı**; running-left, running-right'ın
   yatay aynasıdır (çağrı yok). Çağrı başına ~26-43 sn, toplam ~4-6 dk.
4. **Dilimleyici:** şerit saydam sütun boşluklarından bölünür; medyan genişliğin %15'inden dar
   parçalar (kıvılcım) komşuya katılır; sayı beklenenle tutmazsa eşit genişliğe düşülür. Opak
   zeminli şeritte dört köşeden renk toleranslı taşırmayla zemin kaldırılır. Kareler alfa sınırına
   kırpılır; **şerit içindeki dikey konum (zıplama yüksekliği) korunur**, satırın en alt tabanı o
   satırın zemini sayılır (karelerin tek tek tabana yapıştırılması zıplamayı yok ederdi).
5. **Yerleşim:** TÜM pet için tek ölçek (en büyük satır zarfı hücre yüksekliğinin %92'si, genişlik de
   sığar) uygulanır; böylece karakter satırlar arasında boyut değiştirmez. Kareler ortak zemine
   (y=204) hizalanır, yatayda 192×208 hücrede ortalanır; kullanılmayan hücreler saydam. Kurulum yine
   `pet_sprite.import_pet` ile (doğrulama tek yerde).
6. **Dayanıklılık:** satır başına en çok iki deneme; hâlâ olmazsa o satır temel görselden prosedürel
   animatörle üretilir (`pet_animator.procedural_row`) ve kullanıcıya hangi satırların yedek olduğu
   söylenir. `BudgetExceeded` yedeğe düşmez, üretimi durdurur. İptal çağrılar arasında gözlenir
   (süren çağrı bitene kadar bekler).
7. **Arayüz:** "Resim modeli (animasyonlu)" yöntemi; model listesi `kind`'a göre süzülür (SVG için
   sohbet, resim için resim modelleri). Sohbet → temel önizleme; her ek mesaj `edits`; "Animasyonları
   üret" 8 çağrıyı QThread'de ilerleme ("3/8: zıplama…"), satır satır canlı önizleme ve iptalle
   çalıştırır; "Kaydet ve seç". Ücretli modelde çağrıdan önce tutar × çağrı sayısı açıkça onaylatılır;
   ücretsiz proxy'de "abonelik kotasından düşer" notu gösterilir. Demo modunda kapalıdır.

**Sonuçlar:** Bir pet ≈ 9 resim çağrısı (1 temel + 8 şerit; her düzeltme +1) ve birkaç dakika
tutar; abonelik kotasından düşer. Karakter tutarlılığı modele bağlıdır (referans görsel ve sabit
istemle sağlanır, garanti değildir; bozuk satır yedeğe düşer). Canlı model doğrulaması bu eke dahil
değildir: testler sahte taşıma/üreteçle çalışır.
