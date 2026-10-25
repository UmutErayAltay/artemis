# Windows birleşik kontrol listesi

Bulut oturumunda yapılamayan (gerçek ses, ekran, uygulama, canlı model) ve şimdiye dek
"Windows'ta denenmedi" diye bırakılan her şey burada toplanır. Bulut oturumu sahte
testlerle geçtiyse bile bu maddeler kabul sayılmaz. Faz 6–7'nin ayrıntılı listesi ve sonuç
tablosu [F6_F7_WINDOWS_ACCEPTANCE.md](F6_F7_WINDOWS_ACCEPTANCE.md)'dedir; burada yalnız
bekleyenleri işaret ediyoruz. Sonucu (geçti / kaldı + gözlem) her maddenin altına yazın.

Kurallar: sır ekrana, dosyaya veya rapora yazılmaz; ücretli model yalnız açık izinle
çağrılır; kapatma/uyku denemesinden önce açık işler kaydedilir; onay pencerelerine
kullanıcı tıklar.

## Hazırlık

```powershell
git checkout main
git pull origin main
python -m pip install -e ".[dev,desktop,browser]"
pytest -q
ruff check .
mypy
python -m bench.run --sahte
```

Ölçüm turları için `config/config.yaml` içinde `voice.telemetry_report: true` açın; ses
kapanınca içeriksiz rapor `state_dir/voice-telemetry/` altına yazılır (ADR 0041).

## A. Konuşma kalitesi planı

### A1. Aşama 1: gecikme ölçümü (ADR 0041)
- [ ] `python -m bench.voice_latency --canli --config config/config.yaml --duration 600 --rapor reports/voice-latency.json`
  ile kullanılan her profil (ör. yerel Ollama, claude_cli) için **en az 30** tur; hem sohbet
  hem araçlı komut, hem ilk (cold) hem sonraki (warm) tur. Raporda `targets.*.met` artık
  `null` değil, gerçek değer olmalı.
- [ ] Raporda `model_first_token_ms` görünüyor mu (streaming açık profillerde)?

### A2. Uyandırmasız söz kesme (ADR 0042)
- [ ] Artemis konuşurken "dur" / "bir saniye" → susar; bu söz komut olarak **çalışmaz**;
  hemen ardından "artemis" demeden söylenen cümle takip komutu olur.
- [ ] Hoparlörle (kulaklıksız) uzun bir yanıt: Artemis kendi sesini söz sanıp kendini
  susturuyor mu? Yankı giderme açık/kapalı ayrı denenmeli.
- [ ] Gürültü (öksürük, masa vuruşu) sesi kesmiyor (ADR 0040).

### A3. Streaming yanıt (ADR 0043)
- [ ] Uzun bir soru: ilk cümle model bitmeden duyuluyor; tur sonunda cümle tekrarlanmıyor.
- [ ] "Not defterini aç" gibi araçlı komut: araç sonucu gelmeden "açtım" denmiyor.
- [ ] Akış sırasında söz kesme: TTS ve model aynı anda duruyor, eski cümle yeni tura sızmıyor.
- [ ] `claude_cli` profili: `stream-json` biçimi gerçek CLI'de belgeyle aynı mı? Denetimde
  `model_first_token` yoksa metin akmıyor demektir (yanıt yine tamamı gelince söylenir).
  Abonelik çağrısıdır; Umut'un açık izniyle.
- [ ] Yedek TTS farklı örnekleme hızıyla gelirse ses bozulmadan sürüyor.

### A4. Uygulama adı ve niyet (ADR 0044)
- [ ] "Brave aç" en az 5 kez. Denetimde (`audit.jsonl`, `tool_requested` → `name`) STT ne
  yazdı? Brave açıldı mı, açıldıysa yanıt "en yakın uygulama Brave" dedi mi?
- [ ] Yanlış anlarsa "hayır, Brave'i kastettim" → `state_dir/app_aliases.json` içinde
  takma ad oluştu mu; Artemis yeniden açılınca aynı söyleyiş doğrudan Brave'i açıyor mu?
- [ ] Gerçek katalogda belirsiz bir ad ("kod", "code") → soruyor mu?
- [ ] "Brave'i açma", "Brave açık mı?", "dün Spotify'da şarkı dinledim" → hiçbir şey açılmıyor.
- [ ] "Breyv'i kapat" (takma ad yokken) → kapatmıyor, "Brave'i mi?" diye soruyor.
- [ ] Eşikler: yanlış uygulama açıldığı **bir** örnek bile varsa not alın (söylenen, açılan,
  katalogdaki rakip); eşik değişikliği bu örneklerle yapılacak.
- [ ] Canlı niyet seti (ücretsiz OpenRouter modeli, `OPENROUTER_API_KEY` ortamda):
  `python -m bench.run --canli --model <model> --rapor reports/intent-live.json` →
  `by_category.intent.wrong_target_actions` ve `dangerous_errors` **0** olmalı.

### A5. STT ipucu ölçümü (ADR 0044)
- [ ] Kendi sesinle uygulama komutları içeren küçük bir kayıt seti
  ([SPEECH_ACCURACY_PLAN.md](SPEECH_ACCURACY_PLAN.md) biçimi) ile A/B:
  `python -m bench.speech_accuracy --canli --corpus <corpus.yaml>` ve aynısı
  `--ek-hotword Brave --ek-hotword Spotify ...` ile. Uygulama adı doğruluğu artıyor, WER
  ve sessizlik uydurması kötüleşmiyor mu?
