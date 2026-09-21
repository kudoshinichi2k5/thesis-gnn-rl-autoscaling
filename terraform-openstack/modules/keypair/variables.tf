variable "keypair_name" {
  description = "Name of the OpenStack keypair to create or manage."
  type        = string
}

variable "public_key_path" {
  description = "Path to the local SSH public-key file."
  type        = string
}
