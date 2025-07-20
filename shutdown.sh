#!/bin/bash
set -x

pkill -P `cat "/tmp/tg-port-forward.pid"`
helm delete tg
docker stop postgresDB
docker rm postgresDB
docker volume rm backend-db-configs_postgres-data

helm delete metrics-server --namespace kube-system

helm delete demo


sleep 10s
helm delete kopf
