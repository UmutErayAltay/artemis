---
name: haftalik-rapor
description: Haftanın işlerini, açık konuları ve gün özetlerini yöneticiye gidecek kısa bir haftalık rapora çevirir.
---
Kullanıcı haftalık rapor isterse:

1. memory_open_work ile açık ve yarım işlere bak.
2. Son yedi günün her biri için daily_summary_read çağır (day: YYYY-MM-DD). Özeti olmayan günü atla, uydurma.
3. Gerekirse conversation_history ile bu haftanın konuşmalarından ne yapıldığını tamamla.
4. Raporu dört başlıkta yaz:
   - Bitenler
   - Sürenler (her birine bir cümlelik durum)
   - Engeller ve kararı beklenenler
   - Gelecek hafta ilk üç iş
5. Her başlıkta en çok beş madde. Kısa, düz Türkçe; ölçülebilir olanı sayıyla yaz.
6. Bilmediğin bir sonucu "tamamlandı" diye yazma; emin değilsen "durumu belirsiz" de.

Rapor bitince kullanıcıya panoya kopyalamak isteyip istemediğini sor; isterse write_clipboard kullan.
