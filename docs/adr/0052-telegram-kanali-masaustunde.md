# ADR 0052: Telegram kanalı masaüstünde

Durum: kabul edildi

## Bağlam

İki yönlü Telegram kanalı (ADR 0016) ayrı `artemis telegram` süreci gerektiriyordu. Masaüstü
kullanıcısı için ikinci bir süreç ve terminal pratik değil (ADR 0051 paneli bunu kapsam dışı bıraktı).

## Karar

- **İsteğe bağlı:** `telegram.run_in_desktop` (varsayılan `false`). Servis yalnız bayrak, `chat_id`
  ve token varken ve demo değilken başlar. Ayarlar > Telegram'daki onay kutusu anahtarı yazar;
  servis çalışma zamanını yeniden kurmadan canlı açılıp kapanır.
- **Kendi iş parçacığı/döngü/runtime:** `TelegramService` daemon iş parçacığında kendi asyncio
  döngüsünü açar; runtime (SQLite, döngü ilişkisi) o iş parçacığında kurulur ve orada kapanır.
  Konuşma geçmişi panelinkinden ayrıdır (ADR 0016). Onay yalnız Telegram düğmesiyle (`channel.approve`)
  sorulur; masaüstü onay penceresi kullanılmaz. `computer_use` burada yoktur.
- **İkinci proaktif zamanlayıcı yok:** masaüstünün zamanlayıcısı vardır; gömülü kip kendi
  zamanlayıcısını başlatmaz (çift teslim riski). Proje iş deposunun süreç içi kilidi de alınmaz,
  yoksa kanal önce kurulursa masaüstünün proje araçları kapanırdı (`with_code_agent=False`).
- **HTTP 409:** başka bir yoklayıcı (örn. `artemis telegram`) varsa kanal yeniden denemeden
  durur ve panelde "Çakışma" gösterilir. Diğer geçici ağ hataları mevcut üstel geri çekilmeyle
  sürer. Kurulum arızasında yalnız hata türü raporlanır.
- **Eşleştirme:** panelin getUpdates yoklaması kanalla çakıştığından, kanal çalışırken
  "Eşleştir" kapalıdır.
- **Yaşam döngüsü:** açılışta (açıksa) başlar; `_finish_exit` içinde çalışma zamanı kapanmadan
  önce durdurulur (sınırlı bekleme).
- `run_telegram` (CLI) aynı `serve_telegram` çekirdeğini `embedded=False` ile kullanır; çıktılar
  ve dönüş kodları (2/2/2/1/0) değişmez.

## Sonuçlar

- Tek süreçle Telegram'dan komut alınır; Artemis kapalıyken kanal da kapalıdır.
- Aynı süreçte iki runtime aynı SQLite dosyalarını (hafıza, hatırlatma) ayrı bağlantılarla kullanır;
  `persist_memory` açıksa kanal ve panel geçmişi yeniden başlatmada aynı depodan yüklenir.
- Uzun süren bir tur sırasında durdurma, tur bitene ya da bekleme sınırına kadar sürebilir.
- Gerçek bot, ekran ve Windows kasası yalnız kullanıcının makinesinde doğrulanır.
