# OpenShift Clusters Monitoring Platform v0.4.0

## Portal authentication and Patch Monitoring

Test deployment enables server-side portal authentication and the isolated
KKBTEST1 Patch Monitoring module. On first startup, when `portal_user` is empty,
`KOCC_ADMIN_USERNAME` and `KOCC_ADMIN_PASSWORD` bootstrap the administrator.
The password is stored only as PBKDF2-HMAC-SHA256 hash in SQLite; later pod
restarts do not overwrite a password changed at `/change-password`.

Patch Monitoring uses only the configured Patch Master Service DNS and fixed
`/api/v1/*` paths. Browser credentials and the Patch Master bearer token never
cross the server boundary. Five-second UI polling targets Patch Master only and
does not trigger Kubernetes collection. Disable the module atomically with
`KOCC_PATCH_ENABLED=false`.

Required environment variables are `KOCC_AUTH_ENABLED`,
`KOCC_ADMIN_USERNAME`, `KOCC_ADMIN_PASSWORD`, `KOCC_SESSION_SECRET`,
`KOCC_AUTH_COOKIE_SECURE`, `KOCC_PATCH_ENABLED`, `KOCC_PATCH_BACKEND_URL`,
`KOCC_PATCH_TIMEOUT_SECONDS`, and optional `KOCC_PATCH_API_TOKEN`. When the
token is empty KOCC sends no Authorization header, allowing the current
unauthenticated internal Patch Master. When Patch Master authentication is
enabled, both services must receive the same token. Auth is enabled whenever
any admin/session credential is supplied; partial configuration fails startup
instead of exposing the portal anonymously. Credentials must be created from
`openshift/kocc-auth-secret-template.yaml` without committing real values.

```bash
oc project ocp-monitoring-portal-test
cp openshift/kocc-auth-secret-template.yaml /tmp/kocc-auth-secrets.yaml
# Replace every REPLACE_WITH_* value outside the repository.
oc apply -f /tmp/kocc-auth-secrets.yaml
oc apply -f openshift/kocc-v0.4.0.yaml
oc rollout status deployment/kocc
```

## SQLite Persistence Phase-1 (test)

Test deployment mevcut `kocc-data` PVC'sini `/data` altında mount eder ve
SQLite veritabanını `/data/kocc.db` konumunda otomatik oluşturur. WAL mode ve
5000 ms busy timeout kullanılır. Schema migration'ları `schema_version`
tablosunda izlenir; ilk migration `cluster_snapshot` ve
`workload_image_snapshot` tablolarını oluşturur.

Persistence yalnız başarılı, yeni Kubernetes collection sonrasında çalışır.
Cache hit yeni kayıt üretmez ve aynı cluster için minimum kayıt aralığı beş
dakikadır. SQLite initialization veya insert hataları dashboard/API yanıtını
engellemez. Phase-1 UI ve REST endpointleri SQLite'tan veri okumaz.

Bu release, OpenShift Clusters Monitoring Platform'a ilk multi-cluster
altyapısını ekler.

## Cluster bağlantıları

- `kkbtest`: Pod ServiceAccount kimliğiyle `load_incluster_config()`
- `rmtest`: `/etc/portal/clusters/rmtest.kubeconfig` üzerinden
  `new_client_from_config()`

Python içerisinde API host, token veya CA değeri tanımlanmaz.

OpenShift API çağrıları, bağlantı sorunlarının portal worker'larını
süresiz bloke etmemesi için connect/read timeout ile yapılır.

Overview; problemli Pod, restart/CrashLoop, eksik request/limit ve namespace
resource özetlerini aynı Pod/Node/Namespace snapshot'ından üretir. Workload,
PVC ve Route çağrıları Overview sırasında yapılmaz; yalnız ilgili sayfa veya
arama açıldığında lazy-load edilir. Bu kaynaklardan biri alınamazsa kendi
bölümü `Unavailable` gösterir ve diğer sayfalar çalışmaya devam eder.

