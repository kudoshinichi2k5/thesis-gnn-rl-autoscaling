variable "nodes" {
  description = "Nodes and their flavor/boot-volume specifications."
  type = list(object({
    name           = string
    role           = string
    flavor_id      = string
    flavor_name    = string
    volume_size_gb = number
  }))
}

variable "network_id" {
  description = "ID of the existing network to attach to each instance."
  type        = string
}

variable "security_group_name" {
  description = "Security group name attached to each instance."
  type        = string
}

variable "keypair_name" {
  description = "Existing OpenStack keypair name attached to each instance."
  type        = string
}

variable "image_id" {
  description = "Glance image ID used to create each boot volume."
  type        = string
}
