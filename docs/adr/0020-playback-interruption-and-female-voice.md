# ADR 0020 — Konuşurken söz kesme ve kadın sesi

**Bağlam:** Kullanıcı STT'nin daha iyi anladığını, ancak asistanın yanıtını
bitirene kadar konuştuğunu ve erkek sesinin robotik geldiğini bildirdi.
Sekme kapatma bu isteğin kapsamı değil. Emel örneğini aldıktan sonra yanıt
metninin Microsoft'a gönderilmesini bilerek Emel'i etkinleştirmeyi seçti.

**Karar:** Seslendirme ayrı iptal edilebilir görevdir. Seslendirme sırasında
varsayılan 180 ms kesintisiz VAD konuşma algısı yalnız oynatmayı durdurur.
Kısa tık sesi eşiğe ulaşmaz. Model/araç turu VAD ile iptal edilmez; kesin komut
tanıması ADR 0019'daki kurala tabidir. Söz kesme sonrası takip penceresi açılır;
kullanıcının yeni cümlesini bitirmesi beklenir, sonra STT sonucu işlenir.
Ses aygıtı üretici/ffmpeg temizliği beklenmeden abort edilir.

Yerel profil `tr-TR-EmelNeural` ve edge-tts 7.3.0 kullanır. Önceki 7.2.8
canlı serviste 403 verdi, yeni sürüm ses üretti. Mikrofon STT'si yereldir;
yalnız asistan yanıtı metni uzaktaki TTS'ye gider, aktarım audit'e metinsiz yazılır.
Yerel erkek sese sessiz geri dönüş istenmediğinden bu profilde
`tts_local_fallback: false`; servisin kesilmesi sesli yanıtı engelleyebilir,
yazılı yanıt korunur. Varsayılan profilde eski fallback davranışı korunur.

**Sınırlar:** VAD konuşmacı kimliği veya yankı giderme değildir. Hoparlör
sesinin mikrofona dönmesi ya da uzun arka plan konuşması oynatmayı kesebilir;
kulaklıkla gerçek kullanıcı kabulü hâlâ gereklidir. 180 ms algı eşiğidir,
uçtan uca garanti edilmiş gecikme değildir. Gönderilmiş araç etkileri geri alınmaz.