KKBTEST in-cluster ServiceAccount için ClusterRole yalnız
`config.openshift.io/clusteroperators` kaynağında `get` ve `list` yetkileri
verir. RMTEST aynı çağrıyı remote kubeconfig'ten oluşturulan ApiClient ile yapar.
Collector cluster adını veya authentication yöntemini bilmez.

Platform sayfasındaki EgressIP görünümü `k8s.ovn.org/v1` kaynaklarını yalnız
`get/list` yetkileriyle ve cluster-bazlı 60 saniyelik sonuç cache'iyle lazy-load
eder. Local ServiceAccount ve remote kubeconfig kullanıcısının yetkisini
doğrulayın:

```bash
oc auth can-i list egressips.k8s.ovn.org
```

Missing Requests / Limits özeti varsayılan olarak yalnız adı `openshift-` ile
başlayan namespace'leri hariç tutar. UI'daki `Include OpenShift namespaces`
seçeneği açıldığında aynı, önceden toplanmış Pod verisinin tümü gösterilir; bu
seçim yeni bir Kubernetes API çağrısı oluşturmaz ve diğer resource toplamlarını
değiştirmez.

`Last Refresh` değeri backend'de açıkça `Europe/Istanbul` timezone'una çevrilir.
Auto refresh Manual, 15 saniye, 30 saniye, 1 dakika ve 5 dakika seçeneklerini
destekler; cluster URL'de, namespace/ranking/OpenShift tercihleri browser
storage'da ve scroll konumu session storage'da korunur.

## Kurumsal görünüm ve operasyon tabloları

Header'da KKB'nin resmi görsel galerisindeki `KKB TURUNCU - LACİVERT LOGO`
kullanılır. PNG dosyası `app/static/kkb-turuncu-lacivert-logo.png` altında local
serve edilir; public siteye runtime hotlink yapılmaz. Görsel kaynak:
`https://www.kkb.com.tr/Content/img/logos/kkb-turuncu-lacivert-logo.png`.
Dashboard paleti lacivert header, turuncu vurgu ve düşük kontrastlı açık yüzeyler
üzerine kuruludur.

Missing Requests / Limits verisi container başına tek canonical kayıt olarak
sunulur. Kayıt; namespace, Pod, container, dört resource alanının defined/missing
durumu ve `missing_count` değerini içerir. UI mevcut snapshot üzerinde arama,
numeric/text sorting, sayfa başına 50 kayıt pagination ve filtrelenmiş tüm veri
için CSV export uygular. Namespace Resource Summary sıralaması görüntülenen
`mCPU/Core` veya `MiB/GiB` metnini değil raw millicore/byte data attribute'larını
kullanır.

Auto refresh `setInterval` kullanmaz. Dashboard response'u tamamlandıktan sonra
tek recursive `setTimeout` kurulur; `refreshInProgress` guard aynı sayfadan ikinci
bir navigation başlatılmasını engeller. Manuel refresh aynı single-flight yolu
kullanır. Collector her request'te Pod, Node ve Namespace listelerini birer kez
alır ve bütün widget'ları bu snapshot'tan üretir. `/health` hiçbir Kubernetes API
çağrısı yapmaz.

## Dashboard sayfaları ve performans

- `/`: yüksek seviyeli Overview
- `/resources`: namespace resources, Top CPU/Memory request/limit ve missing resources
- `/workloads`: restart sıralaması ve Pod/Deployment/StatefulSet/DaemonSet araması
- `/platform`: Platform Health, Critical Controls ve lazy EgressIP/PVC/Route özeti
- `/storage`: backward-compatible PVC detay ekranı
- `/routes`: backward-compatible Route detay ekranı
- `/health-overview`: operator, node, pod ve collection diagnostics
- `/diagnostics`: lazy, read-only problem Pod listesi
- `/diagnostics/{namespace}/{pod}`: container state, event, log ve rule-based analiz

