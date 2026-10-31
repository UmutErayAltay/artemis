# ADR 0046: Tanıma hassasiyeti seçimi ve karma (yerel uyandırma + bulut komut) tanıma

- Tarih: 2026-10-26
- Durum: Kabul edildi (kullanıcı: panelde int8_float16 / float16 / bulut seçimi; bulutta
  uyandırma yerelde kalsın)

**Bağlam:** ADR 0045 ölçümleri (Windows, RTX 4050 6 GB): large-v3 `int8_float16` sentetik
294 örnekte WER %18,3 / hedef %89,0 / 569 ms, `float16` %18,6 / %90,2 / 640 ms; Whisper
VRAM ~2,5 GB'a karşı ~4,3 GB. Doğruluk aynı, int8 ~1,8 GB az bellek. Turbo gerçek seste
kötü (Common Voice WER %13,6 → %19,6), ama katalog hâlâ turboyu "Önerilen" diyor.
Kullanıcı GPU'yu boşaltmak için isteğe bağlı bulut tanıma istiyor. Uyandırma STT metninden
algılanır (`VoiceController._wake_command`); tam bulutta odadaki her söz (TV, sohbet)
buluta giderdi. Uyandırma ölçümü (wake_noise 150, CPU int8): small kaçırma %2,7, yanlış
uyanma 0.

**Karar:**

1. **Hassasiyet** `voice.stt_compute_type` panelde seçilir: `int8_float16` ("Dengeli, az
   bellek") ve `float16` ("En yüksek hassasiyet"). CPU cihazında seçici kapalıdır
   (faster-whisper CPU'da int8 kullanır). Varsayılan değer kodda değişmez (`float16`);
   kullanıcı panelden seçer.
2. **Katalog** ADR 0045'e uyar: turbo notu gerçek seste daha zayıf olduğunu söyler,
   "Önerilen" large-v3'e geçer; `recommended()` ≥ 3000 MB VRAM'de `large-v3` döner (int8 ile
   ~2,5 GB), altında `small`.
3. **Karma mod** `voice.stt_mode: "local" | "hybrid"` (varsayılan `local`). Hybrid'de:
   - Yerel tanıyıcı (kullanıcının seçtiği model, önerilen small) her sözü çözer; yankı,
     söz kesme, uyandırma ve takip kararları yerel metinle verilir.
   - Söz **komut olacaksa** (yerel metinde uyandırma + komut kuyruğu var, takip penceresi
     açık, ya da kesintisiz kip) aynı PCM bulut tanıyıcıya gönderilir; komut bulut
     metninden çıkarılır (uyandırma sözcüğü bulunursa ondan sonrası, yoksa tam metin).
   - Yalnız "Artemis" (boş kuyruk), elenen söz ve kısmi (partial) çözümler buluta
     **gitmez**.
   - Bulut hatası/zaman aşımı (8 sn) → yerel metne düşülür, olay günlüğüne
     `stt_cloud_fallback` yazılır; ses komutu kaybolmaz.
   - Kesintisiz kipte her nihai söz komut olduğu için her söz buluta gider; panel bunu
     karma seçiliyken açıkça yazar.
4. **Sağlayıcı** OpenAI uyumlu tek adaptör: `POST {base_url}/audio/transcriptions`
   (multipart WAV 16 kHz mono, `model`, `language` = `stt_language` `tr` ise `tr`,
   `response_format=json`, hotword'ler `prompt` alanına). Uç `voice.stt_cloud_endpoint`
   ile mevcut `endpoints` kaydından seçilir (ADR 0033: anahtar `stored_endpoint_key`,
   config'e yazılmaz); model `voice.stt_cloud_model` (koda gömülmez; örnek config:
   `groq` + `whisper-large-v3`). Yönlendirme takip edilmez; yanıt gövdesi ve anahtar
   loga/hata mesajına girmez.
5. **Gizlilik** (aşağıdaki Ek madde 2 ile kaldırıldı; `stt_mode: hybrid` seçimi izindir):
   `voice.allow_cloud_stt_audio: false` varsayılan. Hybrid seçili ama izin
   kapalıysa ses kanalı başlamaz, açık hata verir (Edge TTS `allow_cloud_tts_text` deseni).
   Panelde karma seçimi izin onay kutusunu da açar ve "komut sesin {uç} sunucusuna gider"
   uyarısını gösterir. Ham ses diske yazılmaz.

**Sonuçlar:** Varsayılan davranış değişmez (yerel, float16). Karma modda GPU'da yalnız
small kalır (~1 GB); komut doğruluğu sağlayıcıya bağlıdır ve gerçek seste ölçülmedi
(kendi ses seti işi ertelendi). Groq ücretsiz kotası (20 istek/dk, 2000/gün) doğrulanmadı;
aşılırsa yerel düşüş devreye girer. Oyun modu ve uygulama adı hotword'ü ayrı işler.

## Ek (2026-10-26, canlı kullanım)

1. **Karma hata düzeltmesi:** Yerel Whisper tam `stt_hotwords` listesiyle "Artemis" tek başına denince "Artemis tarayıcı" uydurdu; kuyruk komut sanıldı, buluta gitti, bulut da aynı metni (veya yalnız uyandırmayı) döndürünce yerel metne düşülüp komut çalıştı. Artık hybrid'de yerel tanıyıcı yalnız uyandırma sözcüğünü ipucu alır, bulut isteğinde `prompt` yoktur (Whisper gürültüde prompt'u yankılıyor) ve başarılı bulut yanıtı nihaidir: boş ya da yalnız uyandırma ise tek başına uyandırma sayılır. Yerel metne yalnız istisnada düşülür (`stt_cloud_fallback` yalnız orada).
2. **Tek onay:** `allow_cloud_stt_audio` kaldırıldı; `stt_mode: hybrid` seçmek açık izindir (mesajlar zaten bulut LLM'e gidiyor). Panelde tek kutu ve görünür uyarı kalır; `cloud_stt_transfer` denetimi sürer. Eski config'teki anahtar yutulur, diğer bilinmeyen alanlar yine reddedilir.
3. **`voice.followup_listening` (varsayılan false):** Söz kesme yalnız sesi durdurur, yanıt sonu ve susturma sonrası Artemis demeden dinleme açılmaz. Tek başına "Artemis" karşılığı takibi her zaman açar; kesintisiz kip değişmez. true eski davranıştır. Panelde `barge_in_without_wake` ile birlikte iki kutu.
4. **`voice.wake_ack_text` (varsayılan "Seni dinliyorum."):** "Dinliyorum." EMA TTS'te çok kısa olduğundan bozuk çıkıyordu. Karşılık metne göre bir kez sentezlenir, sonraki uyandırmalarda aynı PCM çalınır (yarım kesilen akış önbelleğe girmez).
