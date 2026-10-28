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