Teknik `/health` liveness endpoint'i değişmeden hızlı ve cluster API'sinden
bağımsızdır. Readiness probe aynı özellikteki `/ready` endpoint'ini kullanır.
Collector `collect_nodes`, `collect_pods`, `collect_namespaces`,
`resource_summary`, `collect_version`, `collect_operators` ve toplam süreyi
INFO seviyesinde ölçer; credential veya Secret loglamaz.

Başarılı dashboard snapshot'ları cluster anahtarı bazında thread-safe ve
varsayılan 120 saniye cache'lenir. Diagnostics cache varsayılanı da 120
saniyedir. Bu değerler `KOCC_SNAPSHOT_TTL_SECONDS` ve
`KOCC_DIAGNOSTICS_CACHE_TTL_SECONDS` ile değiştirilebilir. Manuel Refresh
snapshot cache'ini bypass eder; normal sayfa geçişleri ve auto refresh etmez.
Eşzamanlı aynı-cluster talepleri tek collection üzerinde birleşir;
KKBTEST ve RMTEST lock/cache alanları ayrıdır. Yeni collection hata verirse son
başarılı snapshot `Stale snapshot` ve data age bilgisiyle sunulur. Tek Uvicorn
worker korunur; böylece process başına cache kopyası ve ilave bellek baskısı
oluşturulmaz. Mevcut 502/restart gözlemleri için gerçek pod event/log ve
`OOMKilled` durumu cluster üzerinde ayrıca kontrol edilmelidir; kod tarafında
uzun tekrar collection, büyük Overview HTML'i ve eşzamanlı refresh baskısı
azaltılmıştır.

Restart/502 kök nedenini cluster üzerinde doğrulamak için:

```bash
oc get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState}'
oc get pods -l app.kubernetes.io/name=kocc
oc describe pod <pod>
oc logs <pod> --previous
oc get events --sort-by=.lastTimestamp | tail -50
oc adm top pod <pod> --containers
oc describe pod -l app.kubernetes.io/name=kocc
oc get pod -l app.kubernetes.io/name=kocc -o jsonpath='{range .items[*]}{.metadata.name}{" reason="}{.status.containerStatuses[0].lastState.terminated.reason}{" exit="}{.status.containerStatuses[0].lastState.terminated.exitCode}{" restarts="}{.status.containerStatuses[0].restartCount}{"\n"}{end}'
oc logs deployment/kocc --previous
oc get events --sort-by=.lastTimestamp
```

`OOMKilled`, `exitCode`, `signal`, liveness/readiness failure, node eviction,
container restart reason ve Route/Service endpoint olayları görülmeden tek bir
restart kök nedeni kesin kabul edilmemelidir.

## Read-only Pod Diagnostics

Diagnostics ana dashboard collection'ına dahil değildir. Problem Pod listesi
yalnız `/diagnostics` açıldığında; Pod detail, event ve son `50/100/200/500` log
satırı yalnız `Diagnose` bağlantısı açıldığında alınır. Succeeded/Completed Pod'lar
listelenmez. Analyzer OOMKilled/137, ImagePullBackOff/ErrImagePull,
CrashLoopBackOff, FailedScheduling/Pending, FailedMount/FailedAttachVolume,
Unhealthy probe ve Evicted kanıtlarına açıklanabilir kurallar uygular; kanıt
yoksa kesinlik iddiasında bulunmaz.

ServiceAccount'a yalnız `pods/log get` ve `events get/list` eklenmiştir;
Pod `get/list/watch` yetkisi mevcut kuraldan kullanılır. Delete/create/patch/update
yetkisi yoktur. RMTEST kubeconfig kullanıcısının da remote cluster üzerinde aynı
minimum read izinlerine sahip olması gerekir. Log içeriği yalnız API response'unda
kullanıcıya döner; uygulama loguna yazılmaz. Startup INFO kaydı version, process
ID ve UTC startup timestamp içerir, credential içermez.

## Cluster health score

Health score açıklanabilir 100 puanlık bir modeldir:

