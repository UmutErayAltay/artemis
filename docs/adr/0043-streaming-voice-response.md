# ADR 0043: Sesli yanıtta streaming

- Tarih: 2026-10-25
- Durum: Kabul edildi (2026-10-25, kullanıcı: araç öncesi metin söylenir, başarı iddiası süzülür; sağlayıcılar sırayla)

**Bağlam:** Bugün ses, `run_command` bütün turu (tüm model adımları + araçlar) bitirince
başlar; sağlayıcılar `stream: False` çağrılır. Aşama 1 ölçümü speech_end → ilk PCM'i
model süresinin tamamına bağlı gösterir. Plan, streaming için ayrı ADR ister ve şunları
koşul koyar: kısmi argümanla araç çalışmaz, SafetyGate onayı değişmez, araç sonucu
doğrulanmadan "yaptım" denmez, söz kesmede tek iptal akışı, sınırlı oynatma kuyruğu,
eski tur PCM'i yeni tura sızmaz, streaming'siz sağlayıcı için raporlanan tam yanıt yedeği.

**Karar önerisi:**

1. **Sağlayıcı yeteneği isteğe bağlıdır.** `Provider.complete(..., on_text=None)`:
   `on_text` verilirse ve sağlayıcı destekliyorsa yalnız *kullanıcıya metin* parçaları
   (content delta) callback'e akar; dönüş değeri bugünkü gibi tam ve doğrulanmış
   `Completion`'dır. Araç çağrısı parçaları callback'e hiç gitmez, yalnız sonda bütün
   olarak ayrıştırılır; araç yürütme, şema doğrulaması ve onay bugünkü yerinde kalır.
   Desteklemeyen sağlayıcı callback'i çağırmaz; tur `streamed: false` olarak raporlanır.
2. **Router:** ilk metin parçası dışarı çıktıktan sonra yedek modele geçilmez (iki farklı
   yanıt seslendirilirdi); o noktadan sonraki hata turu sağlayıcı hatasıyla bitirir.
   Bütçe rezervasyonu, denetim (`provider_call`, `cloud_transfer`) ve gizlilik maskesi
   aynı `_call` yolundan geçer.
3. **AgentLoop** `on_text`'i her model adımına iletir ve adım sınırını bildirir
   (`step_start`/`step_end(has_tool_calls)`); ilk parça `model_first_token` aşaması
   olarak `RunResult.phases`'e yazılır. Geçmiş ve denetim yine tam yanıtla yazılır.
4. **Araç öncesi metin:** bir adımın akan metni, o adımda araç çağrısı parçası görülene
   dek cümle sınırında seslendirilir. Geçmiş zamanlı başarı ifadesi taşıyan cümle
   ("kapattım", "gönderdim", "tamamlandı", "silindi" vb.) adım bitene dek bekletilir;
   adım araç çağrısıyla biterse atılır (eylem henüz yapılmadı). Araç parçası görüldükten
   sonra o adımın metni seslendirilmez. Sistem istemine "araç sonucunu görmeden
   tamamlandı deme" kuralı eklenir; süzgeç bunun yedeğidir.
5. **Ses hattı:** `run_command(text, on_text=...)`; controller parçaları cümleye böler,
   sınırlı (`maxsize`) bir cümle kuyruğundan TTS'ye verir, tek `sink.play` akışında
   birleştirir. Tur sonunda `result.text` akan metinle başlıyorsa yalnız kalanı, aksi
   halde (zaman aşımı, iptal, adım sınırı mesajı) `result.text`'i söyler. Söz kesme
   bugünkü `generation` olayını kullanır: runner iptali sağlayıcı akışını keser, kuyruk
   boşaltılır, TTS ve sink aynı olayla durur; kuyrukta kalan cümleler yeni tura geçmez.
6. **Sağlayıcı sırası:** önce Ollama (NDJSON) ve OpenAI uyumlu/OpenRouter (SSE);
   `claude_cli` için `--output-format stream-json` + yapılandırılmış çıktının `content`
   alanını artımlı çıkarmak ayrı adım; Anthropic SSE sonra. Hiçbiri yokken davranış
   bugünküyle aynıdır.

**Sınırlar:** Seslendirilmiş metin geri alınamaz; süzgeç Türkçe kalıp tabanlıdır, her
başarı iddiasını yakalamaz (sistem istemi kuralı + süzgeç birlikte). Cümle cümle TTS
cümle başına sentez başlatma maliyeti ekler. `claude_cli`'de asıl gecikme süreç
başlatmadır; streaming bunu azaltmaz. Gerçek kazanç ancak Windows canlı ölçümüyle
(aşama 1 raporu) gösterilir.

**Doğrulama planı:** sahte streaming sağlayıcıyla: araç parçası metne sızmaz, kısmi
argümanla araç çalışmaz, başarı iddiası araçlı adımda seslendirilmez, ilk parçadan sonra
yedeğe geçilmez, söz kesmede sağlayıcı/TTS/sink tek iptalle durur ve eski cümle yeni
tura geçmez, streaming'siz sağlayıcıda davranış değişmez. Her güvenlik kuralı için
mutasyon testi.

