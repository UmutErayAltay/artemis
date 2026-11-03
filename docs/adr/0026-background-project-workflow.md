# ADR 0026 — Proje taslağı, arka plan işleri ve aşamalı soru/cevap

**Durum:** Kabul. Ajan türü (Claude dışı) ve başlangıç klasörü seçimi ADR 0049'dadır.

**Bağlam:** ADR 0023'ün ilk sürümü görevi başlatır, fakat iş durumu bellektedir;
zaman aşımından sonraki `communicate()` sınırsızdır. Faz 5, sesli görüşmeden
onaylanabilir bir taslak ve izlenebilir bir kodlama işi üretmelidir.

**Karar:**
- Haiku dahil mevcut sohbet sağlayıcısı isteği netleştirir. Proje, görev, kapsam ve
  kabul ölçütleri içeren taslak oluşturulur; taslak hazırlamak kod ajanını başlatmaz.
  Kod ajanı yalnız tam taslak gösterilerek SafetyGate onayından sonra çalışır.
- Yönetici ve alt ajan modelleri config'tedir. Claude Code'un resmi `--agents` /
  `--agent` arayüzü kullanılır; varsayılan rol dağılımı kullanıcı tarafından
  yapılandırılabilen Opus yönetici, Sonnet kodlayıcı, Haiku mekanik/test ajanıdır.
  Model kimliği/fiyatı koda gömülmez. Profil gerçek çağrı modelini zorlayan bir
  harcama garantisi değildir; CLI'nin mevcut erişimi ve limitleri geçerlidir.
- Sabit `--json-schema` çıktısı `question` ve `completed` türlerini ayırır.
  Soru çıktısında süreç normal biçimde biter, iş `waiting` olur. Kullanıcı cevabı
  tek kullanımlık soru kimliği ve geçerli yakalanmış session UUID ile `--resume`
  üzerinden yeni segmente taşınır. Yerel repo çıktısı VERİDİR; yeni yetki değildir.
  Cevap verme de SafetyGate'ten geçer ve onay ister. Native AskUserQuestion'a
  sahte protokol cevabı veya ajana otomatik araç onayı üretilmez.
- İşin toplam son tarihi segmentler ve soru beklemesi boyunca değişmez. İptal,
  süre dolması ve kapanış süreç ağacını sahiplik sınırında sonlandırır; bütün
  beklemeler sınırlıdır. Temizlenemeyen süreç `done` sayılmaz ve raporlanır.
- İş metaverisi SQLite'ta tutulur. Başlangıçta yarım işler otomatik yeniden
  çalıştırılmaz; kesintiye uğramış olarak raporlanır. Aynı fiziksel repo başka
  takma adla da eşzamanlı kullanılamaz. Başarı için CLI exit=0, is_error=false,
  completed çıktısı ve git durumunun hatasız doğrulanması birlikte gereklidir.
- Projeler sekmesi taslak, durum, soru/cevap, iptal ve sonucu gösterir. UI doğrudan
  tool.execute veya jobs.start/cancel çağırmaz; ortak runner/dispatcher'dan geçer.

**Yetki sınırı:** ADR 0023'teki kullanıcının güvenilen yerel repo kararı korunur;
VM/container kurulmaz. Claude Code'un kendi Bash/alt ajan araçları Artemis
SafetyGate'iyle tek tek denetlenmez. Push yasağı izin deseni ve talimattır, OS
izolasyonu garantisi değildir. Onay kapsamı bu sınırı gösterir. Kimlik bilgileri
Claude'un resmi girişinde kalır; Artemis anahtar/token okumaz veya alt sürece aktarmaz.
Başkasının reposu, otomatik ücretli API yedeği ve kontrolsüz yeniden başlatma eklenmez.

**Sonuçlar:** Windows ve gerçek Claude Code kabulü ayrı elle yapılır. Sahte CLI
testleri abonelik erişimini, görev gecikmesini veya gerçek model dağılımını kanıtlamaz.
Bu karar Artemis'in mevcut ses sağlayıcısını ve CPA bağlantısını değiştirmez.

Kaynaklar: [headless çıktı ve resume](https://code.claude.com/docs/en/headless),
[alt ajanlar](https://code.claude.com/docs/en/sub-agents), ADR 0023.