- Node readiness: 30 puan. NotReady node oranına göre doğrusal kesinti yapılır.
- Problemli Pod'lar: 20 puan. Succeeded Pod'lar çıkarıldıktan sonra non-ready
  Pod oranına göre doğrusal kesinti yapılır.
- Resource baskısı: 40 puan. CPU/Memory request için `%80` ve `%100`; limit
  için `%100` ve `%150` eşikleri dört bağımsız sinyal olarak değerlendirilir.
  Her sinyal sırasıyla `0`, `5` veya `10` puan keser.
- ClusterOperator health: 10 puan. Her Degraded operator 3, Available olmayan
  operator 2, Progressing operator 1 puan keser; toplam kesinti 10 ile
  sınırlandırılır. Operator verisi alınamıyorsa cluster cezalandırılmaz.

Sonuç `90–100 Healthy`, `75–89 Warning`, `0–74 Critical` olarak gösterilir.
CPU ve Memory overcommit yüzdeleri request/limit değerlerinin cluster
capacity'ye oranından hesaplanır.

Python 3.12 build'i, eski çalışan monitoring portal ile aynı exact runtime
sürümlerini kullanır:

- `fastapi==0.116.1`
- `starlette==0.47.3`
- `uvicorn==0.35.0`
- `jinja2==3.1.6`
- `kubernetes==33.1.0`

## Dizin yapısı

```text
.
├── app
│   ├── __init__.py
│   ├── cluster_loader.py
│   ├── collector.py
│   ├── main.py
│   ├── resource_parser.py
│   └── templates
│       └── index.html
├── openshift
│   ├── kocc-v0.4.0.yaml
│   └── rmtest-secret-template.yaml
├── Dockerfile
├── requirements.txt
└── README.md
```

## OpenShift image akışı

Tüm namespaced kaynaklar `ocp-monitoring-portal-test` project'inde oluşturulur.
Binary Build sonucu aynı project'teki `kocc:0.4.0` ImageStreamTag'ine yazılır.
Deployment'ın başlangıç image referansı şudur:

```text
image-registry.openshift-image-registry.svc:5000/ocp-monitoring-portal-test/kocc:0.4.0
```

Deployment ayrıca `image.openshift.io/triggers` annotation'ıyla
`kocc:0.4.0` ImageStreamTag'ini izler. Build tamamlandığında OpenShift image
trigger, Deployment image alanını ImageStream'in resolve ettiği immutable image
referansıyla günceller ve yeni rollout başlatır. `imagePullPolicy: Always`, aynı
release tag'iyle tekrarlanan Binary Build'lerde node cache'inden eski image'ın
kullanılmasını önler.

KOCC ServiceAccount ve ImageStream aynı project'te olduğu için OpenShift'in
otomatik `system:image-pullers` yetkisi kullanılır; ayrı bir registry pull secret
veya cross-project RoleBinding gerekmez.

## Dynatrace exclusion

KOCC workload'u Dynatrace injection dışındadır. Pod template üzerindeki
`dynatrace.com/inject: "false"` annotation'ı yalnızca KOCC Pod'larını etkiler;
cluster genelindeki Dynatrace kurulumunu değiştirmez. Yeni KOCC Pod'unda
Dynatrace kaynaklı `LD_PRELOAD` bulunmamalıdır.

## Kurulum ve doğrulama

Aşağıdaki komutlar `kocc-v0.4.0` dizininde çalıştırılmalıdır.

### 1. OpenShift manifestlerini apply et

```bash
oc project ocp-monitoring-portal-test
oc apply -f openshift/kocc-v0.4.0.yaml
```

Bu aşamada ImageStreamTag ve RMTEST Secret henüz yoksa Deployment Pod'unun
geçici olarak `Pending`, `ErrImagePull` veya volume mount bekleme durumunda olması
normaldir. Sonraki adımlar eksik kaynakları oluşturur.

### 2. RMTEST kubeconfig Secret'ını oluştur

Gerçek kubeconfig repository'ye eklenmemelidir. Secret doğrudan yerel dosyadan
oluşturulur:

