variable "security_group" {
  description = "Security-group definition and its Neutron rules."
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
