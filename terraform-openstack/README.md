# OpenStack K8s research cluster

Terraform configuration for a non-HA research cluster on UIT NetChallenge OpenStack.
The environment root is `environments/dev`; reusable resources live in `modules/`.

## What it creates

- Looks up the existing external `Public_Net`; it never creates a network, subnet, or router.
- Creates `k8s-cluster-sg` with SSH, Kubernetes API, NodePort, self-referencing Flannel VXLAN/ICMP, and managed IPv4 egress rules.
- Imports `~/.ssh/kltn_autoscaling.pub` as an OpenStack keypair.
- Creates `node-app`, `node-observability`, and `node-loadgen`, each booting from a new Cinder volume based on Ubuntu 22.04.
- Outputs the fixed IPv4 address of each node. `Public_Net` is external, so this configuration does not allocate floating IPs.

## Prerequisites

- Terraform 1.6 or later.
- OpenStack CLI and a valid `openstack-openrc.sh` file.
- An SSH public key at `~/.ssh/kltn_autoscaling.pub` (or override `public_key_path` in `terraform.tfvars`).

The provider is `terraform-provider-openstack/openstack` v3.x. Authentication comes only from the `OS_*` variables exported by the OpenRC file; credentials must not be committed in Terraform files or `terraform.tfvars`.

## Run

Use WSL or another Bash shell so that the OpenRC file can be sourced:

```bash
cd /mnt/d/School/HK7/thesis-gnn-rl-autoscaling/terraform-openstack
source openstack-openrc.sh
test -f ~/.ssh/kltn_autoscaling.pub

terraform -chdir=environments/dev init
terraform -chdir=environments/dev validate
terraform -chdir=environments/dev plan
terraform -chdir=environments/dev apply
```

Review the plan carefully before approving `apply`. To remove resources managed by this configuration:

```bash
terraform -chdir=environments/dev destroy
```

## Configuration

Current inventory defaults are in `environments/dev/variables.tf`. Copy `terraform.tfvars.example` to `terraform.tfvars` and override node flavor IDs, flavor names, volume sizes, image ID, or SSH key path when needed. Do not commit `terraform.tfvars`.

`terraform init` creates `environments/dev/.terraform.lock.hcl`. Commit that lock file so every user receives the same tested provider version.

## Module conventions

`environments/dev` owns the only `provider "openstack" {}` block. Child modules declare only `required_providers` and inherit the default root provider configuration; no module stores authentication settings. For a future multi-region deployment, define aliased provider configurations in the environment root and pass them to a module with its `providers` map.

Provider syntax and resource arguments follow the [OpenStack provider documentation](https://registry.terraform.io/providers/terraform-provider-openstack/openstack/latest/docs), including the documented boot-from-volume block, public-key import, and Neutron security-group rules.
