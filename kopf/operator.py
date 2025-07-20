import threading
from random import randint

import kopf
import kubernetes
import logging as logger
import subprocess
import requests
from requests.auth import HTTPBasicAuth

import asyncio

lock = threading.Lock()

logging_task = None

clusters = {}

def get_new_cluster_instance(pod_labels):
    cluster_group = pod_labels["cluster-group"]

    if clusters.get(cluster_group) is None:
        clusters[cluster_group] = []

    while True:
        cluster_id = randint(1, 100)
        new_cluster = f"{cluster_group}-{cluster_id}"
        if not clusters.get(cluster_group).__contains__(new_cluster):
            break

    return new_cluster

@kopf.on.startup()
def discover_running_clusters(**kwargs):
    from kubernetes import config
    config.load_incluster_config()

    api = kubernetes.client.CoreV1Api()
    logger.info("Starting cluster discovery!")
    pods = api.list_namespaced_pod(namespace='default', label_selector='app.kubernetes.io/component=coordinator')
    for pod in pods.items:
        labels = pod.metadata.labels
        cluster_group = labels['cluster-group']
        cluster_instance = labels['cluster-instance']
        if cluster_group is not None and cluster_instance is not None:
            with lock:
                if clusters.get(cluster_group) is None:
                    clusters[cluster_group] = [cluster_instance]
                elif not clusters.get(cluster_group).__contains__(cluster_instance):
                    clusters.get(cluster_group).append(cluster_instance)

    logger.info("Cluster discovery complete!")

@kopf.on.startup()
async def run_background_timer(**kwargs):
    global logging_task
    loop = asyncio.get_running_loop()
    logging_task = loop.create_task(background_loop())

async def background_loop():
    while True:
        logger.info(f"Active clusters: {clusters}")
        await asyncio.sleep(10)

@kopf.on.cleanup()
def on_cleanup(**_):
    global logging_task
    if logging_task:
        logger.info("Shutting down background logging_task")
        logging_task.cancel()  # Graceful shutdown


@kopf.on.create(version='v1', kind='Pod', labels={'app.kubernetes.io/component': 'coordinator'})
def install_cluster_components(spec, meta, namespace, **kwargs):
    labels = meta.get('labels', {})
    cluster_group = labels["cluster-group"]

    name = meta['name']
    api = kubernetes.client.CoreV1Api()

    cluster_instance = get_new_cluster_instance(labels)
    label_patch = {
        "metadata": {
            "labels": {
                "cluster-instance": cluster_instance
            }
        }
    }

    logger.info(f"Assigning coordinator pod {name} to cluster instance '{cluster_instance}'")
    api.patch_namespaced_pod(name=name, namespace=namespace, body=label_patch)

    # create service coordinator, deployment worker, service worker and worker autoscaler with clusterInstance value set
    helm_cmd = [
        "helm", "install", f"{cluster_instance}",
        "--values", "/app/worker-override.yaml",
        "/worker-helm-dir/chart",
        "--namespace", "default",
        "--set", f"clusterInstance={cluster_instance}",
        "--set", f"coordinator.host={cluster_instance}-coordinator"
    ]

    logger.info(f"Installing components of coordinator {name} in cluster '{cluster_instance}'")
    try:
        logger.info(f"Running Helm command: {' '.join(helm_cmd)}")
        result = subprocess.run(helm_cmd, check=True, capture_output=True, text=True)
        logger.info(f"Helm output: {result.stdout}")

        with lock:
            clusters.get(cluster_group).append(cluster_instance)

    except subprocess.CalledProcessError as e:
        logger.error(f"Helm install failed: {e.stderr}")
        raise kopf.TemporaryError("Failed to install Helm chart", delay=30)



