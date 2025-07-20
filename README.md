### Steps to start trino cluster:
1. start docker
2. create local kubernetes cluster using kind
    - `kind create cluster test-cluster`
    - `kind get clusters`
3. Add `trino`, and `metrics-server` helm charts onto helm repo
    - `helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/`
    - `helm repo add trino https://trinodb.github.io/charts`
    - `helm repo list`
    - `helm search repo` -- get all helm charts available
   
4. Install metric-server for exposing metrics ( required for HPA ). Note that it should be installed in `kube-system` namespace
    - `helm upgrade --install --set args={--kubelet-insecure-tls} metrics-server metrics-server/metrics-server --namespace kube-system`
    - `kubectl get all --namespace kube-system`

5. Change directory to `~/projects/kube-trino` and start `kubernetes operator service`. The service monitors coordinator scaling and spawns worker deployments as needed
    - `kopf run --namespace default --verbose operator.py`

6. Start a trino cluster. Install it from local chart repo for testing with values to override
    - `helm install resil --values trino-override.yaml ../charts/charts/cluster-init`
    - `kubectl get all -o wide` -- default namespace

7. Check operator service logs to see if worker nodes and services are created as needed
    - `kubectl get all -o wide` -- default namespace 

6. Figure out service port for trino coordinator service and do port forwarding
    - `kubectl port-forward service/resil-1-coordinator 8090:8090`

7. Run simple query and check response to see if coordinator and nodes are tied
    - `trino http://localhost:8090`
    - `select * from system.runtime.nodes`


### Setup gateway for HA cluster mode
1. Start postgresql container for gateway backend storage
    - `docker volume rm kube-trino_postgres-data` (delete persistent postgres docker volume if need be)
    - `docker-compose -f compose.yaml start`
    

2. Create configmap from default routing rule to be provided to gateway
    - `kubectl create configmap routing-rules-config --from-file=routing-rules.yml`

3. Start gateway using helm
    - `helm install gateway --values gateway-override.yaml ../charts/charts/gateway`

4. Port forward gateway service port to localhost
    - `kubectl port-forward service/trino-gateway 8080:8080`

5. Navigate to UI and add backends and routing rules, if not present, according to the trino cluster you have setup
    - `http://localhost:8080`
    - Backend cluster must proxy to `http://<coordinator_service_name>:8090`
    - Routing logic must point to backend cluster name created above

6. Run simple query and check response from CLI
    - `trino http://localhost:8080`
    - `select * from system.runtime.nodes`

7. Navigate back to UI to see the query details


### Running kopf in kubernetes
- create cluster config maps:
  - `kubectl create configmap kopf-operator-code --from-file=operator.py`
  - `kubectl create configmap trino-override --from-file ../trino-override.yaml`
  - `kubectl create configmap post-construct-templates --from-file=../../charts/charts/post-construct/templates/`
  - `kubectl create configmap post-construct-chart --from-file=../../charts/charts/post-construct/`
- The init container of kopf in manifest.yaml will copy these configs to a common directory which is used by kopf during helm install
- apply rbac.yaml
  - `kubectl apply -f rbac.yaml`
- apply manifest.yaml
  - `kubectl apply -f manifest.yaml`
- helm and kubectl sources the same kubeconfig present in ~/.kube/config by default for registering resources. We copy this config from localhost
    to the pod in kopf's Dockerfile so that they both refer to same kind cluster
- NOTE: incase kind cluster is restarted, update the config file `kopf/config` with that present in `~/.kube/config` before recreating the kopf container

NOTE: delete cluster, gateway and then kopf. Else pod will never be deleted

NOTE: we cannot use hpa to manage coordinator scaling because:
1. Although scaling up is stateless, scale down should only be done by deleting coordinators WITHOUT any running queries
2. On detecting scale down, hpa will directly change deployment's 'spec.replica', allowing controller to drop last spawned pod(usually)
3. although kopf can intercept this change from hpa and deployment, it cannot stop/block the pod deletion by controller as the change would have already been implemented by the time kopf detects and intercepts the event
4. Even if kopf event handler reverts the replica count, it would only make a new pod spawn AFTER the pod deletion was initiated by controller, causing query failures in pod that was dropped
5. Hence, need to allow kopf itself to manage scaling by polling trino jmx metrics exposed to prometheus server


### Bring up prometheus server and grafana for scaling and monitoring
- Add prometheus helm repo
  - `helm repo add prometheus-community https://prometheus-community.github.io/helm-charts`
  - `helm repo upgrade`
  - `helm install prometheus prometheus-community/prometheus`
  - `kubectl port-forward service/prometheus-server 8080:80`
- Goto http://localhost:8080 and fire promQL queries
  - Refer `https://medium.com/@gayatripawar401/deploy-prometheus-and-grafana-on-kubernetes-using-helm-5aa9d4fbae66`


### Enable JMX in trino coordinators and make prometheus server scrape metrics 
- Enable jmx catalog, jmx server and jmx prometheus exporter as provided in trino-override.yaml
- Add prometheus scrape path/port as annotations in coordinator annotations for prometheus server to scrape from
  - Refer `https://github.com/prometheus-community/helm-charts/tree/main/charts/prometheus#scraping-pod-metrics-via-annotations`
- Install trino coordinator using
  - `helm install resil --values trino-override.yaml ../charts/charts/cluster-init`
- coordinator pod will have two containers with one being 'trino-coordinator' and the other 'jmx-exporter'
- Check logs of each to make sure both are working fine
  - `kubectl describe pod/<pod>`
  - `kubectl logs -f pod<pod> <container_name>`
- Port forward jmx-exporter container port to confirm that jmx metrics are exposed
  - `kubectl port-forward pod/<coordinator_pod> 5556:5556`
- Goto `http://localhost:5556`
- Get a metric and run promQL query in prometheus UI to confirm that metrics are scraped


### Useful kube commands
- kubectl component_types:
  - `pod`
  - `service`
  - `deployment`
  - `hpa`
  
- Get logs of a component
  - `kubectl logs <component_type/podname>`
  - `kubectl logs -f <component_type/podname>` -- tail logs
  - `kubectl logs -f <component_type/podname> <initContainer command name>` -- check init script logs
- Get component details
  - `kubectl describe <component_type> <component_name>` -- if pod has not started due to some reason
- Get component manifest
  - `kubectl get <component_type>/<component_name> -o yaml`
- Debug template issues
  - `helm install <name> --values <overriden_values_file> <helm_template_path> --debug`
  - `helm install <name> --values <overriden_values_file> <helm_template_path> --dry-run`
  


[//]: # (TODO)
1. Run a scheduled process from kopf to scrape running/started/queued query metric prometheus server for each cluster_instance by filtering on label
2. Scale up if threshold is met
3. Scale down if value is below threshold for a time(use aggregated metric, avg over 1m)
    - could use 'java_lang_Memory_HeapMemoryUsage_used'/java_lang_Memory_HeapMemoryUsage_max and 'trino_memory_MemoryPool_FreeBytes'
    - use metric to decide which cluster_instance to uninstall

