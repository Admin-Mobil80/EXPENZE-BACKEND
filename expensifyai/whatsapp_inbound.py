"""The topic inbound WhatsApp arrives on, in the region AWS insists on.

Meta POSTs a webhook to a URL. AWS End User Messaging does not: it publishes
to SNS, and the WABA's event destination has to name a topic **in the WABA's
own region**. Ours is linked in us-east-1 and everything else Expenze runs is
in ap-southeast-1, so the topic cannot sit beside the function that consumes
it - `PutWhatsAppBusinessAccountEventDestinations` refuses a topic from
anywhere else with "Invalid EventDestination Region", which is a clear enough
message once you have seen it and tells you nothing in advance.

So: one topic, here, and a subscription reaching back across regions to the
adapter in ap-southeast-1. SNS allows that for Lambda, and it is the smaller
of the two compromises available. The other was re-linking the WABA into
ap-southeast-1, which means redoing the Meta onboarding by hand and buys a
saved hop on a path that already waits on Meta.

Neither end is passed in. Both are fixed names this project chose, so the ARNs
can be spelled out rather than threaded through a cross-region, cross-stack
reference - which in CDK costs a custom resource and an ordering constraint
between two stacks that otherwise have nothing to say to each other.

What this stack deliberately does not do is point the WABA at the topic. The
account is linked in the console, outside CloudFormation, and a stack that
cannot see a resource cannot own its configuration. `wa_migrate.py aws-wire`
does that, and says so.
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_sns as sns,
)
from constructs import Construct

# Where the WABA is linked, and therefore where the topic has to be.
REGION = "us-east-1"

# The adapter, which lives in the other region.
CONSUMER_REGION = "ap-southeast-1"
CONSUMER_FUNCTION = "expenze-whatsapp"

TOPIC = "expenze-whatsapp-inbound"


class ExpenzeWhatsAppInboundStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        topic = sns.Topic(
            self,
            "Inbound",
            topic_name=TOPIC,
            display_name="Inbound WhatsApp from AWS End User Messaging",
        )

        # The raw subscription, not `topic.add_subscription(LambdaSubscription)`.
        #
        # The friendly form also writes an AWS::Lambda::Permission allowing the
        # topic to invoke the function - correct, and impossible here, because
        # that permission would be created in this stack's region: "Functions
        # from 'ap-southeast-1' are not reachable in this region ('us-east-1')".
        #
        # The permission is real and necessary; it is simply declared where the
        # function is, in ExpensifyAI. All that belongs here is the subscription
        # itself, which SNS accepts across regions.
        sns.CfnSubscription(
            self,
            "Adapter",
            topic_arn=topic.topic_arn,
            protocol="lambda",
            endpoint=(f"arn:aws:lambda:{CONSUMER_REGION}:{self.account}"
                      f":function:{CONSUMER_FUNCTION}"),
        )

        CfnOutput(self, "InboundTopic", value=topic.topic_arn)