@kopf.on.create(version='v1', kind='Service', labels={'app.kubernetes.io/component': 'coordinator'})
def register_in_gateway(spec, meta, namespace, **kwargs):

    labels = meta.get('labels', {})
    cluster_group = labels["cluster-group"]
    cluster_instance = labels["cluster-instance"]
    service_name = meta['name']
    service_port = spec['ports'][0]['port']

    logger.info(f"Registering cluster '{cluster_instance}' in gateway")
    try:
        resp = requests.post(
            url="http://trino-gateway:8080/entity?entityType=GATEWAY_BACKEND",
            json={
                "name": cluster_instance,
                "proxyTo": f'http://{service_name}:{service_port}',
                "active": "true",
                "routingGroup": f'{cluster_instance}'
            }
        )
        logger.info(f"Response code: {resp.status_code}; Body: {resp.text}")
    except Exception as e:
        logger.error(f"service registration failed: {e}")
        raise kopf.TemporaryError(f"Failed to register against gateway for '{cluster_instance}'", delay=30)

    logger.info(f"Updating gateway rule to route traffic to cluster '{cluster_instance}'")
    resp = requests.post(
        url="http://trino-gateway:8080/webapp/updateRoutingRules",
        json={
            "name": f"route_to_{cluster_group}",
            "description": f'Rule to route traffic to active instance of {cluster_group}',
            "priority": 1,
            "condition": f'request.getHeader("X-Trino-Client-Tags") contains "cg:{cluster_group}"',
            "actions": [f'result.put("routingGroup", "{cluster_instance}")']
        }
    )
    try:
        logger.info(f"Response code: {resp.status_code}; Body: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to update routing rules for cluster group {cluster_group}: {e}")
        raise kopf.TemporaryError(f"Failed to failed to update routing rules for cluster-group '{cluster_group}'", delay=30)

@kopf.on.delete(version='v1', kind='Pod', labels={'app.kubernetes.io/component': 'coordinator'})
def delete_cluster_components(spec, meta, namespace, **kwargs):
    labels = meta.get('labels', {})
    cluster_group = labels["cluster-group"]
    cluster_instance = labels["cluster-instance"]

    uninstall_cluster(cluster_group, cluster_instance)


def uninstall_cluster(cluster_group, cluster_instance):
    helm_cmd = [
        "helm", "uninstall", f"{cluster_instance}"
    ]
    logger.warning(f"Un-installing components of cluster {cluster_instance}")
    try:
        logger.info(f"Running Helm command: {' '.join(helm_cmd)}")
        result = subprocess.run(helm_cmd, check=True, capture_output=True, text=True)
        logger.info(f"Helm output: {result.stdout}")
        try:
            with lock:
                clusters.get(cluster_group).remove(cluster_instance)
        except Exception as e:
            logger.error(f"Error removing cluster instance {cluster_instance} from clusters: {e.__str__()}")
        return "success"

    except subprocess.CalledProcessError as e:
        logger.error(f"Helm command failed: {e.stderr}")
        if str(e.stderr).__contains__("Release not loaded"):
            logger.warning("Ignoring error as release might have been deleted manually!")
        else:
            raise kopf.TemporaryError("Failed to run Helm command", delay=30)

