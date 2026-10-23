# F2 kapsamı ve paralel geliştirme sözleşmesi

Önce ARCHITECTURE, ADR 0006/0007 ve CLAUDE okunur. F2: iptal, süre aşımı,
döngü koruması, bütçe/yedek hata enjeksiyonu, kalıcı silme kaydı. Ses/UI/hafıza/
Telegram F2'ye alınmaz. Push ve ücretli model çağrısı yapılmaz.

## İptal ve kanal alanı

`CommandRunner.cancel_current() -> bool`: yalnız aktif turu iptal eder, bekleyen
turu etkilemez. Aktif tur bitince işaret ve görev temizlenir; sonraki tur çalışır.
`AgentLoop` sağlayıcı/gate beklemelerini context.cancelled ve remaining süresiyle
yarıştırır; iptal/timeout sonrası yeni model veya araç çağrısı yoktur. Dış task
iptali de finally ile temizlik yapar ve dürüst audit sonucu üretir.
`ToolContext` ek alanları: `turn_id: str = ''`, `deadline: float | None = None`.
AgentLoop bunları tur başlangıcında doldurur; Gate doğrudan kullanımda TurnState
değerlerini context'e taşır. Süre temizleme için işçi kısa sabit grace kullanır.

## Masaüstü süreç alanı

Yeni `artemis.tools.processes` içinde async sabit işçi çağrısı yardımcıları.
Yeni `artemis.tools.desktop_worker` yalnız `open_application`, `close_application`,
`read_clipboard`, `write_clipboard`, `open_browser` çağrılarına izin verir; stdin
JSON isteği, stdout tek JSON sonucu. Argüman/çıktı boyutu sınırlıdır; ham hata
gövdeleri/sırlar stderr veya repr'e yazılmaz. Worker mevcut WindowsDesktop sync
metotlarını kullanır. Platform dosyası bu alanın sahibidir.
`plugins/desktop.py` default yolu bu işçiye gider. context.services desktop ve
url_opener enjekte yolu test için korunur. Görev iptalinde işçi terminate/kill +
wait ile temizlenir; başka kullanıcı uygulamaları öldürülmez.

## Geri alma alanı

`artemis.safety.undo` SQLite UndoJournal + typed kayıtlar. Dosya
`settings.state_dir / 'undo.sqlite3'`; testler tmp_path state_dir kullanır.
`plugins/filesystem.py` DeleteFile kayıt → trash → doğrula → kayıt sonucu sırasını
uygular. Hazırlık, tamamlandı, başarısız, belirsiz durumları ayırt edilir.
Context.services['undo_journal'] hata enjeksiyonu için değiştirilebilir.
Yeni tek dosyalı plugin `plugins/undo.py`: `list_undo_records(limit=20)` READ_ONLY,
external=True, content_kind='file'. Liste sınırı 1..100. OS geri yükleme yönergesi
verilir; otomatik restore yapılmaz. Mutation script filesystem.failure çapasını
kullanır; değiştirilirse scripts sahibine bildir.

## Sağlayıcı/bütçe alanı

Settings'a `provider_timeout_seconds: float = 15.0` (gt0, le3600) eklenir. Router
her denemeye min(kalan süre, provider_timeout_seconds) ayırır ve asyncio timeout
ile uygular. Bulut timeout'u turun tüm süresini tüketmez, yerel yedeğe kalan süre
verilir. Kullanıcı iptali fallback başlatmaz. Auth/budget fallback başlatmaz.
Başarısız/iptal çağrının bilinmeyen maliyeti mevcut rezervasyonda kalır.
SQLite disk/lock/kayıt hataları yeni ağ çağrısına geçmeden güvenli hata üretir.

## Doğrulama

Yeni testler gerçek tmp_path FS/SQLite, sahte provider ve yalnız loopback HTTP
kullanır. Süreç testleri kontrollü Python bekleme/yazma işçisini tmp_path içinde
başlatabilir; gerçek masaüstüne dokunmaz ve sır aktarılmaz. Rapor F2 arızaları
ayrı ölçer. Bütün F1 kabul senaryoları ve üç mutasyon korunur.