```bash
oc create secret generic kocc-remote-clusters \
  --from-file=rmtest.kubeconfig=./rmtest.kubeconfig \
  --dry-run=client -o yaml | oc apply -f -
```

RMTEST kubeconfig içindeki kullanıcı veya ServiceAccount; `nodes`, `namespaces`,
`pods`, `config.openshift.io/clusterversions` ve isteğe bağlı operator widget'ı
için `config.openshift.io/clusteroperators` kaynaklarında read yetkisine sahip
olmalıdır.

### 3. Binary Build başlat

```bash
oc start-build kocc --from-dir=. --follow
```

### 4. Build sonucunu kontrol et

```bash
oc get build
oc get builds -l buildconfig=kocc
oc get build -l buildconfig=kocc
```

Son build `Complete` durumunda olmalıdır.

### 5. ImageStreamTag'in oluştuğunu doğrula

```bash
oc get imagestream kocc
oc get istag kocc:0.4.0
oc get imagestreamtag kocc:0.4.0
oc get buildconfig kocc -o jsonpath='{.spec.output.to.kind}{" "}{.spec.output.to.namespace}{"/"}{.spec.output.to.name}{"\n"}'
```

BuildConfig output sonucu
`ImageStreamTag ocp-monitoring-portal-test/kocc:0.4.0` olmalıdır.

### 6. Deployment rollout yap

ImageStream trigger normalde build sonrası rollout'u otomatik başlatır. Mevcut
hatalı Pod'u kesin olarak yenilemek için rollout yeniden başlatılır:

```bash
oc rollout restart deployment/kocc
oc rollout status deployment/kocc
```

### 7. Pod image ve pull durumunu kontrol et

```bash
oc get deployment kocc -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
oc get pods -l app.kubernetes.io/name=kocc
oc describe pod -l app.kubernetes.io/name=kocc
oc describe serviceaccount kocc
oc auth can-i get imagestreams/layers \
  --as=system:serviceaccount:ocp-monitoring-portal-test:kocc
```

Yetki kontrolü `yes` dönmelidir. Deployment image alanı build öncesinde sabit
internal-registry pullspec'ini, image trigger çalıştıktan sonra ise aynı
ImageStreamTag'in resolve edilmiş image referansını gösterebilir.

Dynatrace injection ve `LD_PRELOAD` kontrolü:

```bash
BUILD_POD=$(oc get pods \
  -l openshift.io/build.name \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}')

oc get pod "$BUILD_POD" \
  -o jsonpath='{.spec.initContainers[*].name}{"\n"}'

oc get pod "$BUILD_POD" -o yaml | \
  grep -iE 'dynatrace|oneagent|LD_PRELOAD'

RUNTIME_POD=$(oc get pods \
  -l app.kubernetes.io/name=kocc \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}')

oc get pod "$RUNTIME_POD" \
  -o jsonpath='{.spec.initContainers[*].name}{"\n"}'

oc get pod "$RUNTIME_POD" -o yaml | \
  grep -iE 'dynatrace|oneagent|LD_PRELOAD'
```

Init container listelerinde `dynatrace-operator` bulunmamalı; iki `grep` komutu da
boş sonuç dönmelidir.

### 8. Health ve cluster API endpointlerini test et

```bash
ROUTE=$(oc get route kocc -o jsonpath='{.spec.host}')

curl -k "https://${ROUTE}/health"
curl -k "https://${ROUTE}/api/summary?cluster=kkbtest"
curl -k "https://${ROUTE}/api/summary?cluster=rmtest"
```

Dashboard adresleri:

```text
https://ROUTE/?cluster=kkbtest
https://ROUTE/?cluster=rmtest
```

## Yerel test

```bash
python -m pip install -r requirements-dev.txt
pytest
```

## Performans ve restart teşhisi

