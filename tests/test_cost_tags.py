"""Telling Expenze's share of a shared AWS bill from everybody else's.

Account 231427841372 runs a dozen Mobil80 products together. The invoice is one
invoice: its S3 line is every product's S3, and nothing on it says what Expenze
costs. Cost allocation tags are the answer AWS gives to that, and they only work
if every billable resource actually carries one - a bill where 90% of the spend
is attributed and 10% is not is a bill nobody can reconcile, and the 10% is
always the thing somebody forgot.

So these tests are about coverage and consistency rather than about mechanics:

* every billable resource type in the synthesised templates is in the map;
* the map does not claim resource types that cannot carry a tag, which would be
  a coverage gap disguised as coverage;
* the key matches the convention the rest of the account already uses, because
  a second spelling of "which product is this" yields two partial answers and
  no total;
* the value names the product as well as the service, because Cost Explorer
  shows tag values without their keys.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from expensifyai import tagging  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STACKS = ("ExpensifyAI", "ExpenzeSite", "ExpenzeBms")

# Types that appear in the templates, charge nothing, and are deliberately left
# out of the map. Naming them here means a new free resource type shows up as a
# failure to be classified rather than silently passing as "billable, untagged".
FREE = {
    "AWS::IAM::Role", "AWS::IAM::Policy",
    "AWS::Lambda::Permission", "AWS::Lambda::EventSourceMapping",
    "AWS::ApiGateway::Method", "AWS::ApiGateway::Resource",
    "AWS::ApiGateway::Deployment",
    "AWS::S3::BucketPolicy",
    "AWS::SES::ReceiptRule", "AWS::SES::ReceiptRuleSet",
    "AWS::CloudFront::OriginAccessControl",
    "AWS::CertificateManager::Certificate",   # free when used with CloudFront
    "AWS::Route53::RecordSet",                # the zone charges, records do not
    "AWS::CDK::Metadata",
    # No Tags property in CloudFormation, so it cannot be tagged at all. Layer
    # storage counts against the Lambda code-storage quota and bills untagged.
    "AWS::Lambda::LayerVersion",
    # CDK's own scaffolding, not resources we chose.
    "Custom::S3AutoDeleteObjects", "Custom::S3BucketNotifications",
    "Custom::CDKBucketDeployment",
}


def _templates() -> dict[str, dict]:
    out = {}
    for name in STACKS:
        path = os.path.join(ROOT, "cdk.out", f"{name}.template.json")
        if not os.path.exists(path):
            raise unittest.SkipTest("run `cdk synth` first")
        with open(path, encoding="utf-8") as handle:
            out[name] = json.load(handle)
    return out


class EverySpendingResourceIsClassified(unittest.TestCase):
    """Nothing in the stacks is neither billable-and-tagged nor known-free."""

    def setUp(self):
        self.templates = _templates()

    def test_no_resource_type_is_unaccounted_for(self):
        for name, template in self.templates.items():
            for lid, res in template["Resources"].items():
                kind = res["Type"]
                self.assertTrue(
                    kind in tagging.BILLABLE or kind in FREE,
                    f"{name}/{lid} is a {kind}, which is in neither "
                    "tagging.BILLABLE nor the FREE list in this test. Decide "
                    "which it is - an unclassified type bills untagged.")

    def test_the_services_that_actually_charge_are_all_covered(self):
        # The floor, named explicitly. If a refactor drops the tag from any of
        # these, the bill silently stops adding up.
        tagged = set()
        for template in self.templates.values():
            for res in template["Resources"].values():
                for tag in (res.get("Properties") or {}).get("Tags") or []:
                    if isinstance(tag, dict) and tag.get("Key") == "Component":
                        tagged.add(tag["Value"])
        for service in ("S3", "Lambda", "DynamoDB", "API Gateway",
                        "Secrets Manager", "CloudFront", "EventBridge",
                        "CloudWatch Logs"):
            self.assertIn(tagging.component(service), tagged,
                          f"nothing carries {service}'s cost tag")


class TheKeysFollowTheAccountsExistingConvention(unittest.TestCase):

    def test_the_product_key_is_the_one_the_account_already_uses(self):
        # Flaunt and CloudMeter tag Application=FLAUNT / CLOUDMETER in this
        # same account. A third key meaning the same thing gives three partial
        # answers and no total.
        self.assertEqual("EXPENZE", tagging.APPLICATION)

    def test_a_component_value_names_the_product_too(self):
        # Cost Explorer lists tag values without their key, so a bare "S3"
        # sitting beside Flaunt's components would be unattributable.
        self.assertEqual("Expenze : Lambda", tagging.component("Lambda"))
        for value in tagging.BILLABLE.values():
            self.assertTrue(tagging.component(value).startswith("Expenze : "))

    def test_both_keys_are_on_every_billable_resource(self):
        for name, template in _templates().items():
            for lid, res in template["Resources"].items():
                if res["Type"] not in tagging.BILLABLE:
                    continue
                # The one knowing exception. CDK builds this through
                # CustomResourceProvider, which makes a raw CfnResource with no
                # tag manager behind it, so Tags.of() is a no-op on it. Reaching
                # in with add_property_override would work and would couple this
                # repo to CDK's internal logical ids and property shapes - for a
                # function that runs once, when the stack is deleted.
                if lid.startswith("CustomS3AutoDeleteObjects"):
                    continue
                keys = {t["Key"] for t in
                        ((res.get("Properties") or {}).get("Tags") or [])
                        if isinstance(t, dict)}
                self.assertIn("Application", keys, f"{name}/{lid}")
                self.assertIn("Component", keys, f"{name}/{lid}")

    def test_the_older_project_tag_is_left_alone(self):
        # It is on every deployed resource already and costs nothing to keep.
        # Removing it would make the two ways of asking disagree during the
        # window where only some resources had been redeployed.
        app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        self.assertIn('cdk.Tags.of(app).add("Project", "Expenze")', app)


class TheMapClaimsNothingItCannotDo(unittest.TestCase):

    def test_layer_versions_are_not_claimed(self):
        # CloudFormation gives AWS::Lambda::LayerVersion no Tags property, so
        # listing it would be a coverage gap dressed as coverage.
        self.assertNotIn("AWS::Lambda::LayerVersion", tagging.BILLABLE)

    def test_the_hosted_zone_is_handled_outside_the_stacks(self):
        # It predates them and is imported by id, so no deploy will ever tag
        # it - and Route 53 charges per zone, with twenty zones in the account.
        self.assertIn("AWS::Route53::HostedZone", tagging.BILLABLE)
        tool = open(os.path.join(ROOT, "tools", "tag_expenze.py"),
                    encoding="utf-8").read()
        self.assertIn("Z08768761ELNU80IGOYLG", tool)
        self.assertIn("change_tags_for_resource", tool)

    def test_the_straggler_report_never_tags_on_a_name_match_alone(self):
        # A substring is a hint, not a fact. Writing a cost tag onto another
        # product's resource on the strength of one puts their spend on our
        # line, and nobody checks a report that agrees with itself.
        tool = open(os.path.join(ROOT, "tools", "tag_expenze.py"),
                    encoding="utf-8").read()
        body = tool.split("def _untagged(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("tag_resources", body)
        self.assertNotIn("change_tags_for_resource", body)


class ATagIsInertUntilItIsActivated(unittest.TestCase):
    """The step this repo cannot take, written down where it will be found.

    Tagging a resource does nothing to a cost report on its own: the key has to
    be activated as a cost allocation tag, and that is a payer-account action.
    This account is a member of o-1h30usp144, whose payer is 975138397215 - so
    a deploy from here leaves tags that no report reads, and somebody has to
    know that.
    """

    def test_the_limitation_is_documented_beside_the_code(self):
        doc = tagging.__doc__ or ""
        self.assertIn("975138397215", doc)
        self.assertIn("activated", doc)


if __name__ == "__main__":
    unittest.main()
