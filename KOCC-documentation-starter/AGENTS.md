# KOCC Agent Instructions

Project: KKB OpenShift Control Center (KOCC)

Repository: Sprint18/openshift-monitoring

## Rules
- Enterprise multi-cluster OpenShift platform
- Local cluster: load_incluster_config()
- Remote clusters: config.new_client_from_config()
- Kubeconfigs under /etc/portal/clusters/
- collector.py only accepts ApiClient
- cluster_loader.py owns all connection logic
- Never embed host/token/CA
- Deliver complete files
- Never commit secrets
