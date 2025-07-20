#!/bin/bash
set -x

PROJECT_HOME=$(pwd)

echo -e "\nStarting docker\n"
sudo systemctl start docker
sudo systemctl status docker | cat

#---
echo -e "\nStarting kind cluster test\n"
kind create cluster -n test
kind get clusters

#---
echo -e "\nAdd charts to helm\n"
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
helm repo add hatrino https://prabodhtr.github.io/charts/
helm repo list
helm repo update
helm search repo hatrino

#---
echo -e "\nInstall metric-server for exposing metrics\n"
helm upgrade --install --set args={--kubelet-insecure-tls} metrics-server metrics-server/metrics-server --namespace kube-system
kubectl get all --namespace kube-system

#---
echo -e "\nInstall kopf service\n"
cd kopf || exit

TRINO_WORKER_CHART_DIR="/home/prabodh/projects/charts/charts/trino/workers"

kubectl delete configmap kopf-operator-code
kubectl create configmap kopf-operator-code --from-file=operator.py

kubectl delete configmap worker-value-override
kubectl create configmap worker-value-override --from-file=../trino/worker-override.yaml

kubectl delete configmap worker-helm-template
kubectl create configmap worker-helm-template --from-file=${TRINO_WORKER_CHART_DIR}/templates

kubectl delete configmap worker-helm-chart
kubectl create configmap worker-helm-chart --from-file=${TRINO_WORKER_CHART_DIR}/

helm install kopf hatrino/trino-kopf --values kopf-override.yaml
cd "$PROJECT_HOME" || exit

#---
echo -e "\nStarting postgresDB for gateway\n"
cd ./gateway/backend-db-configs || exit
docker-compose up -d --remove-orphans
docker ps
cd "$PROJECT_HOME" || exit

#---
echo -e "\nInstall trino gateway\n"

cd ./gateway || exit
kubectl delete configmap routing-rules-config
kubectl create configmap routing-rules-config --from-file=routing-rules.yml

helm install tg hatrino/trino-gateway --values gateway-override.yaml

echo -e "\n waiting to initialize gateway service\n"
while true; do
  GATEWAY_TARGET_IP=$(kubectl describe service/trino-gateway | grep Endpoints: | cut -d":" -f2 | xargs)
  if [[ -n ${GATEWAY_TARGET_IP} ]]; then
    echo -e "\nGateway service initialized to ${GATEWAY_TARGET_IP}"
    break
  fi
  sleep 5s
done

rm -f gateway-port-forward.log
rm -f /tmp/tg-port-forward.pid
bash -c 'kubectl port-forward service/trino-gateway 8080:8080 &> port-forward.log' & jobs
echo $! > /tmp/tg-port-forward.pid

cd "$PROJECT_HOME" || exit

#---
echo -e "\nInstall trino cluster 'demo'\n"
cd ./trino || exit

helm install demo hatrino/trino-core --values core-override.yaml

cd "$PROJECT_HOME" || exit
