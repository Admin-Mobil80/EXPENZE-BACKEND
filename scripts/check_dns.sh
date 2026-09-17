#!/usr/bin/env bash
# Is expenze.ai delegated to our hosted zone yet?
#
# Checks the .ai registry directly rather than a resolver, because a resolver
# can serve a cached answer for hours after the registry has changed. The
# registry is the source of truth for delegation.
set -uo pipefail

DOMAIN="${1:-expenze.ai}"
ZONE_ID="${2:-Z08768761ELNU80IGOYLG}"
PROFILE="${AWS_PROFILE:-expensifyai}"

echo "Domain     : $DOMAIN"
echo "Our zone   : $ZONE_ID"
echo

ours=$(aws route53 get-hosted-zone --id "$ZONE_ID" --profile "$PROFILE" \
        --query 'DelegationSet.NameServers[]' --output text 2>/dev/null | tr '\t' '\n' | sed 's/\.$//' | sort)
if [ -z "$ours" ]; then
  echo "Could not read the hosted zone. Run: aws sso login --profile $PROFILE"
  exit 1
fi
echo "Our nameservers:"; echo "$ours" | sed 's/^/  /'
echo

tld=$(dig +short NS ai. 2>/dev/null | head -1)
live=$(dig +norecurse NS "$DOMAIN" @"$tld" 2>/dev/null \
        | awk '$4=="NS" {print $5}' | sed 's/\.$//' | sort)
if [ -z "$live" ]; then
  echo "The .ai registry returns no delegation for $DOMAIN."
  echo "Either the domain is not registered, or the nameservers were never set."
  exit 1
fi
echo "Registry ($tld) currently returns:"; echo "$live" | sed 's/^/  /'
echo

# Do the currently-delegated nameservers actually serve the zone? Deleting a
# hosted zone does NOT change delegation, so a domain can end up pointing at
# nameservers that answer REFUSED - which resolvers surface as SERVFAIL.
first_live=$(echo "$live" | head -1)
status=$(dig NS "$DOMAIN" @"$first_live" 2>/dev/null | grep -o "status: [A-Z]*" | head -1 | awk '{print $2}')
if [ "$status" = "REFUSED" ]; then
  echo "BROKEN — the delegated nameservers no longer serve $DOMAIN (REFUSED)."
  echo "The zone they belonged to was deleted, but the domain still points at them,"
  echo "so $DOMAIN resolves nowhere. Fix is the same as below."
  echo
fi

if [ "$ours" = "$live" ]; then
  echo "MATCH — $DOMAIN is delegated to our zone."
  echo "Resolvers may still serve a cached answer until the old TTL expires."
else
  echo "MISMATCH — the domain still points somewhere else."
  echo
  echo "Fix it on the DOMAIN REGISTRATION, not the hosted zone. In the parent"
  echo "account (975138397215):"
  echo "  Route 53 -> Registered domains -> $DOMAIN -> Edit name servers"
  echo
  echo "Or, with that account's credentials:"
  echo "  aws route53domains update-domain-nameservers --region us-east-1 \\"
  echo "    --domain-name $DOMAIN \\"
  printf "    --nameservers"; echo "$ours" | sed 's/^/ Name=/' | tr -d '\n'; echo
fi
