# crewreopt

AFTN mesajıyla tetiklenen ekip yeniden atama demosu.

Bir uçuş gecikir (DLA) ya da iptal olur (CNL). Sistem mesajı ayrıştırır, uçuş tablosunu günceller ve bir
yeniden planlama işini kuyruğa koyar. Worker, OR-Tools CP-SAT ile **kurallara uyan ve mevcut plandan en az
sapan** yeni ekip atamasını bulur. Arayüz hangi görevin hangi ekipten hangisine, **neden** geçtiğini gösterir.

Demonun asıl konusu model değil, modelin etrafındaki mühendislik: uzun süren bir optimizasyon işinin
güvenilir şekilde tetiklenmesi, tam olarak bir kez çalışması, izlenmesi ve sonucunun yeniden üretilebilmesi.

> Tüm uçuşlar, ekipler ve mesajlar uydurmadır. Gerçek kayıt ya da şirket içi doküman kullanılmamıştır.

## Çalıştırma

```bash
docker compose up --build
```

- Arayüz: http://localhost:8000
- API dokümanı: http://localhost:8000/docs
- RabbitMQ yönetim arayüzü: http://localhost:15672 (crew / crew)

`.env` dosyası olmadan demo varsayılanlarıyla açılır (API anahtarı `demo-key`). Değerleri değiştirmek için
`cp .env.example .env`. `/health` içinde commit görünsün isterseniz:
`GIT_SHA=$(git rev-parse --short HEAD) docker compose up --build`

Testler (servis gerekmez; SQLite ve sahte yayıncı kullanır):

```bash
pip install -r requirements.txt
ruff check . && pytest -q
```

## Mimari

```
                 POST /messages (X-API-Key)
  AFTN metni ───────────────┐
                            ▼
                     ┌─────────────┐   1) ayrıştır (aftn.py)        ┌──────────────┐
   tarayıcı ◄──────► │     api     │──────────────────────────────► │   postgres   │
   GET / (Gantt)     │   FastAPI   │   2) tek transaction:          │ flights      │
   GET /schedule     └──────┬──────┘      uçuşu güncelle +          │ crews        │
   GET /jobs/{id}           │             jobs satırı (queued)      │ assignments  │
                            │ 3) job_id yayınla                     │ jobs         │
                            ▼    (persistent + publisher confirm)   │ dead_letters │
                     ┌─────────────┐                                └──────▲───────┘
                     │  rabbitmq   │  durable kuyruk "reopt_jobs"          │
                     └──────┬──────┘                                       │
                            │ prefetch=1, iş BİTİNCE ack                   │
                            ▼                                              │
                     ┌─────────────┐   4) queued -> running (atomik UPDATE)│
                     │   worker    │   5) girdi anlık görüntüsünü sakla    │
                     │             │──────────────────────────────────────►┘
                     └──────┬──────┘   7) atama + sonuç tek transaction
                            │ 6) saf fonksiyon çağrısı
                            ▼
                     ┌─────────────┐
                     │  model.py   │   CP-SAT; veritabanını ve kuyruğu bilmez
                     └─────────────┘
```

| Dosya | Sorumluluk |
|---|---|
| `app/aftn.py` | ICAO Doc 4444 DLA / CNL / DEP / ARR ayrıştırıcı. Saf. |
| `app/model.py` | CP-SAT modeli, değişiklik gerekçesi, çözümsüzlük teşhisi. Saf. |
| `app/db.py` | Şema (tek dosya) ve paylaşılan sorgular. |
| `app/api.py` | HTTP uçları, idempotent iş oluşturma, dead-letter. |
| `app/broker.py` | RabbitMQ: durable kuyruk, persistent mesaj, publisher confirm. |
| `app/worker.py` | İşi sahiplen, çöz, yaz, en sonda ack. |
| `app/seed.py` | Demo verisi: 14 görev, 7 ekip (2 yedek). |

## Model

- Karar değişkeni: `x[görev, ekip] ∈ {0,1}`
- Her iptal edilmemiş görev tam olarak bir ekibe atanır.
- Aynı ekibin iki görevi arasında en az `MIN_REST` dakika olmalı. Çakışan çiftler Python'da önceden
  hesaplanır; kısıt `x[a,e] + x[b,e] ≤ 1` olarak tek satır kalır.
