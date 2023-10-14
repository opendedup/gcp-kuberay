# GCP Ray Setup

## GKE Cluster Setup

```console
gcloud beta container --project "<project-name>" clusters create "gke-ray" --region "us-central1" --no-enable-basic-auth --cluster-version "1.27.4-gke.900" --release-channel "regular" --machine-type "e2-standard-4" --image-type "COS_CONTAINERD" --disk-type "pd-balanced" --disk-size "600" --metadata disable-legacy-endpoints=true --scopes "https://www.googleapis.com/auth/devstorage.read_only","https://www.googleapis.com/auth/logging.write","https://www.googleapis.com/auth/monitoring","https://www.googleapis.com/auth/servicecontrol","https://www.googleapis.com/auth/service.management.readonly","https://www.googleapis.com/auth/trace.append" --num-nodes "1" --logging=SYSTEM,WORKLOAD --monitoring=SYSTEM --enable-private-nodes --master-ipv4-cidr "172.16.0.0/28" --enable-ip-alias --network "projects/hazel-goal-319318/global/networks/ula" --subnetwork "projects/hazel-goal-319318/regions/us-central1/subnetworks/ula" --cluster-secondary-range-name "podcloud" --services-secondary-range-name "servicecloud" --no-enable-intra-node-visibility --default-max-pods-per-node "110" --enable-autoscaling --total-min-nodes "0" --total-max-nodes "3" --location-policy "BALANCED" --security-posture=standard --workload-vulnerability-scanning=disabled --enable-dataplane-v2 --no-enable-master-authorized-networks --addons HorizontalPodAutoscaling,HttpLoadBalancing,GcePersistentDiskCsiDriver --enable-autoupgrade --enable-autorepair --max-surge-upgrade 1 --max-unavailable-upgrade 0 --enable-managed-prometheus --workload-pool "<project-name>.svc.id.goog" --enable-shielded-nodes --node-locations "us-central1-a"

```

## GKE Cluster Credentials Setup
```console
gcloud container clusters get-credentials gke-ray --location=us-central1

```

## GKE Workload Identity Setup
Refer to the following page for more details on setting this up
https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity

```console
kubectl create namespace ray
kubectl create serviceaccount rayray-ksa \
    --namespace ray
gcloud iam service-accounts create rayraysa     --project=<project-name>
gcloud projects add-iam-policy-binding <project-name> \
    --member "serviceAccount:rayraysa@<project-name>.iam.gserviceaccount.com" \
    --role "roles/storage.admin"
gcloud iam service-accounts add-iam-policy-binding rayraysa@<project-name>.iam.gserviceaccount.com \
    --role roles/iam.workloadIdentityUser \
    --member "serviceAccount:<project-name>.svc.id.goog[ray/rayray-ksa]"
kubectl annotate serviceaccount rayray-ksa \
    --namespace ray \
    iam.gke.io/gcp-service-account=rayraysa@<project-name>.iam.gserviceaccount.com

```

## Add an L4 Large Node Pool using g2-standard-96 in us-central1-a

```console
gcloud container node-pools create raypool-gpu   --accelerator type=nvidia-l4,count=1,gpu-driver-version=latest   --machine-type g2-standard-96   --region=us-central1 --cluster gke-ray   --node-locations us-central1-a   --num-nodes 1   --enable-autoscaling    --min-nodes 0    --max-nodes 8 --disk-type "pd-balanced" --disk-size "1000" 
```

## (Alternative) Add an (NVIDIA A100 40GB) Large Node Pool using a2-highgpu-1g in europe-west4

```console
gcloud beta container node-pools create "raypool-a100" --cluster "gke-ray" --region "europe-west4" --machine-type "a2-highgpu-1g" --accelerator "type=nvidia-tesla-a100,count=1" --image-type "COS_CONTAINERD" --disk-type "pd-balanced" --disk-size "1000" --metadata disable-legacy-endpoints=true --scopes "https://www.googleapis.com/auth/devstorage.read_only","https://www.googleapis.com/auth/logging.write","https://www.googleapis.com/auth/monitoring","https://www.googleapis.com/auth/servicecontrol","https://www.googleapis.com/auth/service.management.readonly","https://www.googleapis.com/auth/trace.append" --num-nodes "1" --enable-autoscaling --total-min-nodes "0" --total-max-nodes "4" --location-policy "BALANCED" --enable-autoupgrade --enable-autorepair --max-surge-upgrade 1 --max-unavailable-upgrade 0 --ephemeral-storage-local-ssd count=1
```

## (Alternative) Add an (NVIDIA A100 40GB) Large Node Pool using a2-ultragpu-1g in us-central1-a

```console
gcloud beta container node-pools create "raypool-a100-80" --cluster "gke-ray" --region "us-central1" --machine-type "a2-ultragpu-1g" --accelerator "type=nvidia-a100-80gb,count=1,gpu-driver-version=default" --image-type "COS_CONTAINERD" --disk-type "pd-balanced" --disk-size "1000" --metadata disable-legacy-endpoints=true --scopes "https://www.googleapis.com/auth/devstorage.read_only","https://www.googleapis.com/auth/logging.write","https://www.googleapis.com/auth/monitoring","https://www.googleapis.com/auth/servicecontrol","https://www.googleapis.com/auth/service.management.readonly","https://www.googleapis.com/auth/trace.append" --num-nodes "1" --enable-autoscaling --total-min-nodes "0" --total-max-nodes "8" --location-policy "ANY" --enable-autoupgrade --enable-autorepair --max-surge-upgrade 1 --max-unavailable-upgrade 0 --ephemeral-storage-local-ssd count=1 --node-locations us-central1-a
```

## Enable Auto provisioning on the gke cluster
```console
gcloud container clusters update gke-ray     --enable-autoprovisioning     --max-cpu 500     --max-memory 3000     --min-accelerator type=nvidia-l4,count=0     --max-accelerator type=nvidia-l4,count=32 --region=us-central1
```
For A100-40's it would look like this
```console
gcloud container clusters update gke-ray     --enable-autoprovisioning     --max-cpu 500     --max-memory 3000     --min-accelerator type=nvidia-tesla-a100,count=0,gpu-driver-version=latest     --max-accelerator type=nvidia-tesla-a100,count=32,gpu-driver-version=latest --region=europe-west4
```

## Set ray as the default namespace
```console
kubectl config set-context --current --namespace=ray
```

### Example Run for classificatoin
```console
export HUGGING_FACE_HUB_TOKEN=<huggingface token>
export ANYSCALE_ARTIFACT_STORAGE=gs://<bucket_name>
python finetune_hf_llm.py --batch-size-per-device 1 --eval-batch-size-per-device 1 --num-devices 5 --grad_accum 2 --model_name <model-name> --model_revision <model-revision> --num-epochs 1 --ctx-len 4096 --dataset data/nflpp-classification-2016-2023-3.jsonl --output_dir /tmp/ray
```
