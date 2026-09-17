"""Making Expenze's share of a shared AWS bill legible.

Account 231427841372 runs a dozen Mobil80 products side by side. The invoice
that arrives is one invoice, and "Cost by service" on it answers a question
nobody is asking: the S3 line is every product's S3, and no amount of staring
at it says what Expenze costs.

Cost allocation tags are how AWS answers the question it is actually asked.
Every billable resource carries a tag; Cost Explorer groups the bill by the tag
value instead of by the service; and one product's spend separates out of a
shared account without separating the account.

Two keys, because there are two questions.

**``Application``** - which product is this? Values are bare product names, and
the key is not new: the rest of the account already tags ``Application=FLAUNT``
and ``Application=CLOUDMETER``. Matching that convention rather than inventing
a parallel one is the whole reason the roll-up works - a third key with a third
spelling of the same idea produces three partial answers and no total.

**``Component``** - which part of this product? Values read ``Expenze : S3``,
``Expenze : Lambda``, ``Expenze : DynamoDB``. The product name is repeated
inside the value on purpose: Cost Explorer shows tag values without their key,
so a bare ``S3`` in a list beside Flaunt's components would be unattributable.

Only what can appear on a bill
------------------------------
IAM roles and policies, Lambda permissions, API Gateway methods and SES rules
are all taggable and all free. Tagging them adds rows to a cost report that are
permanently zero, which makes the report harder to read in exchange for
nothing. The map below is deliberately a list of services that charge, and a
resource type missing from it is untagged by design.

Two things this cannot do from here
-----------------------------------
A tag is inert until it is **activated** in the Billing console, and that is a
payer-account action - this account is a member of ``o-1h30usp144``, whose
payer is 975138397215. Until somebody activates both keys there, every resource
below carries a tag that no report reads.

And the hosted zone is not in this file, because it is not in these stacks: the
``expenze.ai`` zone predates them and is imported by id. ``tools/tag_expenze.py``
tags it, along with anything else standing outside CloudFormation.

One resource is knowingly left untagged: the ``S3AutoDeleteObjects`` handler
CDK generates per stack. It is built by ``CustomResourceProvider``, which
bypasses the tag manager, and it runs once per stack deletion - so the cost of
chasing it is real and the cost it incurs is not.
"""
from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

# The product, spelled as the account's existing tags spell their products.
APPLICATION = "EXPENZE"

# How a component reads in a cost report: the product, then the service.
PRODUCT = "Expenze"

# CloudFormation type -> the service name as AWS itself bills it. Keys here are
# the services that charge; see the module docstring for what is left out.
BILLABLE = {
    "AWS::S3::Bucket": "S3",
    "AWS::Lambda::Function": "Lambda",
    # No AWS::Lambda::LayerVersion. CloudFormation gives it no Tags property at
    # all, so listing it here would have this file claiming a coverage it does
    # not have - CDK drops the tag silently and the layer bills untagged under
    # Lambda code storage regardless.
    "AWS::DynamoDB::Table": "DynamoDB",
    "AWS::ApiGateway::RestApi": "API Gateway",
    "AWS::ApiGateway::Stage": "API Gateway",
    "AWS::SecretsManager::Secret": "Secrets Manager",
    "AWS::Logs::LogGroup": "CloudWatch Logs",
    "AWS::Events::Rule": "EventBridge",
    "AWS::CloudFront::Distribution": "CloudFront",
    "AWS::SQS::Queue": "SQS",
    "AWS::SNS::Topic": "SNS",
    "AWS::Route53::HostedZone": "Route 53",
}


def component(service: str) -> str:
    """The value one component carries. ``Expenze : Lambda``."""
    return f"{PRODUCT} : {service}"


def apply(scope: Construct) -> dict[str, int]:
    """Tag every billable resource under ``scope``. Returns a count per service.

    A walk of the construct tree rather than an Aspect. Aspects are the usual
    way to do this and would be the right tool if the tag depended on anything
    about the resource beyond its type - but a tag applied from inside an aspect
    visit is applied by *another* aspect, and the ordering between the two is
    not something worth relying on for the input to an invoice. This runs once,
    before synthesis, over a tree that is fully built by then.

    The count is returned rather than logged so the caller can assert on it: a
    map that silently stops matching after a CDK upgrade renames a resource
    type would otherwise show up as an unexplained gap in a cost report months
    later, which is the slowest possible way to find out.
    """
    counted: dict[str, int] = {}
    for node in scope.node.find_all():
        if not isinstance(node, cdk.CfnResource):
            continue
        service = BILLABLE.get(node.cfn_resource_type)
        if not service:
            continue
        cdk.Tags.of(node).add("Application", APPLICATION)
        cdk.Tags.of(node).add("Component", component(service))
        counted[service] = counted.get(service, 0) + 1
    return counted