- Ekip başına günlük toplam görev süresi `≤ MAX_DUTY` (basitleştirilmiş).
- Kalkmış (DEP) görev mevcut ekibine kilitlenir.
- Amaç: `min 100 · (ekibi değişen görev sayısı) + 30 · (kullanılan yedek ekip sayısı)`
- Mevcut plan çözücüye ipucu (hint) olarak verilir.

Çıktıda atamanın yanında şunlar da var:

- **Gerekçe:** her değişiklik için düz metin, örn. `C1 flies P01: P01 and P02 would overlap by 25 min`.
- **Çözücü izi:** bulunan her çözümde amaç ve en iyi sınır; arayüzde gap'in kapanışı çizilir.
- **Çözümsüzlük teşhisi:** `INFEASIBLE` çoğu zaman veri hatasıdır. Her görevin "karşılansın" kuralı bir
  varsayım (assumption) literaline bağlanır, CP-SAT çelişen varsayımları döndürür, küçültme ile birlikte
  karşılanamayan en küçük görev kümesi bulunur. İş kaydında "şu görevler birlikte karşılanamıyor" yazar.

## Güvenilirlik kuralları

| Kural | Nasıl sağlanıyor | Test |
|---|---|---|
| Aynı mesaj iki kez gelirse tek iş | `jobs.idempotency_key` üzerinde unique constraint. Anahtar `reopt:<tarih>:<gövde hash'i>`; başlık hash'e girmez, yeniden iletim aynı anahtarı üretir. | `test_same_message_twice_creates_one_job` |
| Uçuş güncellemesi ile iş kaydı tutarlı | İkisi tek transaction'da yazılır. | `test_dla_shifts_both_legs_and_queues_one_job` |
| Kuyruk erişilemezse iş kaybolmaz | İş satırı önce yazılır; yayın başarısızsa 503 döner, iş `queued` kalır, aynı istek tekrarlanınca yeniden yayınlanır. | `test_queue_outage_keeps_job_and_retry_publishes_it` |
| Aynı iş iki kez çalışmaz | Worker `UPDATE ... WHERE status='queued'` ile sahiplenir; satır güncellenmediyse ack edip geçer. | `test_duplicate_delivery_is_skipped` |
| Worker çökerse iş kaybolmaz | Ack iş bitince verilir; broker mesajı yeniden teslim eder (`redelivered`), `running` kalmış iş devralınır. | `test_running_job_is_taken_over_only_on_redelivery` |
| Zehirli iş kuyruğu tıkamaz | 3 denemeden sonra `failed`. | `test_poison_job_fails_after_max_attempts` |
| Süre sınırı | `SOLVER_TIME_LIMIT_S`. Sınırda çözüm varsa `succeeded` + `FEASIBLE` + gap; hiç çözüm yoksa `timed_out`; çözümsüzlük kanıtlandıysa `failed` + teşhis. | `test_infeasible_job_fails_with_diagnosis_and_keeps_plan` |
| Bozuk mesaj kaybolmaz | Ham metin + neden `dead_letters` tablosuna, gönderene 422. | `test_malformed_message_is_dead_lettered` |
| Sonuç yeniden üretilebilir | Modele verilen girdi `jobs.input_snapshot` içinde; `GET /jobs/{id}/input`. | `test_demo1_...` |
| İzlenebilirlik | Tek satır JSON log, her satırda `job_id`; iş bitişinde durum, amaç, sınır, gap, süre. | – |

## Demo

Arayüzdeki metin kutusuna yapıştırın ya da `curl` kullanın. Başa dönmek için arayüzde **Veriyi sıfırla**.

### 1) Gecikme: THY1591 90 dakika gecikir

```
FF LTFMZPZX
231200 EDDFZQZX
(DLA-THY1591-LTFM0830-EDDF-DOF/261123)
```

