"""A static application behind CloudFront, with SSL. Instantiated once per app.

Expenze runs two of these:

    expenze.ai      (+ www)  - the marketing site
    bms.expenze.ai           - the BMS application

Each gets its own bucket, its own distribution and its own certificate, so they
deploy, cache and roll back independently. What they share is the single
`expenze.ai` hosted zone: `bms` is a record in it, not a zone of its own. A
child zone only earns its keep when DNS control for that subdomain has to be
handed to a different team or account, and both of these are ours.

Deployed to **us-east-1**, not ap-southeast-1 like the API. That is not a
preference - CloudFront only accepts an ACM certificate issued in us-east-1, so
putting the stack there avoids a cross-region reference for one certificate.
CloudFront is global; the bucket's region only affects origin fetches on cache
misses, immaterial for a handful of static files.

us-east-1 is already CDK-bootstrapped in this account, so this adds no shared
bootstrap resources. The hosted zone is **imported by id, never declared** -
declaring a zone that already exists is what produced the duplicate
cloudmeter.io zone here.
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct


class ExpenzeSiteStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str,
        zone_name: str,
        hosted_zone_id: str,
        source_dir: str,
        aliases: list[str] | None = None,
        bucket_suffix: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        aliases = aliases or []
        all_names = [domain_name, *aliases]

        # ------------------------------------------------------------------
        # Origin
        # ------------------------------------------------------------------
        # Private. Nothing is world-readable directly from S3 - CloudFront
        # reaches it through Origin Access Control, so the bucket carries no
        # public policy and exposes no website endpoint to leak.
        bucket = s3.Bucket(
            self,
            "SiteBucket",
            bucket_name=f"expenze-{bucket_suffix}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ------------------------------------------------------------------
        # SSL
        # ------------------------------------------------------------------
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "Zone", hosted_zone_id=hosted_zone_id, zone_name=zone_name
        )

        certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=domain_name,
            subject_alternative_names=aliases or None,
            # Validation records are written into the zone above, so nobody
            # touches DNS by hand. This blocks the deploy until the certificate
            # is issued - usually a few minutes, and only possible because
            # expenze.ai is already delegated to that zone.
            validation=acm.CertificateValidation.from_dns(zone),
        )

        # ------------------------------------------------------------------
        # Distribution
        # ------------------------------------------------------------------
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"Expenze - {domain_name}",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
                compress=True,
            ),
            domain_names=all_names,
            certificate=certificate,
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            # A missing path should show the app, not an XML S3 error. 403 is
            # what OAC returns for a key that is not there.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=code,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(5),
                )
                for code in (403, 404)
            ],
            # PRICE_CLASS_ALL would add South America and Australia edges for
            # sites with no traffic yet. 200 covers India, SE Asia, Europe and
            # North America.
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
        )

        # ------------------------------------------------------------------
        # DNS - records in the shared expenze.ai zone
        # ------------------------------------------------------------------
        for name in all_names:
            # None means the zone apex; anything else is the label below it.
            label = None if name == zone_name else name.removesuffix("." + zone_name)
            safe = "Apex" if label is None else "".join(
                part.capitalize() for part in label.split(".")
            )
            for kind, record in (("A", route53.ARecord), ("Aaaa", route53.AaaaRecord)):
                record(
                    self,
                    f"{safe}{kind}",
                    zone=zone,
                    record_name=label,
                    target=route53.RecordTarget.from_alias(
                        targets.CloudFrontTarget(distribution)
                    ),
                )

        # ------------------------------------------------------------------
        # Content
        # ------------------------------------------------------------------
        s3deploy.BucketDeployment(
            self,
            "SiteContent",
            sources=[s3deploy.Source.asset(source_dir)],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
            # Every file served here is hand-written HTML under a stable name -
            # there is no fingerprinting to make a stale copy harmless. Without
            # this the objects carried no Cache-Control at all, so browsers
            # applied heuristic caching and went on using an old console for
            # hours after a deploy: a fix would be live at the edge, invalidated,
            # verified by curl, and still absent in the one browser that mattered.
            #
            # `no-cache` does not mean "do not store" - it means revalidate
            # before use, which with the ETag already on every object is a 304
            # and a few hundred bytes. The right trade for a page whose
            # correctness we keep changing.
            cache_control=[s3deploy.CacheControl.from_string("no-cache")],
        )

        CfnOutput(self, "SiteUrl", value=f"https://{domain_name}")
        CfnOutput(self, "DistributionDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
