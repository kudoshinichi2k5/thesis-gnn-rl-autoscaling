variable "external_network_name" {
  description = "Existing provider-managed external OpenStack network name."
  type        = string
}

variable "external_network_id" {
  description = "Expected ID of the existing external network."
  type        = string
}

variable "private_network_name" {
  description = "Name for the project-private network."
  type        = string
}

variable "private_subnet_name" {
  description = "Name for the project-private subnet."
  type        = string
}

variable "private_subnet_cidr" {
  description = "IPv4 CIDR for the project-private subnet. It must not overlap existing networks."
  type        = string
}

variable "router_name" {
  description = "Name for the private-to-external router."
  type        = string
}

variable "dns_nameservers" {
  description = "DNS resolvers assigned to nodes through the private subnet DHCP service."
  type        = list(string)
}

variable "security_group" {
  description = "Security-group definition and its Neutron rules for this environment."
  type = object({
    name                 = string
    description          = string
    delete_default_rules = bool
    rules = list(object({
      name             = string
      direction        = string
      ethertype        = string
      protocol         = optional(string)
      port_range_min   = optional(number)
      port_range_max   = optional(number)
      remote_ip_prefix = optional(string)
      remote_group     = bool
    }))
  })
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
