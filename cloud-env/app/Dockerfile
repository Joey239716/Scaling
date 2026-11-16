# The synthetic service. scripts/setup_cluster.sh builds it inside Minikube as
# scale-app:v1. If you change the app, bump the tag here AND in
# cloud-env/k8s/deployment.yaml and scripts/setup_cluster.sh, so results made
# with different app versions can't be mixed up.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

EXPOSE 8000
# One process per pod: Kubernetes scales by adding pods, not processes.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
