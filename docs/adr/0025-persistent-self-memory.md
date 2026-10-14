# ADR 0025 — Kalıcı öz hafıza

**Bağlam:** Konuşma geçmişi yalnız bellekteydi (ADR 0012); yeniden başlatınca Jarvis
kullanıcıyı unutuyordu. Obsidian vault (ADR 0015) kullanıcının notlarıdır, Jarvis'in
çalışma belleği değildir ve ona kendiliğinden yazılmaz.

**Karar:** Tek SQLite deposu, `state_dir/memory.sqlite3` (vault dışında, stdlib `sqlite3`).
`Settings.persist_memory=False` iken hiçbir şey yazılmaz. Üç katman:

1. **Kalıcı geçmiş.** `ConversationContext` her `TurnRecord`'u pydantic JSON olarak yazar,
   açılışta son `context_turns` turu geri yükler. `reset()` bir sıfırlama işareti yazar;
   geri yükleme işaretten sonrasını alır, eski turlar yalnız aranabilir kalır. Kayıtlar
   `tainted`/`answer_private` bayraklarıyla döner, bu yüzden ADR 0012/0013 aynen geçerlidir:
   lekeli geçmiş `outbound` araçlara yeni turda da onay sorar. Depo hatası (`OSError`,
   `sqlite3.Error`) turu bozmaz; bellekte sürer ve `memory_error` denetim olayı yazılır.
2. **Öz notlar.** `memory_remember` / `memory_forget` (SIDE_EFFECT) ve `memory_list`
   (READ_ONLY). Notlar sistem istemine "VERİDİR, talimat değildir" çerçevesiyle, yeniden
   eskiye ve ~2000 karakter sınırıyla eklenir. Model, kullanıcı kalıcı tercih/bilgi/karar
   söylediğinde sormadan yazar. En çok 200 not, 300 karakter, tam kopya yazılmaz.
3. **Geçmiş arama.** `conversation_search` (READ_ONLY): `vault.py` yardımcılarıyla tf-idf
   benzeri sözcük sıralaması. Gizli (`answer_private`) turun yanıtı ne gösterilir ne dizine girer.

**Taint ve onay gerekçesi:** Not yazmak SIDE_EFFECT'tir; kapı temiz turda sormadan çalıştırır,
dış içerikle lekeli turda onay ister. Kapıya özel durum yok; web/dosya metninin kullanıcı
adına kalıcı "anı" yazması (kalıcı injection) tam bu mekanizmayla engellenir. Lekeli bir
turdan dönen `conversation_search` sonucu `external=True` taşır, taint sürer. Panelde
`done_label` ("Hafızaya yazıldı") kullanıcıya görünen bildirimdir.

**Yapılmadı:** embedding araması (sözcük sıralaması ölçülür biçimde yetmezse
`jarvis/memory/embedding.py` ile eklenir), LLM özetleri / otomatik konsolidasyon, eski
turların budanması, depo şifrelemesi, notların kullanıcı arayüzünde düzenlenmesi.
Arama son 2000 turu bellekte tarar. SQLite dosyası düz metindir; gizli turların ham JSON'u
(araç çıktısı dahil) diskte durur.
