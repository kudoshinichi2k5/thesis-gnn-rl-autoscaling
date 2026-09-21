[CmdletBinding()]
param(
    [string]$DesiredPrefix = "192.168.120.",
    [ValidateRange(1, 50)]
    [int]$MaxAttemptsPerNode = 10,
    [string]$TerraformDirectory = (Join-Path $PSScriptRoot "..\environments\dev"),
    [switch]$SkipInitialApply
)

$ErrorActionPreference = "Stop"
$terraformDirectoryPath = (Resolve-Path -LiteralPath $TerraformDirectory).Path

function Invoke-Terraform {
    param([string[]]$Arguments)

    & terraform "-chdir=$terraformDirectoryPath" @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Terraform command failed: terraform -chdir=$terraformDirectoryPath $($Arguments -join ' ')"
    }
}

function Get-NodeAddresses {
    $json = & terraform "-chdir=$terraformDirectoryPath" output -json node_fixed_ips
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read Terraform output node_fixed_ips."
    }

    return $json | ConvertFrom-Json
}

if (-not $SkipInitialApply) {
    Invoke-Terraform -Arguments @("apply", "-auto-approve")
}

$attempts = @{}
while ($true) {
    $nodeAddresses = Get-NodeAddresses
    $incorrectNodes = @(
        $nodeAddresses.PSObject.Properties |
        Where-Object { -not $_.Value.floating_ip.StartsWith($DesiredPrefix) }
    )

    if ($incorrectNodes.Count -eq 0) {
        Write-Host "All floating IPs match the required prefix $DesiredPrefix"
        break
    }

    foreach ($node in $incorrectNodes) {
        $nodeName = $node.Name
        $floatingIp = $node.Value.floating_ip
        $previousAttempts = if ($attempts.ContainsKey($nodeName)) { $attempts[$nodeName] } else { 0 }
        $attempts[$nodeName] = $previousAttempts + 1

        if ($attempts[$nodeName] -gt $MaxAttemptsPerNode) {
            throw "Node $nodeName did not receive a $DesiredPrefix floating IP after $MaxAttemptsPerNode replacement attempts. Its latest IP is $floatingIp."
        }

        Write-Warning "Node $nodeName received $floatingIp; replacing its floating IP (attempt $($attempts[$nodeName]) of $MaxAttemptsPerNode)."
        $resourceAddress = "module.floating_ip.openstack_networking_floatingip_v2.node[`"$nodeName`"]"
        Invoke-Terraform -Arguments @("apply", "-auto-approve", "-replace=$resourceAddress")
    }
}
