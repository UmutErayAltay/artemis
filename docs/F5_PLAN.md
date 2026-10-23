# Faz 5 geliştirme sözleşmesi

2026-10-14. Astra planı, Sol çekirdek uygulaması, Luna mekanik test/belgeler.
Karar: [ADR 0026](adr/0026-background-project-workflow.md).

## Akış

Ses veya yazı → mevcut CommandRunner → netleştirme → proje taslağı → SafetyGate
onayı → Claude Code yönetici → rol profiline göre alt ajanlar → sonuç veya soru.
Soru → kullanıcı cevabı + onay → aynı Claude oturumuna devam. Kullanıcı diğer
Artemis konuşmalarını sürdürebilir; kod işi konuşma turunun süre sınırından bağımsızdır.

## Kabul ölçütleri

- Taslak süreç başlatmaz; eksik kapsam sorulur. Onay tam taslağı gösterir; hayırda
  süreç doğmaz. Aynı taslağın iki kere başlatılması reddedilir.
- Projeler sekmesi gerçek durumları ve tam görev/ölçütleri gösterir. UI düğmeleri
  de kayıtlı araç ve gate üzerinden çalışır; durum sorgusu modeli gerektirmez.
- Question segmenti başarı sayılmaz. Yanlış/eski soru kimliği ve session UUID,
  yinelenen cevap, süre sonrası cevap, iptal sonrası cevap reddedilir.
- Süre toplamdır. kill/communicate/join sınırlıdır; alt süreçler sahipsiz kalmaz.
  Sınırda temizlik başarısızsa `cleanup_pending`/dürüst hata vardır, başarı yoktur.
- Exit!=0, is_error=true, bozuk/eksik JSON/schema veya git doğrulama hatasında done
  yoktur. Kalan dosyalar ve gerçek commit listesi raporlanır, otomatik reset yapılmaz.
- SQLite durumları restart'ta korunur; çalışıyor/bekliyor işler kesinti olarak
  işaretlenir. Aynı fiziksel repo iki takma adla kilitlenemez.
- Rol/model ayarları Claude CLI argv'sine doğrulanmış profil olarak gider; prompt
  stdin'dedir. Sırlar argv, audit, repr ve alt süreç env'ine girmez.
- Testler tmp_path git reposu, senaryolu LLM ve sahte CLI/loopback kullanır;
  gerçek kullanıcı dosyası, ücretli model ve canlı abonelik çağrısı yoktur.
- pytest, ruff, mypy, sahte bench ve Ubuntu/Windows CI yeşildir. README ve ROADMAP
  kodlandı/otomatik doğrulandı/Windows kabulü ayrımını açıkça tutar.

## Bilinçli sınırlar

Yerel kod ajanı OS sandbox değildir; kullanıcının ADR 0023 yetki kararı korunur.
Gerçek Windows + Claude kabulü olmadan Faz 5 tamamlandı olarak etiketlenmez.
Sesin STT/TTS ayarları, hafıza, Telegram ve abonelik proxy kurulumu kapsam dışıdır.
