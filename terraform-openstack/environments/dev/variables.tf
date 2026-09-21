variable "network_name" {
  description = "Existing external OpenStack network name."
  type        = string
}

variable "network_id" {
  description = "Expected ID of the existing Public_Net network."
  type        = string
}

variable "security_group_name" {
  description = "Name for the shared Kubernetes security group."
  type        = string
}

variable "keypair_name" {
  description = "OpenStack keypair name to import."
  type        = string
}

variable "public_key_path" {
  description = "Path to the SSH public key imported into OpenStack."
  type        = string
}

variable "image_id" {
  description = "Ubuntu 22.04 Glance image ID used to create boot volumes."
  type        = string
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
}
