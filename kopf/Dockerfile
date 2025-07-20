FROM python:3.11-slim
WORKDIR /app
RUN pip install kopf kubernetes glib uvicorn requests fastapi
#COPY ./operator.py /app
COPY ./config /root/.kube/
RUN apt-get update && \
	apt-get -y install curl tar

RUN curl -fsSLO "https://get.helm.sh/helm-v3.17.1-linux-amd64.tar.gz" \
    && tar -zxvf "helm-v3.17.1-linux-amd64.tar.gz" \
    && mv "linux-amd64/helm" /usr/local/bin/helm \
    && rm -rf "helm-v3.17.1-linux-amd64.tar.gz" "linux-amd64"

CMD ["kopf", "run", "--namespace", "default", "--verbose", "/app/operator.py"]
