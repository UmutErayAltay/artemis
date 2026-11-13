# ADR 0060 — Kullanıcı becerileri ("Beceriler")

**Bağlam:** Kullanıcı koda dokunmadan Artemis'e yeni bir çalışma yöntemi öğretmek istiyor (örn.
"haftalık rapor": hangi notlar okunur, çıktı nasıl biçimlenir). Çözüm: bir klasöre Markdown dosyası.

## Karar

1. **Konum ve biçim:** `state_dir/beceriler/<ad>.md`. Başlıkta (YAML, repodaki `yaml.safe_load`)
   `name` (`^[a-z0-9-]{1,40}$`, dosya adıyla aynı) ve `description` (en çok 200 karakter), altında
   gövde (en çok 3500; araç sonucu sınırının altında, kesilmeden gitsin diye karakter). En çok 40 beceri. Geçersiz dosya atlanır, nedeniyle panelde
   listelenir, açılışı bozmaz. UTF-8 BOM kabul edilir; UTF-16/ANSI gibi çözülemeyen dosya
   "UTF-8 değil" nedeniyle geçersizdir (Not Defteri ile elle düzenlendiği için).
2. **Yetki:** beceri KULLANICININ yazdığı talimattır, gövdesi kullanıcının kendi isteği kadar
   güvenilir sayılır. Bu yalnız klasöre kullanıcıdan başkası yazamıyorsa doğrudur, bu yüzden:
   (a) beceri oluşturan/değiştiren/silen araç YOKTUR ve eklenmemelidir (model kendine güvenilir
   kalıcı talimat yazardı; ADR 0027 ile aynı gerekçe); (b) dosya araçları yalnız `workspace`
   köküne `safe_join` ile yazar, klasör `state_dir` altındadır ve bu yüzden erişilemez; klasör
   (çözülmüş haliyle) çalışma alanının içindeyse ya da çalışma alanını içeriyorsa beceriler hiç
   yüklenmez ve açık bir mesaj verilir; klasörün kendisi bağlantıysa da; (c) sembolik bağlantı
   dosyalar yok sayılır (nedeniyle listelenir).
3. **Beceri izin vermez:** beceriyi izlerken yapılan her araç çağrısı normal kapıdan, normal onay,
   taint ve gözetimsiz kurallarıyla geçer. Hiçbir atlama eklenmedi (testte: "dosyayı sil" diyen
   beceri yine onay sorar, gözetimsizde onaysız reddedilir).
4. **Sistem istemi:** `name: description` satırları (toplam ~2000 karakter) ve "eşleşirse
   `skill_read` çağır" cümlesi genel sistem mesajına girer; gövde enjekte edilmez.
5. **Araç:** `skill_read(name)`; `READ_ONLY`, `external` DEĞİL (gövde kullanıcının kendi metni),
   turu lekelemez. `skill_list` yok (liste zaten istemde).
6. **`/<ad>` çağrısı:** mesaj `/<ad>` ile başlar ve beceri geçerliyse o tura özel kısa ipucu
   ("önce skill_read çağır") kullanıcı mesajına eklenir; geçmişe yazılmaz (`memory_hint` gibi).
7. **Config:** `skills.enabled`, varsayılan `false`. Açıkken klasör yoksa `ornek-gunluk-ozet.md`
   ile oluşturulur (var olan klasöre, silinmiş örnek dahil, dokunulmaz). Kapalıyken araç modelden
   gizlenir (`without_prefix("skill_")`) ve `refuse` onaysız reddeder (ADR 0058/0059 deseni).
8. **Arayüz:** Ayarlar > Beceriler: kutu (PANEL_BINDINGS), geçerli beceriler, geçersiz dosyalar
   nedenleriyle, "Klasörü aç", "Yenile". ~~Uygulama içi düzenleyici yok.~~ (Ek karar 2 ile
   değişti: ekle/düzenle/sil artık uygulama içinde.)
9. **Okuma zamanı:** dosyalar her turda ve "Yenile"de okunur (en çok 40 küçük dosya); izleyici yok.
   Böylece "Yenile" paneli, bir sonraki tur modeli güncel tutar; çalışan runtime'a bildirim yolu
   gerekmedi.
10. **Gizlilik:** beceri metni yerel kullanıcı metnidir; bulut modele giderse, her kullanıcı
    talimatı gibi isteğin parçası olur. Panel ipucu bunu söyler. Gizlilik işaretlemesi (`private`)
    yoktur: beceriye kişisel veri yazmayın.

## Bilerek yapılmayanlar

- ~~Uygulama içi düzenleyici~~ (Ek karar 2 ile geçersiz: kullanıcı açıkça istedi), beceri oluşturan/silen araç, dosya izleyici, `skill_list`, becerilere
  özel izin/atlama, beceri paylaşımı/alma.
