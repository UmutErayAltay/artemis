# ADR 0061 — Tetikleyiciler

**Bağlam:** Artemis, sorulmayı beklemek yerine bir olayı fark edip yardım önermeli ("İndirilenler'e
yeni dosya geldi: rapor.pdf"). İlk olay türü: izlenen klasöre yeni dosya gelmesi.

## Karar

1. **Tetikleyici yalnız config'te tanımlanır** (`triggers.enabled: false`, `triggers.items`, en çok
   10: `name`, mutlak `folder`, `pattern`, `message?`, `suggest?`, `routine?`). Onu kuran ya da
   değiştiren araç YOKTUR; kalıcı otomasyonu kullanıcı yazar (brifing ile aynı neden, ADR 0027).
   Ayarlar > Tetikleyiciler salt okunur liste ve "son bildirim" gösterir.
2. **Yoklama, mevcut zamanlayıcı taramasında** (`os.scandir`, özyinelemesiz, yeni bağımlılık ve
   izleyici iş parçacığı yok; tarama `to_thread` ile olay döngüsünü tutmaz). İlk tarama yalnız taban
   çizgisidir. Dosya, boyutu iki yoklamada aynı kalınca bir kez bildirilir; `.crdownload`, `.part`,
   `.tmp` vb. yok sayılır. Görülen durum proaktif SQLite deposundadır (`trigger_files`), yeniden
   başlatma tekrar bildirmez. Sembolik bağlantılar, klasörler ve UTF-8'e çevrilemeyen adlar atlanır.
   Bir taramada en çok 2000 girdi okunur. Klasör okunamazsa o tetikleyici atlanır, `trigger_unavailable`
   tetikleyici başına bir kez denetime yazılır; tek bozuk tetikleyici diğerlerini durdurmaz.
3. **LLM yok.** Bildirim, kullanıcının `message` şablonundan (`{name}`, `{file}`) sabit metindir;
   şablon `str.format` değil düz değiştirmedir. Dosya adı dış metindir: denetim ve yön karakterleri
   atılır, 120 karaktere kesilir, talimat sayılmaz.
4. **Kendiliğinden eylem yok.** Bildirimin en çok bir düğmesi olur: `suggest` ({path} dolu) metni
   sohbet kutusuna YAZAR, göndermez; `routine` ADR 0058'in aynı "Çalıştır" yolundan normal, onaylı
   tur başlatır. Bu amaçla ADR 0058'in bildirim satırı genişletildi (`Deliverer.deliver(suggest=)`,
   `Proactive.suggest`); ikinci arayüz yok. Aynı değişiklik, rutin bildirim satırının daha önce
   `Proactive` olayından hiç gösterilmediğini de düzeltti. `routines.enabled` kapalıysa düğme konmaz.
5. **Sınır ve talep:** bildirimler `proactive.max_events_per_day`'e sayılır; sınır doluysa dosyalar
   beklemede kalır, ertesi gün bildirilir. Bir taramadaki dosyalar tek bildirimde birleşir. Talep
   kimliği (tetikleyici + dosya kümesi özeti, yerel gün) iki sürecin çift bildirmesini önler.
6. **Gizlilik:** dosya adı kişisel olabilir; tetikleyici bildirimleri Telegram'a hiç gitmez
   (`local_only`).

## Bilerek yapılmayanlar

- Özyinelemeli izleme, silinme/değişme olayları, başka olay türleri (v1 yalnız yeni dosya).
- Tetikleyiciyi sohbetle (araçla ya da modelle) kurma ya da düzenleme. Panelden kullanıcı eliyle yazım için Ek'e bak.
- Dosya içeriğini okuyup özetleme: öneri metni kullanıcıya bırakılır, gönderimi o yapar.
- Teslim başarısızsa yeniden deneme: dosyalar talep edildiği anda "bildirildi" sayılır (en çok bir
  kez, ADR 0027 ilkesi); başarısızlık `failed` olarak kaydedilir.
- Aynı adla silinip yeniden gelen dosyanın yeniden bildirilmesi sınırlaması: silinen dosyanın kaydı
  düşer, aynı ad yeniden gelirse yeni dosya sayılır.

## Ek: Ayarlar formundan tetikleyici (2026-11)

Kullanıcı artık tetikleyiciyi Ayarlar > Tetikleyiciler'deki formdan ekleyebilir, düzenleyebilir ve
silebilir; `config.yaml`'ı elle düzenlemek gerekmez. Form `TriggerConfig` ile doğrular ve aynı
doğrulamalı, atomik config yazımını kullanır (`save_triggers`). Değişmeyen kural: tetikleyiciyi hiçbir
araç ve hiçbir model kuramaz ya da değiştiremez; bu yazım yalnız panelde kullanıcının tıklamasıyla
çalışır, araç kaydı ve SafetyGate yolu yoktur. Yazılan değişiklik çalışan uygulamaya Uygula (çalışma
zamanı yeniden kurulur) ya da yeniden açılışla ulaşır. Zamanlı işler kapalıysa panel uyarır; kutuyu
yalnız kullanıcı işaretler ve Uygula ile kaydeder.
