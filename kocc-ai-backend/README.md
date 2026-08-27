# KOCC AI Backend Phase-1

KOCC portalından bağımsız, stateless FastAPI servisidir. Phase-1 mevcut KOCC
UI/API/SQLite/Kubernetes collector koduyla entegre değildir. LLM erişimi olmasa
da process başlar; `/health` 200 döner ve `/ready` bağımlılıkları `degraded`
olarak raporlayabilir.

## Configuration

- `AI_LLM_BASE_URL` (default `https://llm.kkb.com.tr`)
- `AI_LLM_API_TOKEN` (zorunlu secret; default yok)
- `AI_LLM_MODEL` (chat için zorunlu)
- `AI_LLM_TIMEOUT_SECONDS` (default `20`)
- `AI_MCP_KKBTEST_URL` (default `http://openshift-mcp:8080/mcp`)
- `AI_MCP_TIMEOUT_SECONDS` (default `10`)

Standart kütüphanedeki `urllib`, TLS doğrulamasını açık tutarak OpenAI-compatible
LLM çağrılarını ve MCP Streamable HTTP JSON-RPC/SSE yanıtlarını işler. Yeni HTTP
veya agent framework dependency'si eklenmemiştir.

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

Phase-1 chat yalnız basit LLM completion yapar. MCP tool-calling agent loop,
conversation persistence, yeni UI, endpoint history veya database yoktur.
