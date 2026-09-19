variable "security_group_name" {
  description = "Security group name."
  type        = string
}

variable "network_cidr" {
  description = "CIDR used for internal Kubernetes traffic."
  type        = string
}