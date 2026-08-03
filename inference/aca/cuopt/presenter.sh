#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

: "${RESOURCE_GROUP:=rg-freshroute-cuopt-aca}"
: "${CUOPT_APP_NAME:=cuopt-nim}"
: "${WEB_APP_NAME:=freshroute-web}"
action="${1:-status}"

deactivate_zero_traffic_revisions() {
  while IFS= read -r revision; do
    [[ -z "$revision" ]] && continue
    az containerapp revision deactivate --name "$CUOPT_APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --revision "$revision" --output none
  done < <(
    az containerapp revision list --name "$CUOPT_APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --query "[?properties.active && properties.trafficWeight == \`0\`].name" --output tsv
  )
}

case "$action" in
  prepare)
    az containerapp update --name "$CUOPT_APP_NAME" --resource-group "$RESOURCE_GROUP" --min-replicas 1 --max-replicas 1 --output none
    deactivate_zero_traffic_revisions
    echo "GPU warm-up requested. Follow readiness with: $0 status"
    ;;
  finish)
    az containerapp update --name "$CUOPT_APP_NAME" --resource-group "$RESOURCE_GROUP" --min-replicas 0 --max-replicas 1 --output none
    deactivate_zero_traffic_revisions
    echo "Minimum replicas set to zero. ACA will remove the idle GPU replica."
    ;;
  reset)
    fqdn=$(az containerapp show --name "$WEB_APP_NAME" --resource-group "$RESOURCE_GROUP" --query properties.configuration.ingress.fqdn -o tsv)
    curl --fail --silent --show-error --request POST "https://${fqdn}/api/demo/reset"
    echo
    ;;
  status)
    az containerapp revision list --name "$CUOPT_APP_NAME" --resource-group "$RESOURCE_GROUP" \
      --query "[].{revision:name,state:properties.runningState,replicas:properties.replicas,detail:properties.runningStateDetails}" --output table
    ;;
  *) echo "Usage: $0 {prepare|status|reset|finish}" >&2; exit 2 ;;
esac
