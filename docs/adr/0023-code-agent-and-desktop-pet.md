# ADR 0023 — Kod ajanına iş devri ve masaüstü pet

**Durum:** Kabul. Kullanıcı (2026-10-14) Codex'in masaüstü "pet"ine benzer bir deneyim
istedi: köşede duran, sesle konuşulan, işleri bir kod ajanına yaptıran asistan. Ajan
yetkisi kullanıcı kararıyla **yerel commit**'tir; push yoktur.

**Bağlam:** Bir kod görevi dakikalar sürer, tur ise `max_seconds` (varsayılan 60 sn) ile
sınırlıdır. Ajan, kullanıcının projelerinde dosya düzenler ve commit atar; bu yüzden
yetki sınırı, geri alınabilirlik ve dürüst sonuç bildirimi gerekir. Ajanın çıktısı repo
içeriğinden üretilir, Jarvis için dış veridir.

**Karar — kod ajanı:**

- `code_agent` ayarı: izinli projeler `projects: {takma_ad: mutlak_yol}` listesidir; liste
  boşsa özellik kapalıdır. Model yalnız takma ad seçer, yol veremez.
- `code_agent_start` (CONFIRM_REQUIRED, her seferinde): onayda proje ve görev metni tam
  görünür. Çalışma ağacı kirliyse görev başlatılmaz; böylece ajanın commit'leri kayıtlı
  taban commit'in üstünde ayrışır ve `git reset --hard <taban>` ile geri sarılabilir.
- Görev turdan bağımsız bir işçi thread'inde `claude -p` (headless Claude Code) olarak
  koşar; asyncio döngüsüne bağlı değildir, CLI ve masaüstünde aynı çalışır. Araç
  `status="sent"`, `success=False` döner: görev gönderildi ama sonucu henüz bilinmiyor.
- Alt süreç: `child_environment()` (sağlayıcı/bot sırları aktarılmaz), istem stdin'den,
  `--permission-mode acceptEdits`, `git push`/`git remote`/web araçları yasak, ek sistem
  istemiyle "yalnız yerel commit, push yok". Süre sınırı aşılırsa süreç öldürülür.
- Sonuç doğrulanır: çıkış kodu, JSON `is_error`, taban..HEAD arası gerçek commit listesi ve
  kalan değişiklikler git'ten okunur. Ajanın "yaptım" demesi başarı sayılmaz.
- Başlangıç ve bitiş `code_agent_started` / `code_agent_done` denetim olaylarıdır; masaüstü
  bunları gözlemciden alıp bildirim ve pet balonu gösterir. Ayrı bildirim yolu yoktur.
- `code_agent_status` (READ_ONLY, `external=True`): ajan özeti dış veridir, turu lekeler.
  `code_agent_cancel` (SIDE_EFFECT) süreci sonlandırır; yarım değişiklikler diskte kalır
  ve bu dürüstçe söylenir.

**Karar — pet:** Çerçevesiz, şeffaf, hep üstte, sürüklenebilir küçük bir Qt penceresi;
QPainter ile çizilir, yeni bağımlılık yoktur. Durumları mevcut ses durumu, ses seviyesi,
tur, onay ve kod ajanı olaylarından türetir; kendi komut veya onay yolu yoktur. Tık sesi
açar/kapatır; komutlar ve onaylar mevcut runner ve onay penceresinden geçer.

**Sonuçlar:** Ajan kullanıcı projesinde gerçek değişiklik yapar; geri alma taban commit
üzerinden kullanıcıdadır, otomatik geri alma yoktur. Push yasağı CLI izin kurallarına ve
istem talimatına dayanır; uzak dalın değişmediği ayrıca doğrulanmaz (bilinen sınır).
Bash kısıtsızdır: `cmd /c`, betik veya `curl` gibi dolaylı yollar desen kuralını aşabilir;
repo içeriğindeki bir enjeksiyon kullanıcının yetkisiyle komut çalıştırabilir. Gerçek sınır
izolasyondur (container/VM) ve bilinçli olarak ertelendi; bu yüzden listeye yalnız
güvenilen kendi projeler eklenir. Panel kapanırken çalışan ajan süreçleri öldürülür.
Tamamlanma sesle okunmaz, bildirim ve balonla gösterilir. Gerçek Windows ve gerçek
`claude` kabulü sahte süreç testinden ayrıdır.
