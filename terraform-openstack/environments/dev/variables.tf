variable "network_name" {
  description = "Existing OpenStack network name."
  type        = string
}

variable "network_id" {
  description = "Existing OpenStack network ID."
  type        = string
}

variable "network_cidr" {
  description = "CIDR of the existing OpenStack network."
  type        = string
}

variable "key_pair" {
  description = "Existing OpenStack keypair name."
  type        = string
}

variable "image_id" {
  description = "Ubuntu 22.04 image ID."
  type        = string
}

variable "security_group_name" {
  description = "Kubernetes security group name."
  type        = string
}

variable "nodes" {
  description = "K3s cluster nodes."
  type = list(object({
    name         = string
    flavor       = string
    disk_size_gb = number
    role         = string
  }))
}

variable "assign_floating_ip" {
  description = "Whether to assign floating IPs."
  type        = bool
  default     = false
}