# Deploy LKMBot with Kubernetes

> [!NOTE]
> **This repository is the LKM fork and no longer ships k8s manifests**: the former `k8s/lkmbot/`
> and `k8s/lkmbot_with_napcat/` directories were removed when deployment was consolidated into the
> root orchestration repository — a second set of manifests would inevitably drift from the gateway
> and configuration it has to stay aligned with.

## LKM Stack (Recommended)

Kubernetes deployment for the LKM environment lives in the root orchestration repository
**LKM-Website** under `deploy/k8s/` (Kustomize); it reuses the same assets and configuration as compose:

```bash
git clone https://github.com/LKM-AHZ/LKM-Website.git
cd LKM-Website
kubectl apply -f deploy/k8s/base/namespace.yaml
sh deploy/k8s/gen-secret.sh | kubectl apply -f -
sh deploy/k8s/gen-tls.sh    | kubectl apply -f -
kubectl kustomize deploy/k8s/overlays/prod --load-restrictor LoadRestrictionsNone | kubectl apply -f -
```

LKMBot is an **optional component** there (compose expresses that with `--profile bot`; k8s has no
profile mechanism, so 0 replicas means "not deployed by default"). To enable it:

```bash
kubectl -n lkm scale deploy/lkmbot --replicas=1
kubectl -n lkm logs -f deploy/lkmbot        # the initial dashboard password / login hint is in the startup log
```

Things worth knowing (details in that repo's `DEPLOYMENT.md` "LKM Bot" section and `deploy/k8s/README.md`):

- **The dashboard publishes no host port**; it is exposed by the APISIX gateway as `bot.<community-domain>`.
  The `lkm-bot:latest` image must be built and made pullable by the cluster (for kind, use
  `deploy/k8s/overlays/kind/setup.sh`). Import the certificate using `gen-tls.sh`'s key names
  (`bot.<domain>_fullchain.pem` / `_privkey.pem`).
- **Data lives on a PVC**, and the Deployment uses `strategy: Recreate`: SQLite has a single writer and
  an RWO volume cannot be mounted by two Pods at once.
- **The code sandbox (Shipyard) is not self-hosted in-cluster**: Bay spawns sibling containers through
  the Docker Engine API and bind-mounts host paths, which does not fit Kubernetes' scheduling/network
  model (nodes usually run containerd and have no docker.sock). If you need the sandbox, point its
  endpoint at a Bay running **outside** the cluster (dashboard → Configuration → Sandbox).
- **NapCat** is best deployed as its own workload in the same namespace, connecting to
  `ws://lkmbot:6199/ws`; alternatively configure the platform connection in the dashboard.

## Upstream Generic Example

If you are not deploying in the LKM environment (i.e. without the root orchestration repo), the upstream
AstrBot repository still keeps its `k8s/` manifests and the matching instructions — follow
`docs/*/deploy/astrbot/kubernetes.md` there, aligning names and images with this fork
(`lkmbot` / the `/LKMBot` path).
