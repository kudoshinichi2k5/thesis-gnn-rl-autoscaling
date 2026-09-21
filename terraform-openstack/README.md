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

### Create the SSH keypair

The keypair module imports an existing **public** key into OpenStack. Run this once, as the same Linux user that will run Terraform:

```bash
mkdir -p ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/kltn_autoscaling -C "kltn-autoscaling"
chmod 600 ~/.ssh/kltn_autoscaling
chmod 644 ~/.ssh/kltn_autoscaling.pub
```

This produces a private key (`~/.ssh/kltn_autoscaling`, keep it secret) and the public key Terraform imports (`~/.ssh/kltn_autoscaling.pub`). Do not commit either file. If you use an existing public key instead, change only `public_key_path` in the ignored `terraform.tfvars` file.

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

If you execute Terraform as `root`, `~` resolves to `/root`, so create the key at `/root/.ssh/kltn_autoscaling.pub` or set `public_key_path` to the actual public-key path. Prefer running Terraform as your normal WSL user.

Review the plan carefully before approving `apply`. To remove resources managed by this configuration:

```bash
terraform -chdir=environments/dev destroy
```

## Configuration

`environments/dev/variables.tf` declares only input names, types, and descriptions. The full environment inventory is in `terraform.tfvars`: network, image, keypair, flavor IDs/names, volume sizes, and nodes. Copy `terraform.tfvars.example` to `terraform.tfvars` for a new environment and edit its values. Do not commit `terraform.tfvars`.

`terraform init` creates `environments/dev/.terraform.lock.hcl`. Commit that lock file so every user receives the same tested provider version.

## Module conventions

`environments/dev` owns the only `provider "openstack" {}` block. Child modules declare only `required_providers` and inherit the default root provider configuration; no module stores authentication settings. For a future multi-region deployment, define aliased provider configurations in the environment root and pass them to a module with its `providers` map.

Provider syntax and resource arguments follow the [OpenStack provider documentation](https://registry.terraform.io/providers/terraform-provider-openstack/openstack/latest/docs), including the documented boot-from-volume block, public-key import, and Neutron security-group rules.
