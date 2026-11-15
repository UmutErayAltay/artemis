# ADR 0065 — Yerel dosya araması

**Bağlam:** "Geçen ayki fatura nerede", "içinde 'kira sözleşmesi' geçen belge" gibi istekler,
kullanıcının seçtiği klasörlerde tam metin arama ister. Gömme (embedding) gerekmez; vault'un
sözcük araması dosya başına bellekte tarar, binlerce dosyada yetmez.

## Karar

1. **Kapalı ve kullanıcı yazımı.** `file_search.enabled: false`; `roots` yalnız config/Ayarlar
   panelinden gelir, hiçbir araç kök eklemez. Kapalıyken araç registry'den çıkar
   (`without_prefix`) ve `refuse` kapıda da reddeder. Kök `state_dir` ya da beceri klasörünün
   içindeyse/eşitse reddedilir; kök onları içeriyorsa gezinti o klasörleri atlar (hafıza
   dosyaları ve indeksin kendisi dizinlenmesin).
2. **İndeks.** `state_dir/dosya_arama.sqlite3`, SQLite FTS5 (`unicode61`). Açılışta FTS5 yoksa
   özellik dürüst mesajla kapanır. Artımlı: `(yol, boyut, mtime_ns)`; silinen dosya satırı
   kalkar. Yarım koşu (iptal, dosya üst sınırı) hiçbir satırı silmez. Dizinleme daemon iş
   parçacığında (`FileIndex.start`, iptal edilebilir, kapanışta `shutdown`), tek iş parçacığı sınırı
   `except Exception` içinde; dosya düzeyi hatalar (OSError, bozuk zip vb.) yalnız o dosyanın
   gövdesini boş bırakır, koşu sürer. Bağlantı iş parçacıkları arasında paylaşılmaz, her işlem
   kısa ömürlü bağlantı + kilit + 5 sn busy timeout kullanır.
3. **Sınırlar.** Koşu başına en çok 50 000 dosya; `max_file_mb` (5) üstü dosya hiç dizinlenmez;
   docx içindeki `document.xml` beyan ve gerçek okuma olarak 20 MB'a sınırlı (zip bombası),
   XML ayrıştırıcısı yerine etiket atma kullanılır. Symlink ve junction ne izlenir ne dizinlenir
   (yalnız kök dışına çıkanlar değil, hepsi: daha basit ve daha güvenli).
4. **Türler.** Metin ailesi (txt, md, csv, json, log, kaynak kodu...) ve docx içerik olarak;
   PDF için `uv.lock`'ta bir PDF metin kitaplığı YOK, bu yüzden PDF ve diğer tüm dosyalar yalnız
   ADIYLA aranır. Tavan: PDF içeriği aranamaz.
5. **Asla dizinlenmeyen.** `.env*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `*.kdbx`, `id_rsa*`
   (ve dsa/ecdsa/ed25519), adında `secret`/`token`/`password`/`credentials` geçenler.
6. **Araç.** `file_search(query, limit)`: READ_ONLY, `external=True`, `content_kind="file"`
   (tur lekelenir, `allow_cloud_file_content` kapalıyken gizli sayılır). Yol, tarih ve en çok 200
   karakterlik alıntı döner; bu kökler için dosyanın tamamını veren araç yoktur. Alıntı sorgu
   anında dosyadan yeniden okunur (indekste özgün metin tutulmaz). Yol ve alıntı dış metindir:
   denetim/yön/görünmez karakterler `sanitize_name` ile atılır. Sorgu sözcüklere ayrılır, her biri
   tırnaklı önek terimi olarak parametreyle `MATCH`'e verilir; söz dizimi karakteri hata vermez.
7. **Türkçe.** Metin ve sorgu önce `turkish_fold` (I→ı, İ→i), sonra `unicode61` ile sadeleşir
   (ş→s, ğ→g, ö→o, ü→u, ç→c). Eşleşmeyenler: noktasız ı ile i ayrı kalır; ek yalnız önek yönünde
   (sorgu "kira", belge "kirası" eşleşir; tersi eşleşmez). cp1254 (eski Windows) metni utf-8
   okunamazsa yedek çözümle okunur.
8. **Arayüz.** Ayarlar > Dosya araması: kutu ve kök listesi `PANEL_BINDINGS` ile Uygula'dan,
   durum satırı, "Yeniden dizinle", "Dizini sil" (indeks dosyasını siler; silinemezse söyler).
   Panel kendi `FileIndex` örneğini kurar; runtime'ınkine erişimi yoktur.

## Bilerek yok

Gömme/anlamsal arama, PDF metni, dosya izleyici (değişiklik yalnız açılışta ve düğmeyle
dizinlenir), tüm dosyayı okuyan araç, kök ekleyen araç, sonuçları Telegram'a gönderme (dosya
içeriği olduğu için zaten gizli sayılır).

## Ek (doğrulama sonrası sertleştirme)

- **Tek kural kümesi.** `Rules.check` hem gezginde hem `search`'te çalışır: gerçek yol (`realpath`)
  kayıtlı yola eşit olmalı (ara bileşen symlink/junction olduysa elenir), yol ŞU ANKİ config'teki
  bir kökün altında olmalı (kaldırılan kökün satırları dönmez), korunan klasörlerde (state, beceri)
  ve hariç/gizli/anahtar-deposu klasörlerinde olmamalı, ad gizli görünmemeli, düz dosya ve boyut
  sınırı içinde olmalı. Arama bir satırı OKUMADAN önce bunu uygular; geçemeyen satır sessizce düşer.
- **Gizli klasörler.** Adı gizli görünen her klasör ve `.ssh .aws .gnupg .azure .kube .docker
  .password-store` budanır. Gizli dosya adları genişledi: `.netrc/_netrc .npmrc .pypirc
  .git-credentials *.ppk *.jks *.keystore`, `Login Data`, `Cookies`, `key4.db`, `logins.json`.
- **Tek örnek.** `FileIndex.shared(settings, audit)` süreçte veritabanı yolu başına tek örnek verir
  (küçük bir modül düzeyi kayıt; CLAUDE.md kural 10'a bilinçli, gerekçeli istisna: tek kilit/iptal/iş
  parçacığı için). `start()` çalışırken hiçbir şey yapmaz. `clear()` iptal ister, iş parçacığı
  GERÇEKTEN bitene dek (sınırlı) bekler, bitmediyse silmez ve söyler; panel bunu QThread'de
  çalıştırır. Silmeden sonra iptal bayrağı açık kalır: dizinleyici kullanıcı "Yeniden dizinle"
  diyene ya da uygulama yeniden başlayana dek indeksi yeniden oluşturmaz (yazmadan önce de denetlenir).
- `search` sınırı kendi içinde 10'a kısılır; `snippet_around`, katlama uzunluğu değiştirirse
  (örn. ß) özgün metinde büyük/küçük harfe bakmadan arar, bulamazsa metnin başını alır.