# @kopf.on.field(kind="HorizontalPodAutoscaler", version='v1', labels={'app.kubernetes.io/component': 'coordinator-hpa'}, field='status.desiredReplicas')
# def control_hpa_scaling(old, new, spec, labels, name, namespace, **kwargs):
#     api = kubernetes.client.AutoscalingV1Api()
#     if old is not None and new < old:
#         actual_min_replicas = spec["minReplicas"]
#         logger.info(f"current min replica: {actual_min_replicas}")
#         # revert scale down event to let kopf perform intelligent cluster uninstall
#         logger.info(f"Scale down detected for {name}: {old} → {new}")
#         # Patch HPA spec to bump minReplicas
#         logger.info(f"Blocking hpa's scale down by setting minReplica as {old} to allow intelligent scale down from kopf!")
#         api.patch_namespaced_horizontal_pod_autoscaler(
#             name=name,
#             namespace=namespace,
#             body={"spec": {"minReplicas": old}})
#
#         # Optionally: remove the least-used pod manually
#         # get the cluster group whose deployment got scaled down
#         cluster_group = labels.get('cluster-group', {})
#         logger.info(f"Performing intelligent scale down for cluster-group {cluster_group}")
#
#         # get number of pods to scale down
#         pods_to_scale_down = old - new
#
#         # iterate through list of cluster-instances under cluster group and find one that has no queries running. Perform helm uninstall for that instance
#         for cluster_instance in clusters[cluster_group]:
#             coordinator_url = f"{cluster_instance}-coordinator:8090"
#             resp = requests.get(url=f"http://{coordinator_url}/ui/api/stats", auth=HTTPBasicAuth("admin", ""))
#             logger.info(f"stats of cluster {cluster_instance}: {resp.text}")
#             # "runningQueries":0,"blockedQueries":0,"queuedQueries":0,"activeCoordinators":1,"activeWorkers":1,"runningDrivers":0,"totalAvailableProcessors":8,"reservedMemory":0.0,"totalInputRows":0,"totalInputBytes":0,"totalCpuTimeSecs":0}
#             if resp.status_code == 200:
#                 resp = resp.json()
#                 if resp["runningQueries"] != 0:
#                     continue
#                 uninstall_cluster(cluster_group, cluster_instance)
#                 pods_to_scale_down = pods_to_scale_down - 1
#
#             # perform till required pods are removed
#             if pods_to_scale_down <= 0:
#                 break
#
#         # if not enough instances are available to scale down, patch deployment with replica count that matches active instances and wait for 1 minute for hpa to trigger again
#         logger.info(f"Finishing scale down handle by setting hpa of {name} with minReplicas: {len(clusters.get(cluster_group))}")
#         api.patch_namespaced_horizontal_pod_autoscaler(
#             name=name,
#             namespace=namespace,
#             body={"spec": {"minReplicas": len(clusters.get(cluster_group))}})

# @kopf.on.field('apps', 'v1', 'deployments', labels={'app.kubernetes.io/component': 'coordinator'}, field='spec.replicas')
# def on_deployment_scaled_down(old, new, name, labels, namespace, **kwargs):
#
#     api = kubernetes.client.AppsV1Api()
#     if old is not None and new < old:
#         # revert scale down event to let kopf perform intelligent cluster uninstall
#         logger.info(f"Scale down detected for {name}: {old} → {new}")
#
#         logger.info(f"Blocking hpa's scale down event to allow intelligent scale down from kopf!")
#         replicas_patch = {
#             "spec": {
#                 "replicas": old
#             }
#         }
#         api.patch_namespaced_deployment(name=name, namespace=namespace, body=replicas_patch)
#
#         # Optionally: remove the least-used pod manually
#         # get the cluster group whose deployment got scaled down
#         cluster_group = labels.get('cluster-group', {})
#         logger.info(f"Performing intelligent scale down for cluster-group {cluster_group}")
#
#         # get number of pods to scale down
#         pods_to_scale_down = old - new
#
#         # iterate through list of cluster-instances under cluster group and find one that has no queries running. Perform helm uninstall for that instance
#         for cluster_instance in clusters[cluster_group]:
#             coordinator_url = f"{cluster_instance}-coordinator:8090"
#             resp = requests.get(url=f"http://{coordinator_url}/ui/api/stats", auth=HTTPBasicAuth("admin", ""))
#             logger.info(f"stats of cluster {cluster_instance}: {resp.text}")
#             # "runningQueries":0,"blockedQueries":0,"queuedQueries":0,"activeCoordinators":1,"activeWorkers":1,"runningDrivers":0,"totalAvailableProcessors":8,"reservedMemory":0.0,"totalInputRows":0,"totalInputBytes":0,"totalCpuTimeSecs":0}
#             if resp.status_code == 200:
#                 resp = resp.json()
#                 if resp["runningQueries"] != 0:
#                     continue
#                 uninstall_cluster(cluster_group, cluster_instance)
#                 pods_to_scale_down = pods_to_scale_down - 1
#
#             # perform till required pods are removed
#             if pods_to_scale_down <= 0:
#                 break
#
#         # if not enough instances are available to scale down, patch deployment with replica count that matches active instances and wait for 1 minute for hpa to trigger again
#         current_replica_count = len(clusters.get(cluster_group))
#         replicas_patch = {
#             "spec": {
#                 "replicas": current_replica_count
#             }
#         }
#
#         logger.info(f"Patching replica count of deployment {name} to '{current_replica_count}'")
#         api.patch_namespaced_deployment(name=name, namespace=namespace, body=replicas_patch)