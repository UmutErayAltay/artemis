# ADR 0049 — Takılabilir kod ajanı arka uçları ve başlangıç klasörü

**Durum:** Kabul (2026-10-28). ADR 0023 ve 0026'yı genişletir; SafetyGate ve onay
anlamı değişmez.

**Bağlam:** Arka plan proje işi yalnız Claude Code'u (`claude -p`) ve cwd = proje reposu
olarak başlatıyordu. Kullanıcı (1) hangi ajanın çalışacağını (Claude Code, Codex, başka
CLI), (2) ajanın nerede BAŞLAYACAĞINI (proje reposu, ikinci beyin = Obsidian vault'u,
başka kayıtlı klasör; böylece ajan `CLAUDE.md` / `AGENTS.md` kurallarını yükleyerek
başlar), (3) proje başına varsayılan + iş başına seçim istedi.

**Karar:**
- `code_agent.agents` kullanıcı profilleridir (`kind: claude|codex|generic`); yerleşik
  `claude` her zaman vardır (komutu ve manager/coder/tester modelleri eski alanlardan).
  `default_agent`, `workdirs` (takma ad -> mutlak yol) ve proje başına
  `{path, agent, workdir}` eklendi. Eski `projects: {ad: yol}` aynen çalışır.
- Başlangıç klasörü takma adları: `proje` (ayrılmış; hedef repo, varsayılan),
  `ikinci-beyin` (`memory.vault_path` verilince örtük; kullanıcı açıkça tanımlarsa onun
  tanımı geçer) ve kullanıcının `workdirs` girdileri. Model yalnız TAKMA AD seçer
  (`^[a-z0-9_-]{1,32}$`); yol, `..`, ayırıcı ve bilinmeyen ad reddedilir (kural 6).
- `ProjectSpec` (değişmez) `agent`, `workdir`, çözülmüş `repo_path`/`workdir_path` ve
  türetilmiş `approval_text` taşır. Onay ekranı mevcut mekanizmayla (tam spec) bunları
  gösterir; `approval_text` doğrulamada her zaman yeniden hesaplanır, modelin yazdığı
  değer kullanılmaz. Başlatmada takma adlar yeniden çözülür; onaylanan yol bugünkünden
  farklıysa (ayar değişmiş) iş başlamaz.
- Sonuç yargısı her tür için git'tendir (ajanın beyanı başarı değildir). Claude'un sıkı
  ölçütleri (completed çıktısı, Q/A, `--resume`, `--json-schema`) yalnız Claude içindir.
  Claude dışı: `done` = çıkış kodu 0 VE git okunabilir; özet yeni commit sayısını, kalan
  commitlenmemiş değişikliği, hiçbiri yoksa "git'te değişiklik görülmedi"yi yazar. Sıfır
  olmayan çıkış `error`. Bekleme/soru protokolü yoktur: ajan girdi isterse başarısız olur.
- cwd = çözülmüş başlangıç klasörü. Kirli ağaç reddi, base commit, commit listesi ve
  artık-değişiklik tespiti HER ZAMAN hedef repoda çalışır. Kilit hedef repodadır; ek
  olarak başlangıç klasörünün kendisi etkin işi olan bir repo yoluysa iş reddedilir.
- Görünür konsol kipi bu işte YOK. `AgentRun.mode` yalnız `"headless"`; profilde
  `visible_args` alanı ayrılmış ama kullanılmıyor.

**Bayrak tablosu:**

