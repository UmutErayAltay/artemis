# ADR 0010: Karışık dilde konuşmayı doğrulukla ölçmek

## Bağlam

Kullanıcı günlük Türkçe konuşmada İngilizce uygulama adlarını ve ifadeleri birlikte
kullanıyor. Tek ışınlı çözümleme hız için seçilmişti; gerçek ses doğruluğu henüz
ölçülmedi. Metin tabanlı sahte testler akustik doğruluğu kanıtlamaz.

## Karar

Kesin STT için yapılandırılabilir beam size (başlangıç 5), kısmi sonuç için 1
kullanılır. Dil Türkçe veya otomatik olabilir; Türkçe ağırlıklı konuşma varsayılan
kalır. Otomatik dil seçimi kelime bazında dil değişimini garanti etmez.
Sınırlı, yapılandırılmış özel kelime listesi yalnız yerel STT'ye ipucu olur;
metne sonradan yaklaşık eşleştirme ile uygulama adı yazılmaz. Uygulama izin listesi
ve tek SafetyGate değişmez. Daha büyük model seçimi gerçek kayıtlarla karşılaştırılır.

Kullanıcının önceden hazırladığı yerel WAV ve referans metinlerle çalışan ayrı
benchmark WER, CER, uygulama adı doğruluğu ve yanlış uygulama adı üretimini raporlar.
Ses buluta gönderilmez; benchmark araç çalıştırmaz. Sessizlik örnekleri ayrıca
ölçülür. Sahte kip yalnız ölçüm kodunu sınar; gerçek tanıma başarısı sayılmaz.

## Sonuçlar

Kesin çözümleme daha uzun sürebilir. İpuçları yanlış pozitif de üretebilir;
kelime listeli/listsiz aynı kayıtlarla karşılaştırılır. Model ve eşikler henüz
Windows/RTX 4050 üzerinde doğrulanmış değildir. Ses tanıma skoru, niyet veya
araç hedefi doğruluğunun yerine geçmez; belirsizlikte soru sorma davranışı ayrıca
ajan kabul senaryolarıyla değerlendirilir.
