# KOCC AI Backend Phase-2

KOCC portalından bağımsız, stateless FastAPI servisidir. Phase-1 mevcut KOCC
UI/API/SQLite/Kubernetes collector koduyla entegre değildir. LLM erişimi olmasa
da process başlar; `/health` 200 döner ve `/ready` bağımlılıkları `degraded`
olarak raporlayabilir.

Phase-2 `/api/v1/chat` akışında seçilen cluster için MCP `tools/list` sonucunu
dinamik olarak OpenAI function tool şemasına dönüştürür. Modelin istediği izinli
araçlar aynı MCP session üzerinden sıralı çalıştırılır ve sonuç LLM'e geri
verilir. Döngü iteration/tool-call limitleriyle bounded durumdadır.

## Configuration

- `AI_LLM_BASE_URL` (default `https://llm.kkb.com.tr`)
- `AI_LLM_API_TOKEN` (zorunlu secret; default yok)
- `AI_LLM_MODEL` (chat için zorunlu)
- `AI_LLM_TIMEOUT_SECONDS` (default `20`)
- `AI_MCP_KKBTEST_URL` (default `http://openshift-mcp:8080/mcp`)
- `AI_MCP_TIMEOUT_SECONDS` (default `10`)
- `AI_AGENT_MAX_ITERATIONS` (default `6`, allowed `1..10`)
- `AI_AGENT_MAX_TOOL_CALLS` (default `10`)
- `AI_AGENT_MAX_TOOL_RESULT_CHARS` (default `40000`)
- `AI_AGENT_MAX_USER_CHARS` (default `8000`)

Standart kütüphanedeki `urllib`, TLS doğrulamasını açık tutarak OpenAI-compatible
LLM çağrılarını ve MCP Streamable HTTP JSON-RPC/SSE yanıtlarını işler. Yeni HTTP
veya agent framework dependency'si eklenmemiştir.

## Read-only security boundary

LLM cluster seçmez; request içindeki `cluster` backend registry üzerinden tek
MCP client'a bağlanır. Cluster/context/kubeconfig/API server/MCP URL içeren model
tool argümanları reddedilir. Tool çıktısı untrusted data kabul edilir, response'a
ham halde konmaz ve yapılandırılmış karakter limitinde kesilir. Prompt, tool
çıktısı ve credential loglanmaz.

Phase-2 allowlist:

```text
configuration_view, events_list, namespaces_list, nodes_stats_summary,
nodes_top, pods_get, pods_list, pods_list_in_namespace, pods_log, pods_top,
projects_list, resources_get, resources_list
```

`nodes_log` ve write/exec özellikleri MCP tarafından sunulsa dahi modele expose
edilmez ve çalıştırılmaz.

## Local validation

```bash
cd kocc-ai-backend
../.venv/bin/python -m compileall app
../.venv/bin/pytest -q
../.venv/bin/python -m pip check
```

## Test OpenShift deployment

Namespace ve EgressIP zaten mevcut olmalıdır. MCP deployment/RBAC değişmez.
Gerçek token repository'ye yazılmadan Secret oluşturulur:

```bash
oc project test-openshift-ai-assistant
oc create secret generic kocc-ai-llm \
  --from-literal=api-token='<LLM_API_TOKEN>'
oc apply -f openshift/kocc-ai-backend-test.yaml
oc start-build kocc-ai-backend --from-dir=. --follow
oc rollout status deployment/kocc-ai-backend
```

Servis dışarı açılmaz. Geçici local kontrol için:

```bash
oc port-forward service/kocc-ai-backend 18080:8080
curl -s http://127.0.0.1:18080/health
curl -s http://127.0.0.1:18080/ready
curl -s http://127.0.0.1:18080/api/v1/clusters
curl -s 'http://127.0.0.1:18080/api/v1/mcp/status?cluster=kkbtest'
curl -s 'http://127.0.0.1:18080/api/v1/mcp/tools?cluster=kkbtest'
```

Firewall açıldıktan ve ConfigMap'te `AI_LLM_MODEL` tanımlandıktan sonra:

```bash
curl -s -X POST http://127.0.0.1:18080/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"cluster":"kkbtest","message":"Cluster durumunu özetle"}'
```

Phase-2 chat read-only MCP tool-calling yapar. Conversation persistence,
streaming, yeni UI, endpoint history, AI database veya autonomous remediation
yoktur. Firewall kapalıysa chat kontrollü `llm_unavailable` döndürür; process,
health ve MCP kontrolleri çalışmaya devam eder.