| Bayrak / davranış | Durum |
| --- | --- |
| claude: mevcut bayraklar aynen (`-p --output-format json --json-schema ... --agent coordinator`) | doğrulandı (mevcut) |
| claude: cwd'den ayrı klasörde `--add-dir <repo>` ek dizine erişim verir | doğrulandı (resmî belge) |
| claude: `CLAUDE.md` cwd ve üst klasörlerinden yüklenir; `--add-dir` klasörlerininki varsayılan yüklenmez | doğrulandı (resmî belge) |
| codex: `exec --sandbox workspace-write -C <dir> --json -o <dosya> -`, istem stdin'de (`-`), konumsal istem yok | doğrulandı (`codex exec --help`, codex-cli 0.164.0-alpha.11.1) |
| codex: `--add-dir <repo>` `exec`'in kendi seçeneği (ek yazılabilir klasör) | doğrulandı (aynı `--help`) |
| codex: `--skip-git-repo-check` yalnız başlangıç klasörü git deposu değilse | doğrulandı (aynı `--help`) |
| codex: ağ varsayılan kapalı kalır; `--full-auto` ve `--dangerously-bypass-approvals-and-sandbox` hiç kullanılmaz | doğrulandı / politika |
| codex: çıkış kodları | doğrulanmadı (belgelenmemiş; sıfır dışı = başarısız) |
| codex: `.git` workspace-write altında salt okunur; Codex kendisi commit atamayabilir | belgeye göre; iş yine git'ten yargılanır |
| generic: `args` şablonu, `{prompt_file}` `{prompt}` `{workdir}` `{repo}`, `prompt_via` | doğrulanmadı (her CLI'nin bayrakları kullanıcıdan gelir; aider örneği denenmedi) |
| gemini ve diğer CLI'ler | doğrulanmadı (yalnız generic profil) |

**Codex ikilisini bulma (Windows):** `codex` PATH'te olmayabilir (Microsoft Store paketi).
`locate_codex()` sırası: (1) profildeki açık mutlak `command` (yoksa başka yere
bakılmaz), (2) PATH, (3) `%APPDATA%\npm\codex.cmd`, (4) yalnız Windows'ta ve yalnız
komut `codex` iken sabit, salt okunur PowerShell betiği
(`Get-AppxPackage OpenAI.Codex` -> en yeni sürümün `InstallLocation`'ı +
`app\resources\codex.exe`, dosya var mı doğrulanır). Sürüm klasör adında olduğu için
sabit yazılmaz; sonuç süreç başına önbelleğe alınır. Testler PowerShell çalıştırmaz
(enjekte edilen sahte runner).

**Güvenlik notları:**
- Ajan, başlangıç klasörü `ikinci-beyin` iken vault'u DEĞİŞTİREBİLİR. Bu yalnız
  kullanıcı açıkça seçtiği ve onay metni "Bu klasördeki dosyaları değiştirebilir" dediği
  için kabul edilir. Aynı uyarı `proje` dışındaki her klasörde gösterilir.
- Kimlik doğrulama her CLI'nin kendi girişindedir (Claude/Codex login). Artemis sır
  saklamaz. Profildeki `env` yalnız değişken ADLARIDIR (`^[A-Z][A-Z0-9_]{0,63}$`); çocuk
  ortamı `child_environment()` + bu adlardan ortamda gerçekten var olanlardır. Değerler
  argv'ye, denetime, `repr`'e veya diske yazılmaz.
- Push yasağı Claude'da CLI deseni (`Bash(git push:*)` reddi) + talimattır. Codex'te
  istem talimatı + ağın varsayılan kapalı olmasıdır. Generic ajanlarda yalnız istem
  talimatıdır: **OS tarafından zorlanmaz**, OS izolasyonu yoktur (Claude dışı hiçbir ajan
  için de yoktur). `AUTHORITY_BOUNDARY` metni geçerlidir.
- Dosyaya yazılan istem (generic `prompt_via: file`) ve Codex `-o` çıktısı geçici
  dosyalardır; süreç temizliği doğrulandığında silinir. Argv liste olarak verilir,
  kabuk (`shell=True`) yoktur.
- Codex'in son mesajı özete "doğrulanmamış iddia" etiketiyle eklenir; başarıyı belirlemez.

**Sonuçlar / sınırlar:** Gerçek Codex/aider/Gemini ile canlı koşu yapılmadı; testler
sahte süreç ve gerçek git kullanır. Kullanıcı arayüzü (ajan/klasör seçimi, `approval_text`
gösterimi) ayrı dalgadadır.

---

## Ek (2026-10-28): yerleşik Codex, taslak silme ve görünür pencere kipi

**Durum:** Kabul. ADR 0049'un yukarıdaki kararlarını değiştirmez; "Görünür konsol kipi bu işte
YOK" maddesini kaldırır. SafetyGate ve onay anlamı aynıdır.

**Bağlam:** Seçici yalnız `claude` + kullanıcının tanımladığı ajanları listeliyordu; kurulu
Codex görünmüyordu ("yalnız kurulu" etiketi yanıltıcıydı). Taslaklar oluşturulabiliyor ama
silinemiyordu. Kullanıcı ajanı arka planda (headless) mı yoksa görünür bir konsol penceresinde
mi çalıştıracağını seçmek istedi.

**Kararlar:**
- **Yerleşik codex.** `BUILTIN_AGENTS = ("claude", "codex")`. `resolve_agent("codex")`
  kullanıcının `agents["codex"]` profilini (örn. açık mutlak yol) varsa onu, yoksa
  `AgentProfile(kind="codex")` döndürür (komut `locate_codex` ile bulunur); mevcut ayarlar aynen
  çalışır. `claude` yeniden tanımlanamaz. Ajan adı listesi tek yerden gelir
  (`CodeAgentConfig.agent_names()`); doğrulayıcılar (`default_agent`, proje girdisi) bunu kullanır.
  `code_agent_status` ayrıca `agent_status: {ad: {found, visible}}` döndürür; komut arama
  (`locate_codex` PowerShell çalıştırabilir, önbellekli) araç içinde `asyncio.to_thread` ile
  işçi tarafında yapılır, arayüz iş parçacığında yapılmaz. Arayüz bulunamayan ajanı
  "codex (bulunamadı)" diye gösterir, seçilemez (pasif) ve ipucu Ayarlar > GitHub > Kod ajanları'na
  yönlendirir. Claude seçicide "Claude Code" görünür (saklanan takma ad `claude`); ipucu Claude
  masaüstü uygulamasının iş alamadığını söyler. Ayarlar "Kod ajanları" listesi iki yerleşiği
  de gösterir; "Codex ekle" düğmesi gereksiz kaldığı için kaldırıldı.
- **Taslak silme.** `code_agent_discard` (argüman: `spec_id`, `extra=forbid`) yalnız başlatılmamış
  taslağı siler. Risk sınıfı `code_agent_cancel` ile aynıdır (SIDE_EFFECT): yalnız yerel,
  başlamamış üstveri kaydıdır; içerik `code_agent_draft` denetim olayında kalır, dosyalara ve
  repoya dokunulmaz, lekeli turda SafetyGate yine onay sorar. İş kaydı, çalışan iş ve başlamış
  taslak silinemez: bellekte `_drafts` + "bu spec bir işe bağlı mı" denetimi, kalıcı depoda
  `DELETE ... WHERE key=? AND kind='draft'` (anahtar ne olursa olsun iş satırı silinmez).
  Bilinmeyen kimlik dürüst başarısızlıktır. Denetime `code_agent_draft_discarded` yazılır.
- **Çalışma kipi.** `mode: background|visible` (varsayılan `background`): `ProjectEntry.mode`,
  `CodeAgentConfig.default_mode`, `project_mode(alias)`, `PrepareArguments.mode`, `ProjectSpec.mode`.
  Kip kullanıcının onayladığı spec'in parçasıdır ve `approval_text` içinde (4. satır) yazar;
  görünürde ek olarak "Görünür pencerede çalışır; pencereyi kapatırsan iş kesilir. Artemis bu
  kipte ajana soru-cevap yapamaz." uyarısı gelir. `approval_text` hâlâ doğrulamada yeniden
  türetilir; modelin yazdığı metin kullanılmaz. Desteklenmeyen ajan/platformda hazırlık ve
  başlatma dürüstçe reddedilir (Windows dışı; generic ajanda `visible_args` yok).
- **Görünür kipin süreci.** Windows'ta `CREATE_NEW_CONSOLE | CREATE_SUSPENDED`, boru yok;
  süreç yine bizim `Popen` tutamacımızdadır ve aynı job nesnesine (`WindowsProcessTree`)
  bağlanır, yani "İşi durdur" ve zaman aşımı süreç ağacını öldürür. `max_minutes` aynen geçerlidir.
  Soru-cevap, `--resume`, `--json-schema`/structured_output ve çıktı yakalama kapalıdır
  (stdout okunmaz). **Başarı yalnız git'ten ve çıkış kodundan yargılanır**: çıkış kodu 0 ise
  `done` (özet yeni commit sayısı ve kalan değişikliği yazar, ajan metni okunmaz), sıfır dışı ise
  `interrupted` ("Pencere kapandı veya ajan N koduyla çıktı; iş tamamlanmış sayılmadı"). Pencere
  elle kapatılınca süreç sıfır dışı çıkar; bu hiçbir zaman başarı sayılmaz. Hedef repodaki git
  doğrulaması, kirli ağaç reddi ve kilit headless ile aynıdır.
- **Görünür kipin argv'si.** Tam istem işin kendi state klasöründeki `code_agent_jobs/<iş>/task.md`
  dosyasına yazılır (state yoksa geçici klasör); argv'ye yalnız "Read <yol> and carry out the task
  described there; stop when done." yönergesi girer. İş bitince dosya ve klasör silinir.
  Ortam `run_environment` ile aynıdır (yalnız ad listesi). `--dangerously-skip-permissions`,
  `--dangerously-bypass-approvals-and-sandbox`, `--full-auto` ve `-a never` hiç kullanılmaz.

| Görünür kip argv'si | Durum |
| --- | --- |
| codex: `codex -C <klasör> [--add-dir <repo>] -a on-request -s workspace-write "<yönerge>"` | doğrulandı (`codex --help`, codex-cli 0.165.0-alpha.1.2: `[PROMPT]`, `-C`, `--add-dir`, `-a on-request`, `-s workspace-write`) |
| claude: `claude "<yönerge>" --permission-mode acceptEdits --allowedTools ... --disallowedTools Bash(git push:*) WebFetch WebSearch AskUserQuestion --append-system-prompt ... --agents ... --agent coordinator [--model m] --add-dir <iş klasörü> [<repo>]` | bayraklar headless ile aynı; etkileşimli sırada konumsal yönergenin çok değerli bayraklardan ÖNCE verilmesi **doğrulanmadı** (canlı koşu yok) |
| generic: profilin `visible_args` listesi; `{prompt_file}` `{workdir}` `{repo}` ve `{prompt}` (= kısa yönerge, tam istem değil) | doğrulanmadı (CLI bayrakları kullanıcıdan gelir); `visible_args` boşsa kip desteklenmez |

**Kabul edilen riskler:**
- Görünür kipte ajanın çıktısı okunamaz; sonuç git'ten ve çıkış kodundan başka doğrulanamaz.
  Ajan pencerede ne yaptığını (ör. yalnız okuma) Artemis göremez. Çıkış kodu 0 + git'te değişiklik
  yok "done" olabilir; özet bunu "git'te değişiklik görülmedi" diye söyler.
- Pencere kapatma anlamı: kapatma sıfır dışı çıkış üretir ve `interrupted` olur. Pencere
  kapatılmadan ajanın kendisi sıfır koduyla çıkarsa (örn. `/exit`) `done` sayılır; kullanıcı
  ajanı bilerek erken bıraktıysa bunu git sonucu ve özet gösterir. Çıkış kodu semantiği ajanlara
  göre canlı doğrulanmadı.
- Tam istem diskte düz dosyadır (işe özel state klasörü; iş bitince silinir, Artemis çöküp iş
  `interrupted` kalırsa dosya kalabilir). İstem argv'ye girmez, fakat dosya yolu argv'dedir; `.cmd`
  kabuğu gerektiren bir ikilide yolda `&`/`%` gibi karakterler yanlış yorumlanabilir (varsayılan
  state yolu bunları içermez).
- Claude görünür kipte `AskUserQuestion` yine yasaktır (headless ile aynı duruş): kullanıcı pencerede
  yazabilir ama ajan soru sormaz. Codex `-a on-request` ile onayı pencerede kullanıcıya sorar;
  Artemis bu onayları görmez.
- Windows konsolu, gerçek `claude`/`codex` ve görünür pencere canlı denenmedi; testler sahte süreç
  ve gerçek git kullanır, pencere bayrağı fabrikaya giden argümandan doğrulanır.
