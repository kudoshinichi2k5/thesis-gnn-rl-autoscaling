variable "external_network_name" {
  description = "Name of the external network from which floating IPs are allocated."
  type        = string
}

variable "node_ports" {
  description = "Managed Neutron ports keyed by node name."
  type = map(object({
    port_id = string
  }))
}
