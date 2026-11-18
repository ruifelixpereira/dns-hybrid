def text:
  if type == "string" and length > 0 then . else error("Expected nonempty string") end;
def boolean:
  if type == "boolean" then . else error("Expected boolean") end;
def optional_text:
  if . == null then null else text end;
def optional_boolean:
  if . == null then null else boolean end;
def identity:
  {name: (.name | text), id: (.id | text |
    if test("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/[^/]+/[^/]+/[^/]+(/[^/]+/[^/]+)*$"; "i")
    then . else error("Invalid ARM resource ID") end)};
def arm_parts($kind):
  .id | text | capture(
    "^/subscriptions/(?<subscriptionId>[^/]+)/resourceGroups/(?<resourceGroup>[^/]+)/providers/Microsoft\\.Network/"
    + $kind + "/[^/]+$"; "i"
  ) // error("Invalid resource ID");
def resource($kind):
  . as $resource | arm_parts($kind) as $parts |
  if ($parts.subscriptionId | ascii_downcase) != ($subscriptionId | ascii_downcase)
  then error("Resource subscription mismatch")
  else ($resource | identity) + $parts end;
def vnet_ref:
  .id | text as $id |
  if ($id | test("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\\.Network/virtualNetworks/[^/]+/*$"; "i"))
  then {id: $id, name: ($id | sub("/+$"; "") | split("/") | last)}
  else error("Invalid VNet reference") end;
def records:
  map(identity + {
    fqdn: (.properties.fqdn | text),
    ttl: (.properties.ttl | if type == "number" and . >= 0 and floor == . then . else error("Invalid TTL") end),
    isAutoRegistered: (.properties.isAutoRegistered | optional_boolean),
    ipv4Addresses: ((.properties.aRecords // []) | map(.ipv4Address | text))
  }) | sort_by(.id | ascii_downcase);
def links:
  map(identity + {
    registrationEnabled: (.properties.registrationEnabled | boolean),
    virtualNetworkLinkState: (.properties.virtualNetworkLinkState | optional_text),
    provisioningState: (.properties.provisioningState | optional_text),
    virtualNetwork: (.properties.virtualNetwork | vnet_ref)
  }) | sort_by(.id | ascii_downcase);
def peerings:
  map(identity + {
    peeringState: (.properties.peeringState | optional_text),
    provisioningState: (.properties.provisioningState | optional_text),
    allowVirtualNetworkAccess: (.properties.allowVirtualNetworkAccess | optional_boolean),
    allowForwardedTraffic: (.properties.allowForwardedTraffic | optional_boolean),
    allowGatewayTransit: (.properties.allowGatewayTransit | optional_boolean),
    useRemoteGateways: (.properties.useRemoteGateways | optional_boolean),
    remoteVirtualNetwork: (.properties.remoteVirtualNetwork | vnet_ref)
  }) | sort_by(.id | ascii_downcase);
