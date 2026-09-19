variable "nodes" {
  description = "K3s cluster nodes."
  type = list(object({
    name         = string
    flavor       = string
    disk_size_gb = number
    role         = string
  }))
}

variable "network_id" {
  description = "Network ID used by the cluster nodes."
  type        = string
}

variable "security_group_name" {
  description = "Security group attached to the cluster nodes."
  type        = string
}

variable "key_pair" {
  description = "Existing OpenStack keypair name."
  type        = string
}

variable "image_id" {
  description = "Ubuntu 22.04 image ID used for boot volumes."
  type        = string
}

variable "assign_floating_ip" {
  description = "Whether to assign floating IPs to instances."
  type        = bool
}