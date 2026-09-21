variable "keypair_name" {
  description = "Name of the OpenStack keypair to create or manage."
  type        = string
}

variable "public_key_path" {
  description = "Path to the local SSH public-key file."
  type        = string

  validation {
    condition     = fileexists(pathexpand(var.public_key_path))
    error_message = "public_key_path must point to an existing OpenSSH public-key file on the machine running Terraform."
  }
}
