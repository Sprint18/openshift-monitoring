# OpenShift Clusters Monitoring Platform v0.4.0

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
- `/storage`: PVC requested capacity, durum ve StorageClass tablosu
- `/routes`: Route/namespace/host arama tablosu
- `/health-overview`: operator, node, pod ve collection diagnostics

Teknik `/health` liveness endpoint'i değişmeden hızlı ve cluster API'sinden
bağımsızdır. Readiness probe aynı özellikteki `/ready` endpoint'ini kullanır.
Collector `collect_nodes`, `collect_pods`, `collect_namespaces`,
`resource_summary`, `collect_version`, `collect_operators` ve toplam süreyi
INFO seviyesinde ölçer; credential veya Secret loglamaz.

Başarılı dashboard snapshot'ları cluster anahtarı bazında thread-safe 15 saniye
cache'lenir. Eşzamanlı aynı-cluster talepleri tek collection üzerinde birleşir;
KKBTEST ve RMTEST lock/cache alanları ayrıdır. Yeni collection hata verirse son
başarılı snapshot `Stale snapshot` ve data age bilgisiyle sunulur. Tek Uvicorn
worker korunur; böylece process başına cache kopyası ve ilave bellek baskısı
oluşturulmaz. Mevcut 502/restart gözlemleri için gerçek pod event/log ve
`OOMKilled` durumu cluster üzerinde ayrıca kontrol edilmelidir; kod tarafında
uzun tekrar collection, büyük Overview HTML'i ve eşzamanlı refresh baskısı
azaltılmıştır.

Restart/502 kök nedenini cluster üzerinde doğrulamak için:

```bash
oc get pods -l app.kubernetes.io/name=kocc
oc describe pod -l app.kubernetes.io/name=kocc
oc get pod -l app.kubernetes.io/name=kocc -o jsonpath='{range .items[*]}{.metadata.name}{" reason="}{.status.containerStatuses[0].lastState.terminated.reason}{" exit="}{.status.containerStatuses[0].lastState.terminated.exitCode}{" restarts="}{.status.containerStatuses[0].restartCount}{"\n"}{end}'
oc logs deployment/kocc --previous
oc get events --sort-by=.lastTimestamp
```

`OOMKilled`, probe failure, process exit code ve Route/Service endpoint olayları
görülmeden tek bir restart kök nedeni kesin kabul edilmemelidir.

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
