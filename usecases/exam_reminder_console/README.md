# Exam Reminder Console

Bu klasör, **orijinal CallScoot koduna dokunmadan** onun yerel API'sini kullanan bağımsız bir client app içerir.

Amaç:
- UI üzerinden öğrenci listesi girmek
- öğrencileri sırayla aramak
- sınav tarihi / saatini bildirmek
- katılım durumunu toplamak
- ulaşılmayanları işaretlemek
- cevaplanamayan soruları not olarak kaydetmek
- süreç ve konfigürasyonu web UI üzerinden izlemek

> Bu uygulama CallScoot runtime'ın üstünde çalışan ayrı bir use-case uygulamasıdır.
> `src/` altındaki asıl proje mantığına müdahale etmez.

---

## Neden bu mimari?

CallScoot zaten şu işleri yapıyor:
- telefon çağrısını başlatma / kontrol etme
- Bluetooth / SIP çağrı runtime'ı
- transcript / session / event log üretme
- ElevenAgents bağlantısını yönetme

Bu use-case uygulaması ise şu işleri yapar:
- öğrenci listesi ve kuyruk yönetimi
- kampanya ekranı / UI
- arama sırası ve retry politikası
- agent için öğrenciye özel bağlam üretimi
- transcript üzerinden sonuç analizi
- operasyonel takip ve CSV export

Yani ayrım şu şekildedir:

```text
Exam Reminder Console
   -> CallScoot local API
   -> CallScoot runtime + agent
   -> ElevenAgents
   -> Android / SIP call path
```

---

## Agent bilgisi nerede ayarlanmalı?

Kısa cevap:
- **temel persona / konuşma tarzı**: ElevenLabs agent içinde
- **öğrenciye özel veri ve bu aramaya özgü talimatlar**: bu client app tarafından

Bu app her arama için CallScoot API'ye şunları gönderir:
- `dynamic_variables`
- `contextual_update`
- açılışı doğru başlatmak için gerekirse `user_message`

Bu sayede:
- öğrenci adı
- sınav tarihi / saati
- arayan kurum adı
- konuşma kuralları
- fallback cümlesi
- SSS / bilgi bankası

her çağrıya özel olarak inject edilir.

Ek olarak çağrı bağlandıktan birkaç saniye sonra agent'e özel bir `user_message` gönderilerek konuşmanın öğrenciye özel açılış cümlesiyle başlaması zorlanır. Bu, generic `Merhaba, size nasıl yardımcı olabilirim?` açılışını bastırmak ve Bluetooth ses yolunun oturmasına biraz zaman vermek için eklendi.

### Pratik öneri

ElevenLabs agent içinde genel prompt'u sade tut:
- Türkçe konuş
- dinamik değişkenleri ve contextual update'leri takip et
- bilmediğin şeyi uydurma

Bu klasörde ayrıca önerilen temel prompt mantığı UI içinde de gösterilir.

---

## Özellikler

- Web UI (`0.0.0.0:8899` bind, yani local ağdan erişilebilir)
- UI üzerinden ADB / SIP telephony modu seçimi
- SIP server / username / password bilgilerini UI'dan kaydetme ve CallScoot runtime'a uygulama
- Tekil öğrenci ekleme
- CSV içeriğini textarea veya dosya seçimiyle içe aktarma
- Sıralı arama kuyruğu
- Öğrenci satırından tek tıkla **Şimdi Ara** manuel arama başlatma
- Generic açılışı bastırmak için gecikmeli açılış `user_message` tetikleme
- Ulaşılamayan kayıtlar için retry desteği
- CallScoot status görüntüleme
- UI üzerinden CallScoot config patch gönderme
- Prompt / contextual update preview
- Sonuç CSV export
- SQLite ile lokal veri saklama

---

## Dosyalar

```text
usecases/exam_reminder_console/
  app.py
  campaign.py
  callscoot_bridge.py
  analysis.py
  prompting.py
  storage.py
  index.html
  static/
    app.js
    style.css
  sample_students.csv
  run.sh
  exam-reminder-console.service
```

