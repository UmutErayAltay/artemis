# ADR 0056 — Gizlilik kökeni ve onayın hedef pencereye bağlanması

**Bağlam:** 2026-11-07 incelemesi (commit `cb5389b`) yan etkisiz, sahte sağlayıcı/arka uçla
yeniden üretilen beş bulgu bildirdi; beşi de güncel kodda doğrulandı. İkisi aynı kökten
geliyordu: gizlilik bilgisi ya bir rol bayrağına (`local_fallback`) ya da lekeye (`tainted`)
bağlanmıştı ve onlardan ayrılınca kayboluyordu. Diğerleri bilgisayar kullanımında denetimin
yanlış anda yapılmasıydı: onay pencereye bağlı değildi, yazım sırasında iptal/süre bakılmıyordu.

İlke iki kuraldır: **yerel hafızaya yazma onayı buluta paylaşım izni değildir** ve
**onay, kullanıcının gördüğü hedefe verilir; hedef değişirse onay geçmez.**

## Karar

### Gizlilik kökeni lekeden ve rolden ayrıdır

1. **Yerel model:** `Completion.local_model`, yanıtı üreten modelin girdiyi maskesiz gördüğünü
   bildirir (router her yolda `provider == "ollama"`'dan, `OllamaProvider` kendisi doldurur).
   Ana, yedek ya da zor işler modeli olması fark etmez. AgentLoop gizli geçmiş varken bu bayrağı
   görünce turun yeni mesajlarını, araç argümanlarını ve kalıcı yanıtı (`answer_private`) gizli
   sayar. `local_fallback` yalnız yerel yedeğin tek-araç sınırıdır; gizlilik için açılmaz.
2. **Kalıcı hafıza:** `hafiza/degisiklikler.sqlite3` ve `memory.sqlite3::notes` tablolarına
   `private` sütunu eklendi. Yazan araç turun `turn_private` durumunu saklar
   (`memory_session_update`, `memory_rule_add`, `memory_job_receipt`, `memory_remember`,
   geri alma). Gizlilik leke gibi **yapışkandır**: yazımların çoğu eski bölümleri korur, temiz
   bir yazım dosyada kalan gizli metni açık hâle getirmez. Kural kaldırma metin eklemediği için
   gizlilik eklemez (yapışkanlık yine geçerlidir). Geri alma, geri yüklenen metnin kökenini
   (leke ve gizlilik) geri getirir. Aynı not gizli turda yeniden yazılırsa gizli olur, tersi olmaz.
3. **Taşıma:** gizli companion/not bölümleri genel sistem isteminden çıkar, ayrı bir
   `private=True` sistem mesajıyla gider. Bulut sağlayıcısı onu HTTP sınırında maskeler
   (`_cloud_messages`), yerel model tam görür. Bu mesaj "gizli geçmiş" sayılır: yerel model onu
   yanıtına kopyalarsa 1. madde devreye girer. Genel sistem istemi değişmediği için önbellek
   (ADR 0055) korunur.
4. **`ToolResult.private`:** içerik türünden (`content_kind`) bağımsız gizlilik. AgentLoop'un
   bulut kararı `private or content_kind kuralı`dır. `memory_list`, `memory_open_work` ve
   `memory_changes` kullanır.
5. **Değişiklik geçmişi:** `list_changes` her kaydın `after` metninin yanında `before` metninin
   kökenini de verir (aynı dosyanın bir önceki tamamlanmış yazımı; yoksa iskelet ya da
   kullanıcının elle yazdığı metin, temiz). `memory_changes` döndürdüğü herhangi bir anlık
   görüntü lekeliyse `external=True` (kapı turu lekeler, sonraki yan etkiler onay ister),
   gizliyse `private=True` döner. Güncel dosyanın temiz olması arşivdeki metni aklamaz.

**Geçiş:** eski companion kayıtlarında gizlilik bilgisi yoktu. Gizli içerik (izinsiz dosya,
pano, ekran) yalnız dış içerik okuyan araçlardan gelir ve o turlar `tainted` yazılırdı; bu
yüzden lekeli eski kayıt gizli sayılır (`private = tainted`; fazlası güvenli taraftadır).
Notlar lekeyi de saklamıyordu; eski notların kökeni çıkarılamaz ve **gizli değil** sayılır.
Bu bilinen bir sınırdır: şüpheli eski notu `memory_forget` ile silin. Sütun yoksa ilk
bağlantıda eklenir; eşzamanlı geçiş hatası yutulmaz, yalnız sütun zaten varsa geçilir.

### Bilgisayar kullanımında onay hedefe, son denetim gönderim anına bağlıdır

6. **Pencere kimliği:** her eylemin hedefi pencere kimliğiyle (hwnd) çözülür. Yazma/tuş ön
   plan penceresi, tıklama/kaydırma noktadaki üst düzey pencere (`window_at`, yeni arka uç
   yüzeyi). Hassas adımın onayı bu kimliğe bağlıdır.
7. **Onay penceresinin geçici odağı:** onaydan sonra Artemis'in kendi penceresi önde kaldıkça en
   çok 1,5 sn (`_REFOCUS_SECONDS`) beklenir. Onaylanan pencere geri gelirse adım çalışır; başka
   bir pencere öne geçtiyse ya da süre dolduysa adım `ATLANDI` notuyla yeniden planlanır ve
   sonraki deneme yine onaya gelir. Bütün pencere denetimi kaldırılmaz; yalnız kendi
   penceremizin geçici odağı tanınır.
8. **Gönderimden hemen önce:** iptal (Esc), mutlak süre ve hedef pencere kimliği, girdiyi
   gönderen thread'de bir kez daha denetlenir (executor kuyruğunda beklenmiş olabilir). Bu
   onaysız adımlar ve otomatik kip için de geçerlidir.
9. **Yazım:** `type_text(text, should_continue)` her kod noktasından önce aynı denetimi yineler
   ve gönderilen kod noktası sayısını döner. Bir kod noktasının bütün UTF-16 birimleri (vekil
   çifti) basma/bırakma olaylarıyla tek `SendInput`'tadır: yarım karakter ya da basılı tuş kalmaz.
   Yarım kalan yazım başarı sayılmaz: oturum `stopped`/`timeout`/`blocked` ile ve "gönderilen
   kısım geri alınmadı" notuyla biter; `computer_input_partial` denetime yazılır (sayılar, metin
   değil). Gönderilmiş girdinin geri alındığı söylenmez.

