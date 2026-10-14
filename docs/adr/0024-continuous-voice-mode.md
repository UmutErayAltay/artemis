# ADR 0024 — Kesintisiz sohbet modu

**Bağlam:** Her komuttan önce “artemis” demek akıcı sohbeti bozuyor. Ama mikrofon
asistanın kendi sesini de duyar (ADR 0022); uyandırma sözcüğü bu yüzden ilk savunmadır.

**Karar:** `voice.continuous_mode` (varsayılan kapalı) ve çalışma anında değişen
`VoiceController.continuous` eklendi. Açıkken uyandırma sözcüksüz nihai söz komut
sayılır; “artemis” hâlâ çalışır, tek başına “Dinliyorum.” yanıtı verir.

- **Çıktı sırasında wake word yine şart.** Kesintisiz modda bile asistan konuşurken
  söz kesmek “artemis” ister. Aksi halde hoparlörden mikrofona sızan TTS (yankı
  korumasının kaçırdığı bulanık parçalar) komuta dönüşür ve asistan kendi kendine
  iş yapar. Yankı koruması ve `require_wake_during_output` değişmedi.
- **Kısmi STT hâlâ komut çalıştırmaz.** Onaylar hâlâ ekrandaki pencerededir;
  SafetyGate'e dokunulmadı.
- **Kip sesli de değişir, ama yerelde.** “Kesintisiz moda geç / modu aç / sürekli
  dinle” ve “kesintisiz modu kapat / moddan çık / normal moda geç” LLM'e gitmez:
  modelin güvenlik kipini yorumlayıp yanlış anlaması ya da dış içerikle
  (lekelenmiş tur) tetiklenmesi istenmez, ayrıca gecikme ve maliyet yoktur. Eşleşme
  normalize sözcük dizisine tamdır; uzun cümle içindeki ifade normal komuttur.
  Kısa sesli onay (“Kesintisiz mod açık/kapalı.”) “Dinliyorum.” yoluyla verilir.
- **UI:** ayar paneli onay kutusu `voice.continuous_mode` değerini Uygula ile
  kaydeder; ses açıksa köprüye eklenen sabit `set_continuous` eylemi kipi hemen
  uygular. Kip ses durumu etiketinde “Kesintisiz” olarak görünür. Sesli kip
  değişikliği yalnız oturumluktur, config'e yazılmaz.

**Sınırlar:** Wake word'süz modda ortam konuşması (TV, yanındaki kişi) komut olabilir;
bu yüzden varsayılan kapalıdır ve kulaklık önerilir. Kip cümleleri sabit bir listedir.
