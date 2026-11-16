# phpa: Predictive Horizontal Pod Autoscaler (to build)

**Status: not implemented.** The runner refuses to run it until this folder has a
`manifests/` folder (or `apply.sh` / `start.sh`). Read the contract in
[controllers/README.md](../README.md) first.

PHPA needs its operator installed in the cluster, which the setup script does **not** do.
Suggested layout:

```
controllers/phpa/
├── README.md
├── apply.sh        # install the PHPA operator (e.g. helm upgrade --install ...; pin the version)
├── remove.sh       # uninstall it; must succeed when it isn't installed
└── manifests/
    └── phpa.yaml   # the PredictiveHorizontalPodAutoscaler for scale-app (namespace scale, 1-10 replicas)
```

`apply.sh` runs before every PHPA run and `remove.sh` before *every* run of *any* controller, so
both must be safe to repeat. Installing an operator takes time, so `apply.sh` should skip the install
when it's already there. Pass `--kube-context "$KUBE_CONTEXT"` to helm and `--context "$KUBE_CONTEXT"`
to kubectl.

Note for `remove.sh`: removing the operator but leaving its CRD means `kubectl delete -f manifests/`
still works. Removing the CRD first makes that delete fail. Order it so cleanup always succeeds.

Write down here: the model (linear regression, Holt-Winters, ...), its history length, sync period,
CPU target, and anything else that affects decisions.