- [ ] Fayda görülürse `voice.stt_alias_hotwords: true` ile bir gün kullanım: uyandırma
  ("artemis") kaçırma ya da yanlış uyanma artıyor mu? Geçmişte geniş listeler bunu bozmuştu
  ([CPA_LOCAL_TEST.md](CPA_LOCAL_TEST.md)). Takma ad eklendikten sonra ses yeniden açılmalı.

### A6. Aşama 2 STT karşılaştırması (ADR 0045)
- [ ] Corpus'ları kur (depo dışına yazılır):
  `python -m pip install -e ".[voice,eval]"`,
  `python -m bench.corpus_common_voice --ornek 600 --parca 0 --parca 1`,
  `python -m bench.corpus_synthetic --piper <tr_TR-dfki-medium.onnx> --edge tr-TR-EmelNeural --edge tr-TR-AhmetNeural --bulut-tts-izni`.
- [ ] Sabit baseline (bugünkü config), sonra tek değişken: `large-v3-turbo` ↔ `large-v3`,
  sonra beam 5 ↔ 1, sonra `stt_language` tr ↔ tr_en. Her biri:
  `python -m bench.speech_accuracy --canli --corpus %USERPROFILE%\.cache\artemis-eval\common_voice_tr\corpus.yaml`
  ve sentetik corpus için aynısı. `nvidia-smi` ile VRAM'i not et.
- [ ] Raporda `source_breakdown`, `condition_breakdown` ve `totals.wrong_target` /
  `false_wake` değerleri; sentetik sonuç kabul sayılmaz.
- [ ] "artmış" yanlış uyanma bulgusu (ADR 0045): eşleştirici değişsin mi? (karar)

### A7. Gürültüde uyandırma deneyi (ADR 0045) — SIRADAKİ İŞ
Bulutta sentetik ölçüm: temiz seste "Artemis" kaçırma %0, pembe gürültü 5 dB'de %29, telefon
bandında %19. Dört yolu karşılaştır; feature branch gerekir (main'de değil):
`git fetch origin claude/mt3ui55os-readme-gpugv7` → `git checkout claude/mt3ui55os-readme-gpugv7`.
- [ ] Corpus: bulut oturumunun gönderdiği `wake_noise_tr.zip`'i
  `%USERPROFILE%\.cache\artemis-eval\` altına aç (150 örnek: 15 wake + 15 wake-siz temiz
  örnek × pembe 10/5 dB, babble 10/5 dB, telefon). Zip yoksa yeniden kur:
  `python -m bench.corpus_noise --kaynak <sentetik corpus.yaml> --babble <common_voice corpus.yaml>`.
- [ ] `$C = "$env:USERPROFILE\.cache\artemis-eval\wake_noise_tr\corpus.yaml"` ve:
  1. `python -m bench.speech_accuracy --canli --sentetik-metin --corpus $C > wn-base.json`
  2. `... --ek-hotword Artemis ... > wn-hot.json`
  3. `... --gurultu-engelle orta ... > wn-dfn.json` (16 kHz yol; gerçek 48 kHz zincirin alt sınırı)
  4. config'te geçici `stt_vad_filter: false` → `... > wn-novad.json`
- [ ] Karşılaştır: `totals.missed_wake_rate`, `totals.false_wake`, `condition_breakdown`;
  kaçan örneklerin `hypothesis` alanında Whisper "Artemis" yerine ne yazdı? Yakın yazımsa
  eşleştirici, hiç yoksa ipucu/DFN/VAD yolu. Karar ADR 0045'e eklenecek.
- [ ] İsteğe bağlı: 10 sesli sentetik set (XTTS-v2 8 stüdyo sesi + Piper + Emel, 1340 örnek)
  bulutta kuruldu ama ölçümü yarıda kesildi; GPU'da yeniden ölçülebilir (XTTS için torch 2.8 +
  transformers<5 + coqui-tts 0.27.5 ayrı ortam).

## B. Önceki oturumlardan kalanlar

- [ ] Panelde ayar değiştir → "Uygula" → değişiklik gerçekten etkin (2026-10-23, PR #2).
- [ ] Gerçek mikrofonla ses aç/kapa, Ctrl+Space, "Dinliyorum" çıtırtısı ve
  `end_silence` 1000 ms (aed2e92) elle denenmedi.
- [ ] Bilgisayar kullanımı (ADR 0039): panelden zararsız görev (Not Defteri'ne yazı) ve
  Brave'de "anime 1. bölüm" denemesi tekrarı; Esc süren model çağrısını ve turu anında
  kesiyor (475eb13); oto kipte `screen_read` sormuyor (5a23593); kısa yanıt kuralı (ef67845).
- [ ] Faz 6–7 bekleyenler ([tablo](F6_F7_WINDOWS_ACCEPTANCE.md#sonuçlar)): 2 (kapalıyken
  kaçan hatırlatma bir kez), 8 (ekran ölçeği), 11 (oturum kapatıp açınca tepside başlama),
  13 (tepsi menüsünden açılışta başlatmayı kapatma), 18 (kilit "gönderildi"), 19
  (`COMPUTERNAME` düzeltmesi sonrası 60 sn kapatma uyarısı + iptal), 20 (uyku ve uyanınca
  Artemis), 22 (ADR 0036 sonrası tarayıcı okumasında onay penceresi), 24 (çağrı sonrası
  `chrome` süreci kalmıyor).
- [ ] Gürültü engelleme / NVIDIA Broadcast sanal mikrofonu (2026-10-19 thread'i) açık.

## C. Umut'un kararı bekleyenler

- [ ] `open_url` yol/sorgu sızıntısı (ADR 0036): bugünkü kural yalnız dış içerik varken
  `?`/`#` taşıyan adreste sorar; yol bölümü sorulmuyor. Değişsin mi?
- [ ] LinkedIn taslağı (2026-10-23).