Overview, mevcut cluster snapshot'ından üretilen Executive Dashboard'dur. KPI,
CPU/Memory namespace request dağılımları, request/limit sıralamaları, overcommit gauge'ları,
hotspot ve deterministic aksiyon önerileri yeni Kubernetes çağrısı yapmaz.
Executive görseller metrics-server gerçek tüketimini değil, tanımlı resource
request değerlerinin cluster capacity içindeki dağılımını gösterir. Trendler
yalnız tarayıcı `localStorage` alanında son 30 Overview yenilemesini tutar.
Ana dağılım grafiğinde capacity'nin `%1` değerinin altındaki namespace'ler ve
ilk 10 namespace dilimi dışındaki kayıtlar `Diğerleri` altında birleştirilir;
CPU/Memory toggle aynı cached payload içinde çalışır.

Uygulama her Kubernetes API/işleme adımı için `cluster`, `op`, `duration_ms`,
varsa `items` ve `cache_hit` alanlarını içeren yapılandırılmış INFO logları üretir.
Bir saniyeyi aşan adımlar ayrıca `slow_operation` olarak WARNING seviyesinde
yazılır. Snapshot cache loglarında hit/miss, stale fallback ve snapshot yaşı
görülebilir. Bu ölçümler gerçek cluster ağ/API gecikmesini ancak deployment
sonrasında gösterir.

Dashboard snapshot'ı cluster başına varsayılan 120 saniye tutulur; Overview ve Resources
aynı snapshot'ı paylaşır. Manuel Refresh cache'i bir kez bypass eder. Workloads,
Storage ve Routes HTML kabukları full dashboard collection beklemeden render
edilir ve kendi read-only API verilerini lazy-load eder. Diagnostics problem-pod
listesi de varsayılan 120 saniyelik ayrı bir sonuç cache'i kullanır. Süreler
`KOCC_SNAPSHOT_TTL_SECONDS` ve `KOCC_DIAGNOSTICS_CACHE_TTL_SECONDS` environment
değişkenleriyle pozitif tam saniye olarak ayarlanabilir. Auto Refresh cache'i
bypass etmez; yalnız manuel Refresh zorunlu collection başlatır.

Cluster-wide Pod listelerinde `resource_version="0"` kullanılır. Kubernetes list
semantiğinde bu değer API server watch cache'inden cevap verilmesine izin verir;
monitoring snapshot'ı için uygundur ve herhangi bir nesneyi değiştirmez.
Diagnostics ana listesi Event API çağrısı yapmaz. Eventler yalnız pod detayında
namespace-scoped ve `involvedObject.name`/`involvedObject.kind=Pod`
selector'larıyla alınır.

```bash
POD=$(oc get pods -l app.kubernetes.io/name=kocc -o jsonpath='{.items[0].metadata.name}')
oc get pod "$POD" -o wide
oc describe pod "$POD"
oc logs "$POD" --previous
oc adm top pod "$POD"
oc logs deployment/kocc | grep -E 'perf cluster=|slow_operation|cache\.|resources_page_payload|missing_resources_collected'
```

## Production checklist

- ServiceAccount ve minimum gerekli RBAC izinlerini doğrula.
- RMTEST kubeconfig Secret mount'unu ve Secret rotasyon prosedürünü doğrula.
- EgressIP, firewall kuralları ve remote cluster bağlantısını test et.
- Kurumsal Nexus `PIP_INDEX_URL` erişimini secret/config üzerinden sağla; kimlik bilgisini image'a yazma.
- BuildConfig output ImageStreamTag ve internal registry image akışını doğrula.
- Route TLS termination, sertifika zinciri ve kurum DNS kaydını doğrula.
- Resource request/limit, replica sayısı, readiness ve liveness probe değerlerini kapasiteye göre ayarla.
- Uygulama loglarının merkezi log platformuna aktarıldığını doğrula.
- Kubeconfig ve diğer Secret'ların sahiplik ve rotasyon tarihlerini takip et.
- KKBTEST ve RMTEST network/API erişimini release öncesi doğrula.
- Önceki ImageStreamTag/digest'e dönüşü içeren rollback adımını release kaydına ekle.
- Manifest, image tag ve uygulama sürümünün aynı release numarasını kullandığını doğrula.
