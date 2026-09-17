#!/usr/bin/env python3
"""Tag the Expenze resources that CloudFormation does not own.

Everything declared in the stacks is tagged at deploy time - see
``expensifyai/tagging.py``. This is for the remainder: resources that were
created before the stacks, or by hand, or that CDK imports by id rather than
declaring. They bill exactly like the rest and are invisible in a cost report
for want of two tags.

Today that is the ``expenze.ai`` hosted zone. It predates the stacks and is
imported by id everywhere - declaring a zone that already exists is what
produced the duplicate ``cloudmeter.io`` zone in this account - so nothing in
CDK will ever tag it. Route 53 charges per zone per month, and there are twenty
zones in this account; without this the Expenze one is indistinguishable from
the other nineteen.

It also reports what it finds *untagged* across the regions Expenze runs in,
which is the part worth running again later. A resource created by hand during
an incident is exactly the resource nobody remembers to tag, and it will sit on
the invoice unattributed until something goes looking.

Usage
-----
    python tools/tag_expenze.py            # reports, changes nothing
    python tools/tag_expenze.py --yes      # applies the missing tags
"""
from __future__ import annotations

import argparse
import sys

import boto3

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from expensifyai.tagging import APPLICATION, component  # noqa: E402

# Where Expenze has resources. us-east-1 holds the two CloudFront stacks and
# their certificates; ap-southeast-1 holds everything else.
REGIONS = ["ap-southeast-1", "us-east-1"]

# The zone is global, imported by id, and named here because that id is the
# single source of truth for it in this repo as well (see app.py).
HOSTED_ZONE_ID = "Z08768761ELNU80IGOYLG"
HOSTED_ZONE_NAME = "expenze.ai"

# What identifies a resource as Expenze's when it carries no Application tag
# yet. Deliberately narrow: this account runs a dozen products and a substring
# match that caught somebody else's bucket would put their spend on our line.
NAME_HINTS = ("expenze", "expensifyai")


def _tag_hosted_zone(dry: bool) -> bool:
    r53 = boto3.client("route53")
    current = {t["Key"]: t["Value"] for t in r53.list_tags_for_resource(
        ResourceType="hostedzone", ResourceId=HOSTED_ZONE_ID
    )["ResourceTagSet"]["Tags"]}
    wanted = {"Application": APPLICATION, "Component": component("Route 53"),
              "Project": "Expenze"}
    missing = {k: v for k, v in wanted.items() if current.get(k) != v}
    if not missing:
        print(f"  hosted zone {HOSTED_ZONE_NAME}: already tagged")
        return False
    print(f"  hosted zone {HOSTED_ZONE_NAME}: "
          + ", ".join(f"{k}={v}" for k, v in missing.items()))
    if not dry:
        r53.change_tags_for_resource(
            ResourceType="hostedzone", ResourceId=HOSTED_ZONE_ID,
            AddTags=[{"Key": k, "Value": v} for k, v in missing.items()],
        )
    return True


def _tag_ses_identities(dry: bool) -> bool:
    """The verified sending domain, in each region that sends from it.

    Verified by hand when the domain was set up, not declared in CDK - the
    stacks own the *receipt rules* that act on inbound mail, never the identity
    itself. So like the hosted zone it is ours, it bills, and nothing in a
    deploy will ever tag it.
    """
    changed = False
    for region in REGIONS:
        ses = boto3.client("sesv2", region_name=region)
        try:
            current = {t["Key"]: t["Value"] for t in ses.get_email_identity(
                EmailIdentity=HOSTED_ZONE_NAME).get("Tags", [])}
        except Exception:
            print(f"  SES identity {HOSTED_ZONE_NAME} ({region}): not present")
            continue
        wanted = {"Application": APPLICATION, "Component": component("SES")}
        missing = {k: v for k, v in wanted.items() if current.get(k) != v}
        if not missing:
            print(f"  SES identity {HOSTED_ZONE_NAME} ({region}): already tagged")
            continue
        print(f"  SES identity {HOSTED_ZONE_NAME} ({region}): "
              + ", ".join(f"{k}={v}" for k, v in missing.items()))
        changed = True
        if not dry:
            ses.tag_resource(
                ResourceArn=f"arn:aws:ses:{region}:{_account()}:identity/{HOSTED_ZONE_NAME}",
                Tags=[{"Key": k, "Value": v} for k, v in missing.items()],
            )
    return changed


def _account() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def _untagged(region: str) -> list[tuple[str, str]]:
    """Anything in this region that looks like ours and carries no Application tag.

    Reported, never tagged automatically. A name that contains "expenze" is a
    strong hint and not a fact, and writing a cost-allocation tag onto another
    product's resource on the strength of a substring is how one team's spend
    silently lands on another team's report.
    """
    api = boto3.client("resourcegroupstaggingapi", region_name=region)
    found, token = [], None
    while True:
        page = api.get_resources(**({"PaginationToken": token} if token else {}))
        for row in page.get("ResourceTagMappingList", []):
            arn = row["ResourceARN"]
            tags = {t["Key"]: t["Value"] for t in row.get("Tags", [])}
            if tags.get("Application") == APPLICATION:
                continue
            if any(hint in arn.lower() for hint in NAME_HINTS):
                found.append((arn, tags.get("Application", "—")))
        token = page.get("PaginationToken")
        if not token:
            break
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="apply the tags. Without it, nothing is written.")
    args = ap.parse_args()
    dry = not args.yes

    print("Outside CloudFormation:")
    changed = _tag_hosted_zone(dry)
    changed = _tag_ses_identities(dry) or changed

    print("\nLooks like Expenze but carries no Application tag:")
    stragglers = []
    for region in REGIONS:
        stragglers.extend(_untagged(region))
    if not stragglers:
        print("  nothing — every Expenze resource in "
              + " and ".join(REGIONS) + " is tagged")
    for arn, app in stragglers:
        print(f"  {arn}  (Application={app})")
    if stragglers:
        print("\n  Not tagged automatically. Check each one is really Expenze's"
              "\n  before adding it — see _untagged() for why.")

    if dry and changed:
        print("\nNothing written. Re-run with --yes to apply.")
    elif changed:
        print("\nApplied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