C1 ekibi P01'i (THY1591/1592) uçup 14:10'da dönecek, 15:15'te P02'ye çıkacaktı (65 dk dinlenme). Gecikmeyle
dönüş 15:40 olur; dinlenme −25 dk. O saatte boşta normal ekip yok, bu yüzden tek bir görev yedek ekibe geçer.
Beklenen: 1 değişiklik, amaç 130 (100 + 30), `OPTIMAL`.

### 2) İptal: THY2412 iptal olur

```
FF LTFMZPZX
231210 LTAIZPZX
(CNL-THY2412-LTFM1500-LTAI-DOF/261123)
```

C3'ün öğleden sonraki görevi (P08) düşer.

- **1'den sonra gönderilirse** plan aynı kalır: yedekteki görevi C3'e almak bir değişikliktir (100), kazancı
  bir yedek (30). Model kararlılığı tercih eder; bu ağırlıklar bir iş kararıdır.
- **Sıfırlayıp önce 2, sonra 1 gönderilirse** P02 yedeğe değil boşa çıkan C3'e gider: amaç 100, yedek
  kullanılmaz.

`curl` ile:

```bash
curl -X POST localhost:8000/messages -H 'X-API-Key: demo-key' \
  --data-binary '(DLA-THY1591-LTFM0830-EDDF-DOF/261123)'
curl localhost:8000/jobs/<job_id>
```

Denemeye değer diğer şeyler: aynı mesajı iki kez göndermek (`"duplicate": true`, tek iş); bozuk mesaj
(`(DLA-THY1591-LTFM2460-EDDF-DOF/261123)` → 422); çözüm sırasında `docker compose kill worker`
(iş yeniden teslim edilir ve tamamlanır); `docker compose logs worker` (JSON loglar).

## Bilinçli basitleştirmeler

- Bir bacak iptal olursa gidiş-dönüş görevin tamamı iptal sayılır.
- Gecikme, aynı görevin sonraki bacaklarına aynen yansır (yerde kalış süresi sabit).
- Tüm mesaj tiplerinde `DOF/` zorunlu; gerçek ARR mesajlarında çoğu zaman bulunmaz.
- Görev süresi ve dinlenme kuralları gerçek uçuş görev süresi mevzuatının (EASA FTL) çok sade bir özetidir.
- Tek gün; gece yarısını aşan görev yok.
- İstek başına RabbitMQ bağlantısı; şema için Alembic yok.

## Canlıya alırken neler eklerdim

- **Gerçek kuyruk izleme:** kuyruk derinliği, ack bekleyen mesaj sayısı, iş süresi ve gap için metrikler
  (Prometheus) ve alarmlar; dead-letter exchange; `queued` durumda bekleyen işler için süpürücü ya da
  outbox tablosu (bugün veritabanı ile kuyruk arasındaki tutarlılık yeniden gönderime dayanıyor).
- **Uzun süren çözümler:** çözücüyü ayrı süreçte çalıştırıp heartbeat'i canlı tutmak, işi iptal edebilmek,
  aynı gün için işleri sıraya sokmak (tarih başına kilit).
- **Gurobi lisansı:** konteynerde token server ya da WLS; lisans alınamazsa işin anlamlı bir hatayla
  `failed` olması; çözücüyü ortam değişkeniyle seçebilmek.
- **Ortamlar:** test / UAT / canlı için aynı imaj, farklı yapılandırma; şema değişiklikleri için Alembic.
- **Gizli bilgi kasası:** API anahtarı ve veritabanı parolaları Vault ya da benzeri bir kasadan.
- **Kurumsal SSO:** API anahtarı yerine OIDC; kimin hangi işi tetiklediğinin kaydı.
- **Model sürümleme:** her iş kaydında model sürümü ve parametreleri; yeni sürümü eski girdilerle
  karşılaştırma (anlık görüntüler bunun için saklanıyor).
- **Girdi anlık görüntüsünün saklanması:** bugün `jobs` tablosunda JSON; canlıda nesne deposunda, saklama
  süresi tanımlı.
- **Uçuş bilgi sistemi entegrasyonu:** mesajları HTTP yerine doğrudan AMHS/AFTN ağ geçidinden ya da
  kurumsal mesaj yolundan almak; sıra dışı gelen mesajları ele almak.
