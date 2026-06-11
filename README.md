# NVIDIA GPU Accelerated Application Samples on Microsoft Azure

**Table Of Contents**
- [Description](#description)
- [Support Level](#support-level)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [Samples](#samples)
- [Usage](#usage)
- [Known Issues](#known-issues)
- [Contributions](#contributions)
- [Support](#support)
- [License](#license)

## Description

This repository maintains sample applications designed for NVIDIA software tools
integrated with Microsoft Azure.

For select demonstrations, the sample code is contained within this repository.
For others, we reference and link to exceptional demonstrations available
outside of this repository.

## Support Level

These samples are provided as community examples and are not covered by NVIDIA
Enterprise Support. They are intended as reference implementations to help users
get started with NVIDIA software on Azure. See [Support](#support) below for how
to get help.

## Requirements

- An active [Microsoft Azure](https://azure.microsoft.com/) account with
  permissions to create the resources used by a given sample
- Access to NVIDIA GPU-enabled Azure VM sizes in your target Azure region
  (for example, A10, A100, H100, or H200 GPU families)
- The Azure CLI installed and configured, or access to the Azure portal
- An NGC API key for NVIDIA software access
- Additional, sample-specific prerequisites are documented in each sample's own
  README

## Quickstart

1. Clone this repository:
   ```bash
   git clone https://github.com/NVIDIA/nvidia-azure-samples
   cd nvidia-azure-samples
   ```
2. Browse the [Samples](#samples) section below and pick the one that matches
   your use case.
3. Follow the README inside that sample's directory for setup and run
   instructions.

## Samples

Samples are organized by Azure service:

- [`demo/aks/vss-msbuild/`](./demo/aks/vss-msbuild) - Sample that runs
  [NVIDIA Video Search and Summarization (VSS)](https://docs.nvidia.com/vss/)
  Long Video Summarization on
  [Azure Kubernetes Service (AKS)](https://learn.microsoft.com/azure/aks/),
  self-managed deployment on Kubernetes.
- [`demo/aks/vss-msbuild/demo/insurance-claims-foundry/`](./demo/aks/vss-msbuild/demo/insurance-claims-foundry) -
  Sample that runs an
  [Azure AI Foundry](https://learn.microsoft.com/azure/ai-foundry/) hosted
  agent that orchestrates NVIDIA VSS on AKS for an insurance-claims triage
  workflow.

## Usage

Each sample directory contains its own README with detailed deployment and usage
instructions. In general:

1. Provision the required Azure infrastructure (cluster, compute, networking,
   storage, and AI services) as described in the sample.
2. Deploy the NVIDIA software components.
3. Run the included workloads or applications.
4. Clean up the resources when you are done to avoid unnecessary charges.

## Known Issues

None at this time.

## Contributions

Contributions are welcome. Developers can contribute by opening a merge request
or pull request and agreeing to the terms in [CONTRIBUTING.md](CONTRIBUTING.md).

## Support

For questions or issues:
- Open an issue in this project for bug reports or feature requests
- Refer to each sample's README for sample-specific guidance

To report a security vulnerability, follow the process in
[SECURITY.md](SECURITY.md).

## License

See [LICENSE](LICENSE). This project is licensed under the Apache License 2.0.
