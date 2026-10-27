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