---

## Kurulum

Ön koşul:
- CallScoot kurulmuş olmalı
- `callscoot-api.service` çalışıyor olmalı
- agent runtime (önerilen: ElevenAgents) çalışıyor olmalı

Kontrol:

```bash
systemctl --user status callscoot-daemon.service
systemctl --user status callscoot-agent.service
systemctl --user status callscoot-api.service
curl http://127.0.0.1:8788/v1/health
```

Bu use-case uygulaması için ekstra Python paketi gerekmez; standart kütüphane kullanır.

Çalıştır:

```bash
cd /home/efekurucay/callscoot
python3 usecases/exam_reminder_console/app.py
```

veya:

```bash
cd /home/efekurucay/callscoot
./usecases/exam_reminder_console/run.sh
```

Tarayıcı:

```text
http://127.0.0.1:8899
```

Aynı local ağdaki başka cihazlardan da erişebilmek için uygulama varsayılan olarak `0.0.0.0` üstünde dinler.
Bu yüzden Linux makinenin LAN IP'si örneğin `192.168.1.50` ise şu şekilde de açılır:

```text
http://192.168.1.50:8899
```

Port değiştirmek istersen:

```bash
EXAM_REMINDER_PORT=8900 python3 usecases/exam_reminder_console/app.py
```

İstersen ayrı bir user service olarak da kullanabilirsin. Örnek unit dosyası:

```text
usecases/exam_reminder_console/exam-reminder-console.service
```

---

## Önerilen kullanım akışı

1. UI'dan öğrenci listesini yükle
2. Ayarlarda kurum adı, arayan adı, fallback cümlesi ve SSS alanlarını doldur
3. Gerekirse ADB veya SIP modunu seç, SIP bilgilerini gir ve "Seçili modu CallScoot'a uygula" butonuna bas
4. Gerekirse UI'daki CallScoot patch alanından ek runtime config gönder
5. Prompt preview ile örnek öğrencide agent bağlamını kontrol et
6. İstersen kampanyayı toplu başlat, istersen öğrenci satırındaki **Şimdi Ara** butonuyla tekil test araması yap
7. Öğrenci tablosundan sonuçları takip et
8. Süreç bitince CSV export al

---

## Sonuçların nasıl çıkarıldığı

Arama sonrası bu app, CallScoot session transcript'ini okuyup kaba sınıflandırma yapar:

- **ulaşılamadı**: caller transcript yoksa
- **katılacak**: transcript'te olumlu katılım ifadeleri varsa
- **katılmayacak**: olumsuz ifadeler varsa
- **kararsız / belirsiz**: net olmayan ifadeler varsa
- **geri dönüş gerekli**: fallback cümlesiyle eşleşen soru turu varsa

Bu analiz heuristik tabanlıdır. Gerekirse UI'dan operatör notu girilebilir.

---

## SSS alanı nasıl yazılmalı?

Örnek:

```text
Kimlik gerekli mi: Evet, geçerli bir kimlik belgesi bulundurması gerekir.
Sınav nerede: Ana bina girişinde danışmaya başvurmalıdır.
Geç kalırsa ne olur: Kurum prosedürüne göre sınav görevlisinin yönlendirmesi esas alınır.
```

Bu bilgiler her aramada agent bağlamına eklenir.

---

## Kısıtlar

- ElevenLabs agent prompt'unu bu app doğrudan değiştirmez
- transcript analizi deterministik / heuristik çalışır
- çok gürültülü görüşmelerde manuel gözden geçirme gerekebilir
- CallScoot aynı anda başka bir uygulama tarafından da aktif kullanılırsa session eşleşmesi karışabilir

---

## Neden orijinal projeye dokunulmadı?

İstek doğrultusunda bu çözüm tamamen ayrı use-case katmanı olarak yazıldı.

- `src/` dosyaları değiştirilmedi
- runtime mantığına karışılmadı
- yalnızca mevcut CallScoot API kullanıldı

Bu nedenle bakım ve yükseltme daha güvenlidir.
