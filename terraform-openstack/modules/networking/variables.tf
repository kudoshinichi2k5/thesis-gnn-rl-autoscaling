variable "external_network_name" {
  description = "Name of the provider-managed external network used for egress and floating IPs."
  type        = string
}

variable "external_network_id" {
  description = "Expected ID of the provider-managed external network."
  type        = string
}

variable "private_network_name" {
  description = "Name for the project-private network."
  type        = string
}

variable "private_subnet_name" {
  description = "Name for the project-private IPv4 subnet."
  type        = string
}

variable "private_subnet_cidr" {
  description = "IPv4 CIDR allocated to the project-private subnet."
  type        = string
}

variable "router_name" {
  description = "Name for the router connecting the private subnet to the external network."
  type        = string
}

variable "dns_nameservers" {
  description = "DNS resolvers configured through DHCP on the private subnet."
  type        = list(string)
}
