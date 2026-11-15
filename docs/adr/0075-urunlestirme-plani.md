# ADR 0075 — Ürünleştirme planı ve kullanıcı başına ayar klasörü

**Bağlam:** Bugüne dek Artemis geliştirici deposundan çalıştırıldı; her CLI alt komutu
`--config` varsayılanı olarak çalışma dizinine göreli `config/config.yaml` bekliyordu. `load_settings`
(`artemis/config/settings.py`) `state_dir`, `workspace` ve `audit_path` gibi göreli yolları config
dosyasının bulunduğu klasöre göre çözer. Paketlenmiş bir `.exe` (ör. `Program Files` altında) kendi
yanına yazamaz; ayrıca kurulum, ilk kurulum deneyimi, yerel hafıza şifrelemesi ve dağıtım kararları
fazlara ayrılmalıdır.

## Karar

1. **Faz 1 — Kullanıcı başına config/durum klasörü (bu ADR, yapıldı):**
   - `artemis/config/paths.py` eklendi. Çalışma dizininde `config/config.yaml` varsa geliştirici
     iş akışı aynen korunur ve o dosya kullanılır.
   - Yoksa Windows'ta `%APPDATA%\Artemis\config.yaml` (`APPDATA` yoksa `~/AppData/Roaming/Artemis`),
     diğer sistemlerde `$XDG_CONFIG_HOME/artemis/config.yaml` (`~/.config/artemis`) seçilir.
   - Varsayılan yoldan açılışta dosya yoksa paketle gelen `config/config.example.yaml` (dondurulmuş
     pakette `sys._MEIPASS` altından) bir kez kopyalanır; var olan dosyanın üzerine asla yazılmaz.
     Açık `--config` verildiğinde otomatik oluşturma yapılmaz, mevcut hata davranışı korunur.
   - `load_settings` göreli yolları zaten config dosyasının ebeveynine göre çözdüğü için `.artemis`,
     `desktop` ve `audit.jsonl` kendiliğinden kullanıcı klasörünün altına yerleşir.
2. **Faz 2 — Windows paketi (planlandı):**
   - PyInstaller `onedir` + Inno Setup kurucu.
   - Ağır isteğe bağlı ses modelleri (`faster-whisper large-v3`, EMA, DeepFilterNet) kurucuya
     gömülmez; ilk kullanımda açık kullanıcı onayıyla indirilir.
   - İmzasız kurucu Windows SmartScreen uyarısı verir (açık konu: kod imzalama sertifikası).
3. **Faz 3 — İlk kurulum sihirbazı (planlandı):**
   - Model sağlayıcısı seçimi ve API anahtarının Windows kimlik kasasına yazılması, mikrofon ve ses
     testi.
   - Tüm gizlilik izinleri (`allow_paid`, `allow_cloud_*`, ekran/bilgisayar kullanımı) varsayılan
     kapalı kalır.
4. **Faz 4 — Hafıza güvenliği (planlandı):**
   - `memory.sqlite3` ve `hafiza/` dosyaları için Windows DPAPI tabanlı şifreleme.
   - Varsayılan saklama süresi sihirbazda kullanıcıya açıkça sorulur; sessizce veri silinmez.
5. **Faz 5 — Güncelleme denetimi ve çökme raporu (planlandı):**
   - Güncelleme denetimi yalnız sürüm sorgusu yapar, otomatik indirme/çalıştırma yoktur.
   - Çökme raporu yereldir ve isteğe bağlıdır.
6. **Açık kararlar (kullanıcıda):**
   - `LICENSE` türü: depo public ancak henüz lisans dosyası yok; dağıtımdan önce seçilmeli.
   - Windows kod imzalama sertifikası.

## Sonuçlar

- Geliştirici deposunda `config/config.yaml` mevcutken hiçbir davranış değişmez.
- Paketlenmiş kurulumda ilk açılışta `%APPDATA%\Artemis\config.yaml` oluşturulur ve stderr'e Türkçe
  bilgilendirme satırı yazılır.
- Test: `tests/test_config_paths.py` (gerçek `APPDATA` veya kullanıcı dizinine dokunmaz).