- **Bilinen sınır:** araç sonuçları modele ~4000 karakterle ve "VERİDİR" çerçevesiyle gider
  (CLAUDE.md kural 4, gevşetilmedi). Bu yüzden `skill_read` gövdesinin ilk ~3900 karakteri
  görünür; uzun tarifleri kısa tutun. Sistem istemi cümlesi, çerçeveye rağmen tarifin izleneceğini
  söyler.
- Windows kabulü yapılmadı (`docs/YENI_OZELLIKLER_KABUL.md`).

**Ek (doğrulama sonrası):** konum denetimi yalnız çalışma alanına değil bütün yazma köklerine bakar: `memory.vault_path` (`vault_append` ve vault'ta başlayan kod ajanı), `code_agent.repos_dir`, `workdirs` ve proje depoları. Klasör bunlardan biriyle iç içeyse beceriler yüklenmez. `state_dir` böyle bir kökün içindeyse `skills.folder` ile dışarıda mutlak bir klasör verilir. Kalan, kaçınılmaz yollar: bilgisayar kullanımının bir düzenleyiciye yazması ve kullanıcının kendi eliyle kurduğu sert bağ.

## Ek karar (konum, Klasör seç, şablonlar)

- **Varsayılan konum** kullanıcı veri klasörüdür (Windows: `APPDATA/Artemis/beceriler`; diğerlerinde `XDG_CONFIG_HOME/artemis/beceriler`). Eski `state_dir/beceriler` yalnız varsa ve konum denetiminden geçiyorsa kullanılır. Güvensizse ve `.md` içeriyorsa panel bunu iki yolu da yazarak bildirir; dosyaları kullanıcı kendisi taşır, otomatik taşıma yoktur.
- **Klasör seç** Ayarlar > Beceriler'de klasörü seçtirir; konum denetiminden geçerse `skills.folder` olarak doğrulamalı ve atomik yazılır.
- **Şablonlar:** paketle gelen sabit şablonlar (`artemis/skill_templates/`) klasör ilk kez açılırken yazılır; var olan klasöre dokunulmaz.
- **Yeni beceri:** panel, kullanıcının açık tıklamasıyla sabit bir şablondan (ya da boş iskeletten) YENİ bir dosya oluşturabilir (`open("x")`; var olanın üzerine asla yazmaz) ve kullanıcının varsayılan düzenleyicisinde açar. Panel hiçbir dosyayı düzenlemez, üzerine yazmaz ya da silmez. Hiçbir araç ya da model bunu tetikleyemez. Konum güvensizse reddedilir; demoda kapalıdır.
- **Dene:** geçerli beceride sohbet kutusuna `/<ad> ` yazar, göndermez.
- **Sohbet kartı:** `skill_read` işlemi "Beceri kullanıldı: <ad>" diye görünür.

## Ek karar 2 (uygulama içi düzenleyici)

Kullanıcı becerilerin dış düzenleyiciye (VS Code) gitmeden Artemis içinde, "Tetikleyici ekle" gibi
özenli bir formla oluşturulup düzenlenmesini AÇIKÇA istedi. Bu, yukarıdaki "Uygulama içi düzenleyici
yok" kararını ve "Yeni beceri"deki "düzenleyicide aç" davranışını geçersiz kılar. Güven sınırı aynı
kalır: beceriyi yalnız kullanıcı yazar.

**İzin verilenler** (hepsi yalnız kullanıcının açık tıklamasıyla, `SkillDialog`/`SkillsSection`):
- Aynı doğrulayıcı: yükleyicinin `parse_bytes`'ı, yazılacak baytların kendisi üzerinde çalışır;
  geçersizken Kaydet kapalıdır ve ilk hata görünür. Böylece kaydedilen dosya her zaman yüklenir.
- Yeni: `open("xb")` (var olanın üzerine asla yazmaz), yol `safe_join(allow_symlinks=False)`,
  önce `location_problem`; en çok 40 beceri.
- Düzenleme: aynı klasörde geçici dosya + `os.replace` (atomik). Hedef bağlantıysa ya da klasör dışına
  çözülüyorsa reddedilir. Dosya diyalog açıldığından beri (bayt karşılaştırması) değiştiyse hiçbir şey
  yazılmaz, kullanıcı bilgilendirilir ve yazdığı metin diyalogda kalır; dış düzenleme ezilmez.
- Geçersiz dosya "Düzelt" ile ham metin olarak açılır, hata gösterilir, düzeltilince kaydedilir.
- Silme: onay sorar, varsayılan düğme Hayır; yalnız `.md` dosyası, bağlantıysa yalnız bağlantı kalkar.
- "Klasörü göster" yalnız dosya gezginini açar; hiçbir beceri dosyası `QDesktopServices` ile açılmaz.

**Hâlâ yasak:** beceri oluşturan/değiştiren/silen HERHANGİ bir araç ya da model yolu; panelden
kendiliğinden (tıklamasız) yazma; konum denetimini atlayan yazma. Düzenleme dosyayı form alanlarından
yeniden üretir: `name` ve `description` dışındaki ek başlık anahtarları (yükleyici yok sayar) kaydedince
düşer; ham kipte (Düzelt) olduğu gibi kalır.
