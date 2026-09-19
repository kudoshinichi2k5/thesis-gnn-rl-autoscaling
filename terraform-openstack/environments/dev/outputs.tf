output "k3s_nodes" {
  description = "K3s nodes with IP addresses and roles."
  value       = module.compute.nodes
}