## Uygulama (2026-10-25, ilk adım)

- `artemis/brain/streaming.py` sözleşmesi; `Provider.complete(..., stream=)` yalnız akışta
  geçilir, akışsız çağrının sözleşmesi değişmedi. Ollama (NDJSON) ve OpenRouter/OpenAI
  uyumlu (SSE) akar; parçalar mevcut ayrıştırıcının beklediği tam yanıta toplanır, araç
  adı/argüman doğrulaması ve maliyet tek yerde kalır. Akış bitmeden kopan bağlantı başarı
  sayılmaz (`RetryableProviderError`). OpenAI uyumlu uca `stream_options.include_usage`
  gider (standart alan; desteklemeyen uç 4xx döner, akışsız yazılı kanal etkilenmez).
- Router ilk metin parçasından sonra yedeğe geçmez (`provider_fallback_skipped`).
- Süzgeç her yan cümlenin yalnız son sözcüğüne bakar (Türkçe yüklem sonda): "anlatım",
  "yardım" gibi isimler iddia sayılmaz; "kapattım, ..." ve "... ve açtım" sayılır.
- Söz kesme (ADR 0042) akışlı turda yalnız ilk PCM'den sonra devrededir; düşünme sırasında
  uyandırmasız söz turu kesmez. Yedek TTS farklı örnekleme hızıyla gelirse sink akışı
  yeniden açar.
- `voice.streaming` (varsayılan açık) factory üzerinden; `VoiceController` varsayılanı
  kapalıdır (kanal `stream=` kabul etmiyorsa eski yol).

Doğrulama (Linux, donanımsız): 1686 test; loopback SSE/NDJSON sahte sunucusu; gerçek
AgentLoop + sahte sağlayıcıyla ilk cümlenin model bitmeden çalındığı, araçlı adımdaki
iddianın hiç sentezlenmediği, söz kesmede sağlayıcı/TTS/sink'in tek iptalle durup eski
cümlenin yeni tura geçmediği sınandı. Mutasyon: süzgeç, araç sonrası susma, yedeğe geçiş,
argüman sızıntısı, kesik akışın başarı sayılması, akan metnin tekrarı ve düşünürken söz
kesme testleri kırdı; iki mutasyon eşdeğer çıktı (bekleyen iddia adım başında zaten
temizlenir; tur başına ayrı akış kuyruğu yeni tura geçemez). Kalan: `claude_cli`,
Anthropic SSE, Windows canlı ölçüm.

## Uygulama (2026-10-25, ikinci adım): Anthropic ve `claude_cli`

- **Anthropic** Messages SSE (`message_start` … `message_stop`) tam yanıt gövdesine
  toplanır; `tool_use` girdisi (`input_json_delta`) akışa girmez, `message_stop` gelmeden
  biten akış başarı sayılmaz, `overloaded_error` yeniden denenebilir hatadır.
- **`claude_cli`**: `--output-format stream-json --verbose --include-partial-messages`.
  Agent SDK belgesine göre `--json-schema` yanıtı CLI'nin eklediği `StructuredOutput`
  aracının girdisidir. `artemis/brain/claude_stream.py` bu girdinin yalnız **üst düzey**
  `content` dizgesini artımlı çözer (derinlik + dizge durumu; kaçış ve vekil çift parça
  sınırında bölünebilir, geçersiz kaçış akışı keser). Üst düzey `tool_calls` dizisine öğe
  yazılınca "araç başladı" denir; iç içe `content` (ör. bir aracın argümanı) ve dizge
  olmayan `content` asla akmaz. İkinci `StructuredOutput` çağrısı (şema yeniden denemesi
  veya yedek model geri alması) akıtılmaz; tur sonunda doğrulanmış yanıt söylenir. Tam
  yanıt yine son `result` satırının `structured_output`'undan bugünkü ayrıştırıcıyla
  doğrulanır; `result` yoksa başarısızlıktır. Akışta stderr atılır (boru dolup süreci
  tıkamasın), satır sınırı 16 MiB'tir.
- `finish` karşılaştırması boşluk normalize edilerek yapılır: blokların satır sonuyla
  birleşmesi akan yanıtı ikinci kez söyletmez.

**Doğrulanmayan:** `claude_cli` akış biçimi belgeye ve sahte süreçle sınandı; gerçek CLI
çıktısı bu ortamda çalıştırılmadı (abonelik çağrısı). Biçim farklıysa metin akmaz ve yanıt
tamamı gelince söylenir (bugünkü davranış). Windows'ta canlı deneme gerekir.
Doğrulama: 1700 test; mutasyon: iç içe/dizge olmayan `content`, ikinci deneme, araç işareti,
`result` yokluğu, Anthropic girdi sızıntısı ve kesik akış testleri kırdı.
