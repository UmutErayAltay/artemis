# ADR 0090: Otomatik onay kapsamı ve lekeli turda rutin kaydı

- Tarih: 2026-11-26
- Durum: Kabul edildi
- İlgili: ADR 0039 (otomatik kip), ADR 0058 (rutinler), ADR 0087 (makro kaydedici), issue #37

**Bağlam:** 2026-11-20/21'de kullanıcı otomatik onay kipinin (`computer_use.mode: auto`)
kapsamını genişletti ve `8a5c784` bunu koda geçirdi: `volume_set`, `brightness_set`,
`close_application`, `open_application` (kaldırıcı hariç), `power` (`lock`, `sleep`,
`cancel_shutdown`) ve `routine_save` `auto_mode_ok` aldı; `routine_save`'in "dış içerik okunmuş
turda reddet" kuralı kaldırıldı. Bu değişiklik ADR'siz yapıldı ve CLAUDE.md'deki "`auto_mode_ok`
başka bir araca ADR olmadan eklenmez" kuralıyla çelişti (#37).

Araçlar aynı riskte değil. Ses, parlaklık, uygulama açma/kapatma ve kilit/uyku etkisi o anla
sınırlıdır. Rutin ise kalıcıdır: lekeli turda kaydedilen rutin sonra temiz bir turda çalışır ve
SIDE_EFFECT adımları onaysız yürür. Otomatik kipte lekeli turda onaysız kayıt, bir web
sayfasındaki ya da ekrandaki talimatın kalıcı olarak "kullanıcının rutini" olmasına izin verir;
ADR 0058'in engellemek istediği yol budur.

**Karar:**

1. Yukarıdaki anlık araçların otomatik kipte onay atlaması kullanıcı kararı olarak kabul edilir
   ve bu ADR ile belgelenir; davranış değişmez. `shutdown` ve `restart` her kipte onaylı kalır.
2. `routine_save` dış içerikli turda reddedilmez (kullanıcı kararı: rutin kaydı her zaman açık).
   Normal kipte her zaman onay sorar.
3. Otomatik kipte `routine_save` yalnız temiz turda onay atlar. Dış içerik bağlamdayken
   (`context.turn_tainted`: bu turda okunan ya da geçmişte kalan dış içerik) onay penceresi
   açılır. Uygulama: `RoutineSave.auto_mode_allowed` geçersiz kılınır; kapıya araç adı yazılmaz,
   ikinci onay politikası eklenmez.
4. Makro kaydedici (ADR 0087) aynı aracı kullandığı için bu kuralı kendiliğinden devralır.

**Sonuç:** Kullanıcının istediği akış (sormadan kaydet, metinle çift onay yok) temiz turda
sürer. Enjekte metin kalıcı rutin üretmek için kullanıcının onay penceresinde tam adımları
görüp onaylamasını gerektirir. Test: `tests/test_routines.py` hem lekeli turda iki kipte onayı
hem temiz turda otomatik kipte atlamayı doğrular; geçersiz kılma silinince lekeli test kırılır.
