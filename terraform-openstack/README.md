# OpenStack K8s research cluster

Terraform configuration for a non-HA research cluster on UIT NetChallenge OpenStack.
The environment root is `environments/dev`; reusable resources live in `modules/`.

## What it creates

- Looks up the existing external `Public_Net` for router egress and floating-IP allocation.
- Creates a project-private network, IPv4 subnet, and router. Nodes attach only to the private network.
- Creates `k8s-cluster-sg` with SSH, Kubernetes API, NodePort, self-referencing Flannel VXLAN/ICMP, and managed IPv4 egress rules. Each node receives an explicitly managed Neutron port with this group.
- Imports `~/.ssh/kltn_autoscaling.pub` as an OpenStack keypair.
- Creates `node-app`, `node-observability`, and `node-loadgen`, each booting from a new Cinder volume based on Ubuntu 22.04.
- Allocates and associates one floating IP per node, then outputs both each node's private fixed IPv4 and public floating IPv4 address.

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

### WSL or Bash

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

### Windows PowerShell

PowerShell cannot source `openstack-openrc.sh`. Export the same `OS_*` values from that file into the current PowerShell session (or configure `OS_CLOUD` and a local `clouds.yaml`) before Terraform commands. Do not put credentials in `terraform.tfvars` or commit them.

The key path must exist in the operating system running Terraform. For PowerShell, create a Windows keypair if needed and set the ignored `terraform.tfvars` value to its Windows path:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\kltn_autoscaling" -C "kltn-autoscaling"
# In terraform.tfvars:
# public_key_path = "C:/Users/<your-Windows-user>/.ssh/kltn_autoscaling.pub"
terraform -chdir=environments/dev init
terraform -chdir=environments/dev validate
terraform -chdir=environments/dev plan
```

### Floating-IP prefix workaround

`Public_Net` currently has an infrastructure issue: addresses in `192.168.121.x` can be allocated but are not reachable, while `192.168.120.x` addresses work. The OpenStack API cannot request an arbitrary *available* address within only part of a subnet. To avoid hardcoding addresses that may already be allocated, use the Bash wrapper below from WSL after authentication is available in the shell. It applies the configuration, replaces only floating IPs outside the required prefix, and stops after a bounded number of attempts per node.

```bash
bash scripts/replace-floating-ips.sh --prefix 192.168.120. --max-attempts 10
```

Run it from `terraform-openstack`. The script requires `jq`; install it in WSL with `sudo apt install jq` if necessary. Use `--skip-initial-apply` when the infrastructure has already been applied and only Floating IPs need correction. This is a temporary operational workaround; the durable fix is an external-network allocation pool or routing correction by the cloud administrator.

Review the plan carefully before approving `apply`. To remove resources managed by this configuration:

```bash
terraform -chdir=environments/dev destroy
```

## Configuration

`environments/dev/variables.tf` declares only input names, types, and descriptions. The full environment inventory is in `terraform.tfvars`: external-network identity, private-network/subnet/router names and CIDR, image, keypair, flavor IDs/names, volume sizes, and nodes. Copy `terraform.tfvars.example` to `terraform.tfvars` for a new environment and edit its values. Do not commit `terraform.tfvars`.

The private subnet CIDR must not overlap with another network that the project can route to. `10.42.0.0/24` is a placeholder: confirm it is unused before applying. The external network must be visible to the project and permit router gateway and floating-IP allocation; these are OpenStack policy requirements that Terraform cannot bypass.

`terraform init` creates `environments/dev/.terraform.lock.hcl`. Commit that lock file so every user receives the same tested provider version.

## Module conventions

`environments/dev` owns the only `provider "openstack" {}` block. Child modules (`networking`, `security-group`, `keypair`, `compute`, and `floating-ip`) declare only `required_providers` and inherit the default root provider configuration; no module stores authentication settings. For a future multi-region deployment, define aliased provider configurations in the environment root and pass them to a module with its `providers` map.

Provider syntax and resource arguments follow the [OpenStack provider documentation](https://registry.terraform.io/providers/terraform-provider-openstack/openstack/latest/docs), including boot-from-volume, public-key import, Neutron router/floating-IP resources, and security-group rules.
