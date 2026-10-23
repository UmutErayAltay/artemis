# ADR 0031 — Pencere ve sistem kontrolü

**Bağlam:** Faz 7b şu araçları ekler: açık pencereleri listeleme, öne getirme,
büyütme/küçültme/yarım ekrana yerleştirme, ses ve parlaklık ayarı, kilitleme, uyku, kapatma
ve yeniden başlatma. Kaçınılan hatalar: kilit ve uykuyu SAFE saymak, ses/parlaklık sonucunu
doğrulamamak ve pywin32'ye dayanmak.
Artemis pywin32 kullanmaz; Windows'a özgü işler ya `ctypes` (pano) ya da sabit PowerShell
betiği (toast, ekran OCR) ile yapılır. Mevcut kurallar: kapatma onay gerektirir (CLAUDE.md
3), başarı doğrulanmadan bildirilmez (5), gözetimsiz turda yalnız READ_ONLY çalışır (ADR
0027), ekran içeriği gizlidir (ADR 0029).

**Karar:**

- **Tek servis, sabit betikler.** `artemis/tools/system_control.py` her işlemi sabit bir
  PowerShell betiğiyle (`-EncodedCommand`) yapar; parametreler argv'ye değil ortam
  değişkenlerine, yalnız doğrulanmış değerler olarak gider. Kapatma/yeniden başlatma
  `shutdown.exe`'nin sabit bayraklarıyla çalışır. Alt süreç `child_environment()` alır,
  zaman aşımına tabidir; çıktı tek JSON satırıdır ve sınırlıdır. Çalıştırıcı enjekte
  edilir; testler PowerShell çalıştırmaz.

- **Beş araç, risk işleme göre.**

  | Araç | Risk | Not |
  | --- | --- | --- |
  | `window_list` | READ_ONLY | Görünür pencereler (en çok 50): kimlik, başlık, süreç adı |
  | `window_control` | SIDE_EFFECT | `focus`, `maximize`, `minimize`, `restore`, `left`, `right` |
  | `volume_set` | SIDE_EFFECT | Düzey 0–100 ve/veya sessiz |
  | `brightness_set` | SIDE_EFFECT | Yalnız dahili ekran (WMI); yoksa dürüstçe "desteklenmiyor" |
  | `power` | işleme göre | `lock`, `cancel_shutdown` SIDE_EFFECT; `sleep`, `shutdown`, `restart` CONFIRM_REQUIRED |

  Kilit geri alınabilir ve iş kaybettirmez; uyku, kapatma ve yeniden başlatma kaydedilmemiş
  işi ve çalışan kod ajanı işlerini etkiler, bu yüzden her seferinde onay ister. Oturumu
  kapatma (`sign out`) gecikme verilemediği için bilerek yoktur.

- **Kapatmada iptal penceresi.** `shutdown` ve `restart`,
  `system_control.shutdown_delay_seconds` (varsayılan 60, en az 30) sonra çalışır; bu
  sürede `power(action="cancel_shutdown")` ya da Windows'un kendi uyarısı işlemi
  durdurabilir. Araç komutun verildiğini bilir, gerçekleştiğini değil:
  sonuç `status="sent"`, `success=False` ve dürüst bir mesajdır. Kilit ve uyku da aynı
  nedenle `sent` döner.

- **Doğrulama.** Pencere işlemleri sonrası durum geri okunur (ön plan penceresi, büyütülmüş/
  simge durumu, dikdörtgen yarım çalışma alanında mı). Ses ve parlaklık ayarlandıktan sonra
  geri okunur; ±1 farkla eşleşmiyorsa başarısızdır. Windows odak değiştirmeyi engellerse
  (ön plan kilidi) bu söylenir, başarı denmez.

- **Pencere başlıkları ekran içeriğidir.** Başlık e-posta konusu, belge adı ya da web sayfası
  başlığı taşıyabilir: `window_list` sonucu `external=True` ve `content_kind="screen"`'dir.
  Böylece turu lekeler (sonraki yan etki onay ister) ve `perception.allow_cloud_screen`
  kapalıyken bulut modele gitmez; tur yerel modelle sürer. Başlıklar kontrol
  karakterlerinden temizlenir ve kısaltılır.

- **Pencere kimliği doğrulanır.** `window_control` yalnız sayısal kimlik alır; betik
  pencerenin hâlâ var ve görünür olduğunu denetler, yoksa "pencere bulunamadı" der. Kimlik
  yeniden kullanılmışsa yanlış pencereye dokunma riski düşüktür ve işlemler geri alınabilir.

**Bitiş şartı (testle):**
- Risk tablosu ve gözetimsiz turda hiçbir yazma işleminin çalışmadığı.
- `shutdown`/`restart`/`sleep` onaysız çalışmaz; reddedilince betik çağrılmaz.
- Kapatma gecikmesi en az 30 sn; argv sabit bayraklardan oluşur, kullanıcı metni taşımaz.
- Geri okuma uyuşmazlığı, zaman aşımı, bozuk JSON ve Windows dışı platform dürüst hata verir.
- `window_list` turu lekeler ve gizli sayılır.

**Sonuçlar:**
- Her işlem bir PowerShell başlatır (ses/pencere için C# derlemesi dahil ~1–2 sn).
  Gecikme ölçülürse kalıcı yardımcı süreç ayrı ADR ile düşünülür.
- Çoklu monitörde yerleştirme pencerenin bulunduğu monitörün çalışma alanını kullanır.
- Davranış ancak kullanıcının Windows 11 kabulüyle doğrulanır.
