# KKB OpenShift Control Center (KOCC) v0.4.0

Bu release, KOCC portalına ilk multi-cluster altyapısını ekler.

## Cluster bağlantıları

- `kkbtest`: Pod ServiceAccount kimliğiyle `load_incluster_config()`
- `rmtest`: `/etc/portal/clusters/rmtest.kubeconfig` üzerinden
  `new_client_from_config()`

Python içerisinde API host, token veya CA değeri tanımlanmaz.

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

## RMTEST Secret

```bash
oc project kocc

oc create secret generic kocc-remote-clusters \
  --from-file=rmtest.kubeconfig=./rmtest.kubeconfig \
  --dry-run=client -o yaml | oc apply -f -
```

RMTEST kubeconfig içindeki kullanıcı/ServiceAccount, aşağıdaki kaynaklarda
read yetkisine sahip olmalıdır:

- nodes
- namespaces
- pods
- config.openshift.io/clusterversions

## OpenShift nesneleri

`openshift/kocc-v0.4.0.yaml` içindeki namespace referansları `kocc` olarak
hazırlanmıştır. Proje namespace'i farklıysa uygulamadan önce değiştirin.

```bash
oc new-project kocc
oc apply -f openshift/kocc-v0.4.0.yaml
```

## Binary Build

Proje kök dizininde:

```bash
oc start-build kocc --from-dir=. --follow
oc rollout restart deployment/kocc
oc rollout status deployment/kocc
```

## Test

```bash
curl -k https://ROUTE/health
curl -k "https://ROUTE/api/summary?cluster=kkbtest"
curl -k "https://ROUTE/api/summary?cluster=rmtest"
```

Dashboard:

```text
https://ROUTE/?cluster=kkbtest
https://ROUTE/?cluster=rmtest
```
