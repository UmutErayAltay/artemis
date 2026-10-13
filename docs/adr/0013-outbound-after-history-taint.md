# ADR 0013 — Geçmişte dış içerik varken dışarı giden araçlar onay ister

**Bağlam:** ADR 0012'de taint yeni tura taşınmıyor. Önceki turda okunan dosya veya
web sayfasındaki bir injection metni bağlamda veri olarak kalıyor; model çerçeveyi
yok sayarsa yeni turda onaysız `open_url` veya `web_search` çağırıp içeriği URL ya
da sorgu üzerinden dışarı taşıyabilir. Kullanıcı 2026-10-13'te kuralın yalnız bu iki
araç için uygulanmasına karar verdi; diğer yan etkiler (dosya oluşturma/taşıma)
yeni turda onaysız kalır.

**Karar:** Araç, argümanını makinenin dışına (ağ, tarayıcı) taşıyorsa
`outbound = True` bildirir; bugün `open_url` ve `web_search`. Kapıda araç adı
listesi tutulmaz. `ContextWindow.external`, modele verilen geçmişte taint'li bir tur
olup olmadığını söyler; AgentLoop bunu `TurnState.history_external` olarak kapıya
taşır. Kapı, `history_external` ve `outbound` birlikteyken READ_ONLY olmayan çağrıya
onay sorar. Onay notu "Bu adım dış bir içerik okunduktan sonra istendi (önceki bir
turda)" olur; bu turda taint varsa mevcut not kullanılır. `tool_dispatch`
denetimine `outbound` ve `history_external` yazılır; bench bağımsız denetimi de bu
kuralı sayar.

**Sonuçlar:** Dış içerik bağlamda kaldığı sürece (en fazla `context_turns` tur ya da
`/yeni`, "Yeni sohbet" ile sıfırlanana dek) arama ve bağlantı açma onay ister.
Bağlam bütçesiyle düşen tur artık modele gitmediği için onay da gerekmez. Dosya
oluşturma/taşıma gibi yerel yan etkiler için ADR 0012'deki kalan risk sürer; yeni
bir dışarı giden araç `outbound` bildirmezse kural onu kapsamaz.
