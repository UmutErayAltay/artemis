# ADR 0042: Konuşurken uyandırmasız söz susturur, komut çalıştırmaz

- Tarih: 2026-10-25
- Durum: Kabul edildi

**Bağlam:** ADR 0040 söz kesmeyi nihai STT'ye bağladı ve yanıt sırasında açık
“artemis” istedi (`require_wake_during_output`). Kullanıcı, Artemis konuşurken
yalnız konuşmaya başlamasının (“dur”, “bir saniye”) yeterli olmasını istedi.

**Karar:** Hoparlöre yazım sürerken (`_output_active`) gelen, uyandırma sözcüğü
taşımayan söz şu koşulların hepsini geçerse oynatmayı ve turu durdurur:
nihai STT boş değil (adaptörün hayalet/no_speech süzgeci dahil), yankı süzgecinden
geçti ve söz en az `barge_in_ms` (varsayılan 180 ms) sürdü. Bu söz **komut olarak
çalıştırılmaz**; yalnız susturur ve takip penceresini (`followup_seconds`) açar.
Sonraki söz uyandırmasız komut olarak alınır. Uyandırma sözcüğü taşıyan söz
ADR 0040'taki gibi doğrudan kesip komut olur. Ayar `voice.barge_in_without_wake`
(varsayılan açık); kapalıyken ADR 0040 davranışı aynen sürer. Telemetride sonuç
`interrupted`, neden `barge_in`'dir; stop çifti kesen sözün kimliğindedir.

**Neden komut değil:** Yankı süzgecini aşan kendi sesimiz ya da TV/oda konuşması
uyandırmasız komut olsaydı Artemis kendi sözünü veya yabancı sesi araç çağrısına
çevirebilirdi. Yanlış susturma yalnız yanıtı keser; geri alınamaz bir yan etki
üretmez. Düşünme (model/araç) sırasında uyandırmasız söz turu iptal etmez: o anda
hoparlör sesi yoktur ve iptal yan etkili bir aracı yarıda bırakabilir.

**Sınırlar:** Yankı giderici/süzgeç kendi sesimizi kaçırırsa Artemis kendini
susturabilir; oda konuşması da susturur. Söz kesme hâlâ cümle sonu sessizliğini
ve nihai STT'yi bekler (ADR 0040), anlık değildir. Gerçek Windows mikrofon/hoparlör
kabulü yapılmadı.

**Doğrulama:** `tests/test_voice_barge_in.py`: uyandırmasız söz susturur ve
çalıştırılmaz, ardından takip sözü komut olur; kısa patlama, boş STT, kapalı ayar,
`require_wake_during_output: false` ve kendi yankımız susturmaz; düşünme sırasında
söz turu iptal etmez. ADR 0040 testleri `barge_in_without_wake: false` ile korunur.