## Sonuçlar

- Onay politikası hâlâ yalnız `SafetyGate`'te ve bilgisayar oturumunun tek onay noktasındadır;
  araçlara yeni onay mantığı eklenmedi. Zorunlu onay, taint, gözetimsiz tur ve otomatik kip
  (ADR 0039) kuralları değişmedi; otomatik kip hedef ve gönderim denetimlerini atlamaz.
- Gizli hafıza bulut modeline maskeli gider: bulut modeli o bilgiyi kullanamaz. Bu bilinçli
  bir bedeldir; gizlilik yapışkan olduğu için temizlemenin yolu geri almadır.
- Yazımda her karakterden önce bir `GetForegroundWindow` çağrısı yapılır; hız 30 ms/karakter
  ile değişmedi.

## Sınırlar

- Gerçek Windows kabulü yapılmadı: onay penceresi kapandıktan sonra odağın gerçekte nereye
  döndüğü, 1,5 sn beklemenin yetip yetmediği ve yazım sırasında Esc'nin kalan metni kestiği
  `docs/WINDOWS_CHECKLIST.md` F bölümünde bekliyor. Sahte testler bunun yerine geçmez.
- Odak kontrolü ile `SendInput` arasında çok kısa bir yarış penceresi kalır (işletim sistemi
  atomik "şu pencereye gönder" sunmaz); karakter başına kontrol bunu bir karaktere indirir.
- Tıklama onayı pencere kimliğine bağlıdır; aynı pencere onay sırasında taşınırsa nokta artık
  farklı bir öğeye denk gelebilir (önceki davranışla aynı).
- Eski notların gizlilik kökeni bilinmez (geçiş notuna bakın).
