variable "network_name" {
  description = "Existing external OpenStack network name."
  type        = string
  default     = "Public_Net"
}

variable "network_id" {
  description = "Expected ID of the existing Public_Net network."
  type        = string
  default     = "c3455e8f-ea16-4f5d-ad5e-5c4292015a0d"
}

variable "security_group_name" {
  description = "Name for the shared Kubernetes security group."
  type        = string
  default     = "k8s-cluster-sg"
}

variable "keypair_name" {
  description = "OpenStack keypair name to import."
  type        = string
  default     = "kltn-autoscaling"
}

variable "public_key_path" {
  description = "Path to the SSH public key imported into OpenStack."
  type        = string
  default     = "~/.ssh/kltn_autoscaling.pub"
}

variable "image_id" {
  description = "Ubuntu 22.04 Glance image ID used to create boot volumes."
  type        = string
  default     = "a04bdcd9-40e7-40ba-bd21-7d410965e3a3"
}

variable "nodes" {
  description = "Non-HA Kubernetes research-cluster nodes and their Cinder boot volumes."
  type = list(object({
    name           = string
    role           = string
    flavor_id      = string
    flavor_name    = string
    volume_size_gb = number
  }))

  default = [
    { name = "node-app", role = "app", flavor_id = "00-0000-0008-04", flavor_name = "gp.large-4c", volume_size_gb = 120 },
    { name = "node-observability", role = "observability", flavor_id = "00-0000-0002-01", flavor_name = "gp.small-1c", volume_size_gb = 50 },
    { name = "node-loadgen", role = "loadgen", flavor_id = "00-0000-0002-01", flavor_name = "gp.small-1c", volume_size_gb = 30 },
  ]
}
