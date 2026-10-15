# Faz 6–7 Windows kabul listesi

Bu liste Umut'un Windows 11 makinesinde, yerel Claude Code oturumuyla yürütülür. Bulut
oturumu bunları yapamaz: kayıt defteri, tepsi, ekran, ses, güç ve gerçek Chromium yalnız
burada denenebilir. Her maddenin sonucunu (geçti / kaldı + gözlem) bu dosyanın sonundaki
tabloya yazın; bir madde kalırsa düzeltmeyi bulut oturumuna değil önce buraya not edin.

**Yerel Claude Code için kurallar:** Testler gerçek makineye dokunmaz kuralı bu listede
bilerek esnetilir, çünkü amaç gerçek davranışı görmektir. Ama: kapatma/yeniden başlatma
denemesinden önce açık işler kaydedilir; her adımda Jarvis'in onay penceresi kullanıcıya
gösterilir, Claude Code onu kendi tıklamaz. Sır (token, anahtar) ekrana veya dosyaya
yazılmaz.

## Hazırlık

```powershell
git fetch origin claude/mt3ui55os-readme-gpugv7
git checkout claude/mt3ui55os-readme-gpugv7
python -m pip install -e ".[dev,desktop,browser]"
python -m playwright install chromium
pytest -q                      # Windows'ta da yeşil olmalı
```

`config/config.yaml` içinde ilgili blokları açın: `proactive`, `perception`, `mcp.servers`
(örnekler `config/config.example.yaml`'da).

## 6a — Hatırlatma ve brifing (ADR 0027)

1. `proactive.enabled: true`. Panelde "2 dakika sonra su içmeyi hatırlat" → 2 dk sonra panelde
   ve toast'ta hatırlatma görünür; denetimde `proactive_delivery` `delivered`.
2. Jarvis'i kapatın, kapalıyken bir hatırlatmanın zamanı geçsin, yeniden açın → kaçan
   hatırlatma bir kez teslim edilir (iki kez değil).
3. Sabah brifingi için `briefings` altına 3 dk sonrası bir saat yazın → brifing gelir; brifing
   sırasında yan etkili hiçbir araç çalışmaz (denetimde `unattended=True` red).

## 6b — MCP istemcisi (ADR 0028)

4. Config örneğindeki dosya sistemi sunucusunu (`npx @modelcontextprotocol/server-filesystem`)
   bir test klasörüyle ekleyin → panel açılışında denetimde `mcp_ready`; "test klasöründeki
   dosyaları listele" çalışır.
5. Yazma aracı (`write_file`) onay penceresi açar; reddedince dosya oluşmaz.
6. Görev Yöneticisi'nde çağrı bitince `node` süreci kalmaz.

## 6c — Ekran okuma (ADR 0029)

7. `perception.screen_enabled: true`. Bir not defterine "Merhaba dünya ğüşiöç" yazın,
   "ekrandaki metni oku" → onay penceresi kapsamı gösterir; onaylayınca metin doğru okunur.
8. %125/%150 ölçeklemede etkin pencere doğru bölgeden okunur (kayma yok).
9. Türkçe dil paketi yoksa dürüst "OCR dil paketi bulunamadı" mesajı.

## 7a — Açılışta başlama ve tek örnek (ADR 0030)

10. `python -m jarvis autostart ac --config config/config.yaml` → "açık"; `durum` → "açık".
    `regedit`'te `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Jarvis` değeri
    `pythonw.exe ... desktop --arka-plan --config "<mutlak yol>"` ve `--ses` YOK.
11. Oturumu kapatıp açın → Jarvis konsol penceresi açmadan tepside başlar; mikrofon kapalıdır.
12. Jarvis açıkken ikinci kez `python -m jarvis desktop` → yeni pencere açılmaz, mevcut panel
    öne gelir. Tepsideyken de aynı.
13. Tepsi menüsündeki "Windows açılışında başlat" işaretini kaldırın → `durum` "kapalı";
    kayıt silinmiştir.

## 7b — Pencere ve sistem kontrolü (ADR 0031)

14. "Açık pencereleri listele" → başlıklar ve süreç adları doğru; görünmeyen/araç pencereleri
    listede yok.
15. "Not defterini sol yarıya al", "büyüt", "küçült", "öne getir" → her biri gerçekten olur;
    çoklu monitörde pencerenin bulunduğu ekranın yarısına gider.
16. "Sesi 25 yap", "sesi kapat/aç" → Windows ses düzeyi değişir, yanıt geri okunan değeri söyler.
17. Dizüstündeyse "parlaklığı 40 yap" → değişir; harici monitörde dürüst "desteklemiyor".
18. "Bilgisayarı kilitle" → kilit ekranı; yanıt "gönderildi" der (başarı değil).
19. "Bilgisayarı kapat" → onay penceresi; onayla → Windows 60 sn uyarısı; "kapatmayı iptal et"
    → iptal edilir. **Önce açık işleri kaydedin.**
20. "Uyku moduna al" → onay ister; onaylayınca 2–3 sn içinde uyur, uyanınca Jarvis çalışır.

## 7c — Tarayıcı otomasyonu (ADR 0032)

21. Config'e `browser` MCP girdisini ekleyin (`command: "{python}"`) → `mcp_ready` içinde
    `mcp_browser_browse`.
22. "example.com'u aç ve başlığını oku" → başlık ve metin döner, onay penceresi açılır
    (MCP aracı yan etkili sayılır).
23. "http://127.0.0.1:8080 sayfasını aç" ve "file:///C:/Windows/win.ini oku" → reddedilir.
24. Görev Yöneticisi'nde çağrı bitince `chrome`/`chromium` süreci kalmaz.

## Sonuçlar

| # | Sonuç | Gözlem |
| --- | --- | --- |
| 1 |  |  |
