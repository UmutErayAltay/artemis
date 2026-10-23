# ADR 0030 — Windows açılışında başlama ve tek örnek

**Bağlam:** Faz 6a hatırlatma ve brifingleri yalnız Artemis açıkken teslim eder; kullanıcı
paneli her açılışta elle başlatmazsa kaçan hatırlatma birikir. Tepsi simgesi zaten var
(pencere kapanınca tepsiye iner), fakat Windows açılışında başlama ve "Artemis zaten açık mı"
denetimi yok. İki masaüstü örneği aynı `state_dir` üzerinde çalışırsa aynı SQLite
depolarını, aynı hatırlatma zamanlayıcısını ve mikrofonu paylaşmaya çalışır. Mevcut kurallar:
kayıt defteri değişikliği onay gerektirir (CLAUDE.md 3), mikrofon yalnız kullanıcı açınca
açılır (ses kanalı kuralı), başarı doğrulanmadan bildirilmez (5).

**Karar:**

- **Açılışta başlama yalnız kullanıcı eylemiyle kurulur, model araçla kuramaz.** Giriş
  noktaları `artemis autostart ac|kapat|durum` komutu ve tepsi menüsündeki "Windows
  açılışında başlat" seçeneğidir. Her ikisi de kullanıcının doğrudan tıklaması/komutudur;
  bu, kayıt defteri değişikliği için açık onaydır. Bir LLM aracı EKLENMEZ: model kendi
  kendini kalıcı hale getiremez (kalıcılık, saldırganın ilk hedefidir).

- **Yalnız kullanıcı kovanı.** Kayıt `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
  altında `Artemis` değeridir; yönetici izni istemez, başka kullanıcıyı etkilemez. Görev
  zamanlayıcısı ya da Başlangıç klasörü kısayolu kullanılmaz (biri daha fazla izin, öteki
  COM/kısayol üretimi ister).

- **Komut sabittir ve geri okunur.** Değer `pythonw.exe -m artemis desktop --arka-plan
  --config <mutlak yol>` biçimindedir; `pythonw.exe` yoksa `sys.executable` kullanılır.
  Argümanlar `subprocess.list2cmdline` ile tırnaklanır. Yazdıktan sonra değer geri okunur;
  beklenenle aynı değilse işlem başarısız sayılır. `--ses` asla eklenmez: mikrofon
  açılışta kendiliğinden açılmaz. Her değişiklik `autostart` denetim olayına yazılır.
  Windows dışında komut dürüstçe "yalnız Windows'ta" der.

- **`--arka-plan` paneli göstermez.** Masaüstü tepsiye iner, runtime ve hatırlatma
  zamanlayıcısı çalışır. Tepsi yoksa (nadir) panel yine gösterilir; görünmez bir süreç
  bırakılmaz.

- **Tek örnek: yerel soket.** Masaüstü açılışta `QLocalServer` ile kullanıcıya özel bir ad
  dinler (ad, kullanıcı adı ve çözülmüş `state_dir`'in özetinden türer). İkinci örnek önce
  bu ada bağlanmayı dener: bağlanırsa sabit `show` iletisini gönderir ve çıkar; ilk örnek
  paneli öne getirir. `--arka-plan` ile başlayan ikinci örnek paneli de açmaz, sessizce
  çıkar. Protokol tek sabit iletidir, en çok 16 bayt okunur, başka ileti yok sayılır:
  soket komut kanalı değildir. Windows'ta soket yalnız aynı kullanıcıya açıktır
  (`UserAccessOption`). Bağlantı kurulamıyorsa (bayat soket dahil) bu örnek birincil olur.
  Soket pencere kurulmadan ÖNCE sahiplenilir; aynı anda başlayan iki örnekten dinlemeyi
  kaybeden bir sonraki denemede kazanana bağlanıp çıkar. Bayat soket yalnız hiçbir sahip
  yanıt vermiyorsa kaldırılır. Bekleyen bağlantı sayısı sınırlıdır (8), 1 sn içinde ileti
  göndermeyen bağlantı kapatılır.

- **Geç gelen tepsi.** Oturum açılışında Explorer tepsisi Artemis'ten sonra hazır olabilir;
  `--arka-plan` tepsiyi yaklaşık 30 sn yeniden dener, ancak yine yoksa paneli gösterir.

- **Kurulum varsayımı.** Run değeri `-m artemis` ile başlar ve çalışma dizini System32
  olur; bu yüzden paket aynı ortamda kurulu olmalıdır (`pip install -e .`). `durum`,
  kayıttaki yorumlayıcı silinmişse kaydı "eşleşmiyor" sayar.

**Bitiş şartı (testle):**
- Komut üretimi: `pythonw` tercihi, tırnaklama, mutlak config, `--ses` yokluğu.
- Kayıt yazma/silme/okuma sahte kayıt deposuyla; geri okuma uyuşmazsa başarısızlık.
- Windows dışında `autostart` dürüst hata verir, kayıt deposu çağrılmaz.
- İkinci örnek `show` gönderip çıkar; ilk örnek paneli gösterir; bilinmeyen ileti yok
  sayılır; `--arka-plan` ikinci örnek paneli açmaz (offscreen Qt testi).
- Gerçek kayıt defteri testi yoktur; yalnız Windows'ta elle doğrulanır.

**Sonuçlar:**
- Artemis Windows oturumu açılınca tepside başlar; hatırlatma ve brifingler kaçmaz.
- Telegram kanalı ayrı süreçtir ve tek örnek kuralının dışındadır; çift teslimi 6a'daki
  SQLite talep kaydı zaten önler.
- Kurulum paketi ve imzalı ikili yoktur; kayıt, kullanıcının Python ortamına bağlıdır. Ortam
  taşınırsa `artemis autostart ac` yeniden çalıştırılır (`durum` bunu gösterir).
