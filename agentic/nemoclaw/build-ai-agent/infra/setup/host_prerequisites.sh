#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

if [[ "$EUID" -ne 0 ]]; then
  printf 'VM prerequisites must run through the root provisioning extension.\n' >&2
  exit 1
fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(dpkg --print-architecture)" != amd64 ]]; then
  printf 'Automatic prerequisites require Ubuntu 24.04 amd64.\n' >&2
  exit 1
fi

install -d -m 0755 /etc/ignite-lab /opt/ignite-lab
touch /var/log/ignite-vm-prerequisites.log
chmod 0644 /var/log/ignite-vm-prerequisites.log
exec > >(tee -a /var/log/ignite-vm-prerequisites.log) 2>&1
trap 'printf "VM prerequisites failed at line %s.\n" "$LINENO" >&2' ERR
rm -f /etc/ignite-lab/prerequisites.json

# Wait for context.json and the image's initial apt work, not for this extension.
if command -v cloud-init >/dev/null; then
  cloud_init_rc=0
  timeout 600 cloud-init status --wait || cloud_init_rc=$?
  if [[ "$cloud_init_rc" != 0 && "$cloud_init_rc" != 2 ]]; then
    printf 'Initial cloud-init did not complete successfully.\n' >&2
    exit 1
  fi
fi
test -r /etc/ignite-lab/context.json

apt_options=(-o DPkg::Lock::Timeout=300 -o Acquire::Retries=5)
apt-get "${apt_options[@]}" update
apt-get "${apt_options[@]}" install -y --no-install-recommends \
  ca-certificates curl gnupg python3 python3-venv jq git nano unzip xz-utils \
  openssl build-essential binutils lsof zstd dbus-user-session

download() {
  curl --fail --location --silent --show-error --proto '=https' \
    --proto-redir '=https' --tlsv1.2 --connect-timeout 15 --max-time 300 \
    --retry 5 --retry-delay 2 "$1" --output "$2"
}

work_dir=$(mktemp -d /tmp/ignite-vm-prerequisites.XXXXXX)
trap 'rm -rf "$work_dir"' EXIT
install -d -m 0755 /etc/apt/keyrings
download https://download.docker.com/linux/ubuntu/gpg /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc
download https://packages.microsoft.com/keys/microsoft.asc "$work_dir/microsoft.asc"
gpg --batch --yes --dearmor --output /etc/apt/keyrings/microsoft.gpg "$work_dir/microsoft.asc"
chmod 0644 /etc/apt/keyrings/microsoft.gpg

cat > /etc/apt/sources.list.d/docker.sources <<'DOCKER_REPOSITORY'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-by: /etc/apt/keyrings/docker.asc
DOCKER_REPOSITORY
cat > /etc/apt/sources.list.d/azure-cli.sources <<'AZURE_CLI_REPOSITORY'
Types: deb
URIs: https://packages.microsoft.com/repos/azure-cli/
Suites: noble
Components: main
Architectures: amd64
Signed-by: /etc/apt/keyrings/microsoft.gpg
AZURE_CLI_REPOSITORY
apt-get "${apt_options[@]}" update
apt-get "${apt_options[@]}" install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin azure-cli

node_version=24.21.0
node_sha256=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
node_archive="node-v${node_version}-linux-x64.tar.xz"
node_dir="/opt/ignite-lab/node-v${node_version}-linux-x64"
if [[ ! -x "$node_dir/bin/node" ]] || [[ "$("$node_dir/bin/node" --version)" != "v$node_version" ]]; then
  download "https://nodejs.org/dist/v${node_version}/${node_archive}" "$work_dir/$node_archive"
  printf '%s  %s\n' "$node_sha256" "$work_dir/$node_archive" | sha256sum --check --status
  tar --extract --xz --file "$work_dir/$node_archive" --directory /opt/ignite-lab
fi
for node_command in node npm npx; do
  ln -sfn "$node_dir/bin/$node_command" "/usr/local/bin/$node_command"
done

kubectl_version=$(python3 -c 'import json; print(json.load(open("/etc/ignite-lab/context.json"))["kubernetesVersion"])')
if [[ ! "$kubectl_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'AKS did not return a full supported Kubernetes version.\n' >&2
  exit 1
fi
download "https://dl.k8s.io/release/v${kubectl_version}/bin/linux/amd64/kubectl" "$work_dir/kubectl"
download "https://dl.k8s.io/release/v${kubectl_version}/bin/linux/amd64/kubectl.sha256" "$work_dir/kubectl.sha256"
kubectl_sha256=$(cat "$work_dir/kubectl.sha256")
if [[ ! "$kubectl_sha256" =~ ^[a-fA-F0-9]{64}$ ]]; then
  printf 'Invalid kubectl publisher checksum.\n' >&2
  exit 1
fi
printf '%s  %s\n' "$kubectl_sha256" "$work_dir/kubectl" | sha256sum --check --status
install -m 0755 "$work_dir/kubectl" /usr/local/bin/kubectl

kubelogin_version=0.2.21
kubelogin_sha256=934a2a9618e018cb9fbeb14f3e4c28416fdae815df7706df91500e41d234eb66
download "https://github.com/Azure/kubelogin/releases/download/v${kubelogin_version}/kubelogin-linux-amd64.zip" "$work_dir/kubelogin.zip"
printf '%s  %s\n' "$kubelogin_sha256" "$work_dir/kubelogin.zip" | sha256sum --check --status
unzip -q "$work_dir/kubelogin.zip" -d "$work_dir/kubelogin"
install -m 0755 "$work_dir/kubelogin/bin/linux_amd64/kubelogin" /usr/local/bin/kubelogin

target_user=$(python3 -c 'import json; print(json.load(open("/etc/ignite-lab/context.json"))["vmAdminUsername"])')
id "$target_user" >/dev/null
usermod -aG docker "$target_user"
systemctl enable --now docker

# runuser loads the new supplementary groups, just as a fresh learner login does.
runuser -u "$target_user" -- docker info --format '{{.ServerVersion}}'
runuser -u "$target_user" -- node --version
runuser -u "$target_user" -- npm --version
runuser -u "$target_user" -- az version --output json
runuser -u "$target_user" -- kubectl version --client --output json
runuser -u "$target_user" -- kubelogin --version
python3 - <<'WRITE_READINESS'
import json, os, subprocess, tempfile
def output(*args):
    return subprocess.check_output(args, text=True).strip()
context = json.load(open('/etc/ignite-lab/context.json'))
versions = {
    'schemaVersion': 1,
    'status': 'ready',
    'vmAdminUsername': context['vmAdminUsername'],
    'azureCli': json.loads(output('az', 'version', '--output', 'json'))['azure-cli'],
    'kubectl': json.loads(output('kubectl', 'version', '--client', '--output', 'json'))['clientVersion']['gitVersion'],
    'kubelogin': output('kubelogin', '--version'),
    'docker': output('docker', 'info', '--format', '{{.ServerVersion}}'),
    'node': output('node', '--version'),
    'npm': output('npm', '--version'),
}
with tempfile.NamedTemporaryFile(mode='w', dir='/etc/ignite-lab', delete=False) as f:
    json.dump(versions, f, indent=2)
    f.write('\n')
    temporary_path = f.name
os.chmod(temporary_path, 0o644)
os.replace(temporary_path, '/etc/ignite-lab/prerequisites.json')
WRITE_READINESS
printf 'VM host prerequisites are ready.\n'
