# 使用 Kubernetes 部署 LKMBot

> [!NOTE]
> **本仓库是 LKM 定制 fork，不再自带 k8s 清单**：原 `k8s/lkmbot/` 与 `k8s/lkmbot_with_napcat/`
> 已随部署配置收编而删除，避免与根编排仓库形成第二真相源（第二套清单必然与网关/配置分叉）。

## LKM 全栈（推荐）

LKM 环境的 Kubernetes 部署在根编排仓库 **LKM-Website** 的 `deploy/k8s/`（Kustomize），
与 compose 是同一套资产、同一批配置：

```bash
git clone https://github.com/LKM-AHZ/LKM-Website.git
cd LKM-Website
kubectl apply -f deploy/k8s/base/namespace.yaml
sh deploy/k8s/gen-secret.sh | kubectl apply -f -
sh deploy/k8s/gen-tls.sh    | kubectl apply -f -
kubectl kustomize deploy/k8s/overlays/prod --load-restrictor LoadRestrictionsNone | kubectl apply -f -
```

LKMBot 在其中是**可选组件**（compose 用 `--profile bot`，k8s 无 profile 机制，故用 0 副本表达
「默认不部署」），要启用：

```bash
kubectl -n lkm scale deploy/lkmbot --replicas=1
kubectl -n lkm logs -f deploy/lkmbot        # 首启日志里有面板初始密码或登录提示
```

需要知道的几点（细节见该仓 `DEPLOYMENT.md`「LKM Bot」与 `deploy/k8s/README.md`）：

- **面板不发布宿主端口**，经 APISIX 网关以 `bot.<社群域名>` 暴露；镜像 `lkm-bot:latest`
  需自行构建并让集群可拉取（kind 用 `deploy/k8s/overlays/kind/setup.sh` 注入）。
  证书按 `gen-tls.sh` 的键名导入（`bot.<域名>_fullchain.pem` / `_privkey.pem`）。
- **数据走 PVC**，Deployment 用 `strategy: Recreate`：SQLite 单写者 + RWO 卷不能被两个 Pod 同时挂载。
- **代码沙箱（Shipyard）不在集群内自托管**：Bay 依赖 Docker Engine API 在宿主 spawn 兄弟容器、
  并把宿主路径 bind 进沙箱，与 k8s 的调度/网络模型不兼容（节点多为 containerd，没有 docker.sock）。
  需要沙箱时，在面板「配置 → 沙箱」把 endpoint 指向**集群外**的 Bay。
- **NapCat** 建议作为独立工作负载部署，与本 bot 同 namespace，连 `ws://lkmbot:6199/ws`；
  或在面板里配置对应平台的连接方式。

## 上游通用示例

若你并非在 LKM 环境中部署（不使用根编排仓库），上游 AstrBot 仓库仍保留 `k8s/` 清单与配套说明，
可按上游 `docs/*/deploy/astrbot/kubernetes.md` 操作——注意自行把清单里的命名与镜像对齐到本 fork
（`lkmbot` / `LKMBot` 路径）。
