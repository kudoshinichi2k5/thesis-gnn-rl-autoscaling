variable "security_group_name" {
  description = "Security group name."
  type        = string
}

variable "network_cidr" {
  description = "CIDR of the existing OpenStack network."
  type        = string
}