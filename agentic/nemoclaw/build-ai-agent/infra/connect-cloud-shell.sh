#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run in standard Azure Cloud Shell. No VM password or model key is accepted
# as an argument: OpenSSH prompts for the assigned per-instance VM password.
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: bash connect-cloud-shell.sh --subscription ID --resource-group NAME [--vm NAME] [--source-ip IPV4] [--close]

Selects this lab's Ubuntu VM, allows TCP 22 from one public IPv4 /32, and SSHs
to it. Removes that allow rule when SSH exits. --close removes the rule without
connecting, for example after an interrupted Cloud Shell session.
HELP
}

fail() { printf '%s\n' "$*" >&2; exit 1; }
subscription=''
resource_group=''
vm_name=''
source_ip=''
close_only=false
while (($#)); do
  case "$1" in
    --subscription|--resource-group|--vm|--source-ip)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || fail "Missing value for $1."
      case "$1" in
        --subscription) subscription=$2 ;;
        --resource-group) resource_group=$2 ;;
        --vm) vm_name=$2 ;;
        --source-ip) source_ip=$2 ;;
      esac
      shift 2 ;;
    --close) close_only=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; fail "Unknown argument: $1" ;;
  esac
done
[[ -n "$subscription" && -n "$resource_group" ]] || { usage >&2; exit 1; }
for tool in az python3 ssh curl; do
  command -v "$tool" >/dev/null || fail "Required Cloud Shell command is missing: $tool"
done

# Restrict every discovered network resource to the specified lab subscription
# and resource group, even if a VM has been modified to reference another one.
check_resource_id() {
  python3 - "$1" "$2" "$subscription" "$resource_group" <<'PY'
import re, sys
resource_id, kind, subscription, group = sys.argv[1:]
parts = resource_id.strip('/').split('/')
expected_length = 10 if kind == 'subnets' else 8
valid = (len(parts) == expected_length and
         parts[0].lower() == 'subscriptions' and
         parts[1].lower() == subscription.lower() and
         parts[2].lower() == 'resourcegroups' and
         parts[3].lower() == group.lower() and
         parts[4].lower() == 'providers' and
         parts[5].lower() == 'microsoft.network')
if kind == 'subnets':
    valid = valid and parts[6].lower() == 'virtualnetworks' and parts[8].lower() == 'subnets'
else:
    valid = valid and parts[6].lower() == kind.lower()
if not valid or not re.fullmatch(r'[A-Za-z0-9_.-]+', parts[-1]):
    sys.exit('Network resource is outside the assigned lab scope or has an invalid ID.')
print(parts[-1])
PY
}

validate_public_ipv4() {
  python3 - "$1" <<'PY'
import ipaddress, sys
try:
    address = ipaddress.IPv4Address(sys.argv[1])
    if not address.is_global or address.is_multicast:
        raise ValueError()
except ValueError:
    sys.exit('Expected one public IPv4 address without a range or CIDR suffix.')
print(address)
PY
}

# Use an explicit subscription on every Azure command; do not change the
# Cloud Shell session's active subscription or rely on its CLI defaults.
lab_az() { az "$@" --subscription "$subscription" --only-show-errors; }
vms=$(lab_az vm list --resource-group "$resource_group" \
  --query "[?tags.application=='ignite-nemoclaw-aks-lab'].{name:name,username:osProfile.adminUsername,nicId:networkProfile.networkInterfaces[0].id}" --output json)
vm_fields=$(python3 - "$vms" "$vm_name" <<'PY'
import json, re, sys
vms = json.loads(sys.argv[1])
if sys.argv[2]:
    vms = [vm for vm in vms if vm['name'] == sys.argv[2]]
if len(vms) != 1:
    sys.exit('Expected exactly one tagged lab VM. Use --vm NAME if the group contains several.')
vm = vms[0]
if not re.fullmatch(r'[a-z_][a-z0-9_-]*', vm['username']):
    sys.exit('Unexpected Linux login name; contact the lab instructor.')
print(vm['name'], vm['username'], vm['nicId'], sep='\n')
PY
)
{
  IFS= read -r vm_name
  IFS= read -r vm_username
  IFS= read -r nic_id
} <<< "$vm_fields"
check_resource_id "$nic_id" networkInterfaces >/dev/null
subnet_id=$(lab_az network nic show --ids "$nic_id" --query 'ipConfigurations[0].subnet.id' --output tsv)
check_resource_id "$subnet_id" subnets >/dev/null
nsg_id=$(lab_az network vnet subnet show --ids "$subnet_id" --query networkSecurityGroup.id --output tsv)
nsg_name=$(check_resource_id "$nsg_id" networkSecurityGroups)
rule_name='AllowSshFromCloudShell'
rule_args=(--resource-group "$resource_group" --nsg-name "$nsg_name" --name "$rule_name")

if "$close_only"; then
  lab_az network nsg rule delete "${rule_args[@]}" --output none
  printf 'SSH access closed for %s.\n' "$vm_name"
  exit 0
fi

public_ip_id=$(lab_az network nic show --ids "$nic_id" --query 'ipConfigurations[0].publicIPAddress.id' --output tsv)
check_resource_id "$public_ip_id" publicIPAddresses >/dev/null
public_ip=$(lab_az network public-ip show --ids "$public_ip_id" --query ipAddress --output tsv)
public_ip=$(validate_public_ipv4 "$public_ip")
private_ip=$(lab_az network nic show --ids "$nic_id" --query 'ipConfigurations[0].privateIPAddress' --output tsv)
python3 - "$private_ip" <<'PY'
import ipaddress, sys
address = ipaddress.IPv4Address(sys.argv[1])
if address not in ipaddress.IPv4Network('10.42.1.0/24'):
    sys.exit('VM address is outside the template attendee subnet.')
PY

if [[ -z "$source_ip" ]]; then
  source_ip=$(curl --ipv4 --noproxy '*' --fail --silent --show-error --max-time 10 https://api.ipify.org)
fi
source_ip=$(validate_public_ipv4 "$source_ip")
source_cidr="$source_ip/32"

# Revoke only this session's rule. A second connection may have replaced its
# source; preserve that newer rule rather than closing the other session.
cleanup() {
  local current_rule
  if ! current_rule=$(lab_az network nsg rule list --resource-group "$resource_group" \
    --nsg-name "$nsg_name" --query "[?name=='$rule_name'] | [0]" --output json); then
    printf 'Could not check SSH access. Rerun this helper with --close.\n' >&2
    return
  fi
  if python3 - "$current_rule" "$source_cidr" <<'PY'
import json, sys
rule = json.loads(sys.argv[1]) or {}
prefixes = rule.get('sourceAddressPrefixes') or [rule.get('sourceAddressPrefix')]
sys.exit(0 if prefixes == [sys.argv[2]] else 1)
PY
  then
    if lab_az network nsg rule delete "${rule_args[@]}" --output none; then
      printf 'SSH access closed for %s.\n' "$vm_name"
    else
      printf 'Could not close SSH access. Rerun this helper with --close.\n' >&2
    fi
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

lab_az network nsg rule create "${rule_args[@]}" --priority 100 \
  --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes "$source_cidr" --source-port-ranges '*' \
  --destination-address-prefixes "$private_ip" --destination-port-ranges 22 \
  --description 'Temporary SSH from the current lab Cloud Shell session' --output none
printf 'SSH source: %s. Connecting to %s (%s).\n' "$source_cidr" "$vm_name" "$public_ip"
printf 'Use your assigned Ubuntu VM password at the SSH prompt.\n'
ssh -o ConnectTimeout=20 -o ConnectionAttempts=3 -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 "$vm_username@$public_ip"
