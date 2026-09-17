"""CDK stack for the ExpensifyAI PoC.

Deployed into ap-southeast-1 (Singapore) inside the shared Mobil80 account, so
every resource carries an ``expensifyai-`` / ``ExpensifyAI`` name and a Project
tag. Nothing here is account-scoped: no Route 53 zones, no IAM OIDC provider,
no shared bootstrap resources are touched, which keeps this stack liftable into
a dedicated AWS account later with only a redeploy.
"""
from __future__ import annotations

from aws_cdk import (
    Aws,
    CfnOutput,
    Fn,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_lambda_event_sources as lambda_sources,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_s3_notifications as s3n,
    aws_ses as ses,
    aws_ses_actions as ses_actions,
    aws_secretsmanager as secretsmanager,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as targets,
)
from constructs import Construct

# OpenAI model. Overridable with `-c model_id=...` without touching code.
#
# gpt-5.6-sol: flagship-tier judgment at $4/$20 per MTok, vs $10/$50 for
# gpt-6-astra. The hard part of this workload is not perception but knowledge -
# recognising that "Sula Chenin Blanc" is wine and "Old Monk" is rum, while
# "Kokum Kefir" is not alcoholic - and that is where a weaker tier slips.
# gpt-5.6-terra ($2/$12) is the cost-down once accuracy is proven on real
# receipts; gpt-5.6-luna is too weak for the categorisation call.
DEFAULT_MODEL_ID = "gpt-5.6-sol"


class ExpensifyAIStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        domain_name: str | None = None,
        hosted_zone_id: str | None = None,
        zone_name: str = "expenze.ai",
        otp_sender: str = "noreply@expenze.ai",
        root_admin_email: str = "riyad@mobil80.com",
        site_origins: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        site_origins = site_origins or [f"https://{zone_name}"]

        # ------------------------------------------------------------------
        # State
        # ------------------------------------------------------------------
        table = dynamodb.Table(
            self,
            "ExpensesTable",
            table_name="ExpensifyAI-Expenses",
            partition_key=dynamodb.Attribute(
                name="expense_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # PoC: tear the table down with the stack rather than leaving an
            # orphan behind in an account shared with a dozen live products.
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Credentials
        # ------------------------------------------------------------------
        api_key_secret = secretsmanager.Secret(
            self,
            "OpenAiApiKey",
            secret_name="expensifyai/openai-api-key",
            description="OpenAI API key for ExpensifyAI - value set out-of-band, never in code",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Dependencies layer
        # ------------------------------------------------------------------
        # Built by scripts/build_layer.sh, which pulls prebuilt manylinux
        # wheels rather than compiling - there is no Docker on this machine and
        # the only local Python is 3.9, so the usual bundling path is closed.
        deps_layer = lambda_.LayerVersion(
            self,
            "DepsLayer",
            layer_version_name="expensifyai-deps",
            code=lambda_.Code.from_asset("build/layer"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            description="openai SDK and its dependencies",
        )

        # ------------------------------------------------------------------
        # Compute
        # ------------------------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "AuditorLogs",
            log_group_name="/aws/lambda/expensifyai-auditor",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        auditor = lambda_.Function(
            self,
            "AuditorFunction",
            function_name="expensifyai-auditor",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            layers=[deps_layer],
            memory_size=1024,
            # Generous: two model passes over a receipt image. The API Gateway
            # integration caps the *client-visible* wait at 29s regardless -
            # see the REST API note below.
            timeout=Duration.seconds(120),
            log_group=log_group,
            environment={
                "EXPENSES_TABLE": table.table_name,
                "OPENAI_SECRET_ARN": api_key_secret.secret_arn,
                "OPENAI_MODEL": model_id,
            },
        )

        table.grant_read_write_data(auditor)

        # The OpenAI API key. CDK owns the *resource*; the value is set
        # out-of-band so the key never passes through source, CloudFormation
        # parameters, or a terminal transcript. Until it is set the Lambda
        # returns a clear 503 rather than failing obscurely.
        api_key_secret.grant_read(auditor)

        # ------------------------------------------------------------------
        # Entrypoint
        # ------------------------------------------------------------------
        # REST (not HTTP) API deliberately: HTTP APIs cap integration timeout
        # at 30s with no way to raise it, whereas a regional REST API's 29s
        # default can be lifted via a service quota increase if the two-pass
        # audit turns out to run long.
        api = apigw.LambdaRestApi(
            self,
            "Api",
            rest_api_name="expensifyai-api",
            handler=auditor,
            proxy=False,
            # ap-southeast-1 already hosts 11 REST APIs belonging to other
            # Mobil80 products, and its account-level cloudwatchRoleArn is
            # currently unset. CDK would set it by default - a per-region,
            # account-wide mutation affecting every one of those APIs. We give
            # that up rather than touch shared state; Lambda's own log group
            # covers everything this PoC needs to debug.
            cloud_watch_role=False,
            deploy_options=apigw.StageOptions(
                stage_name="poc",
                throttling_rate_limit=10,
                throttling_burst_limit=20,
            ),
        )

        expenses = api.root.add_resource("expenses")
        expenses.add_method("POST")               # submit a receipt for audit
        expenses.add_resource("{expense_id}").add_method("GET")  # fetch a verdict

        # ------------------------------------------------------------------
        # Email OTP sign-in
        # ------------------------------------------------------------------
        auth_table = dynamodb.Table(
            self,
            "AuthCodesTable",
            table_name="Expenze-AuthCodes",
            partition_key=dynamodb.Attribute(
                name="email", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Codes delete themselves. Nothing here should outlive its 10-minute
            # window, and an expired code left lying around is a liability.
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Who is allowed to receive a sign-in code. Without these, the OTP
        # endpoint would mail anyone who types an address into it.
        # A *membership* table, not a user table: one row per (person,
        # organisation). Somebody can belong to more than one org, and an
        # inbound receipt resolves to the one they joined most recently.
        users_table = dynamodb.Table(
            self,
            "MembershipsTable",
            table_name="Expenze-Memberships",
            partition_key=dynamodb.Attribute(
                name="email", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="org_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # WhatsApp identifies a sender only by number, so that has to be
        # queryable without knowing the email.
        users_table.add_global_secondary_index(
            index_name="by-mobile",
            partition_key=dynamodb.Attribute(
                name="mobile", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        orgs_table = dynamodb.Table(
            self,
            "OrgsTable",
            table_name="Expenze-Orgs",
            partition_key=dynamodb.Attribute(
                name="org_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # The auditor is declared above this table, so it picks it up here:
        # read-only, and for one field. A receipt with no currency printed on
        # it is read as the organisation's default, and that has to be looked
        # up when the receipt is audited rather than frozen into a prompt.
        orgs_table.grant_read_data(auditor)
        auditor.add_environment("ORGS_TABLE", orgs_table.table_name)

        # BMS administrators. Deliberately a different table from Expenze-Users:
        # customer sign-up writes there and can never write here, so no amount
        # of self-service registration grants back-office access.
        admins_table = dynamodb.Table(
            self,
            "AdminsTable",
            table_name="Expenze-Admins",
            partition_key=dynamodb.Attribute(
                name="email", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Every credit movement, attributed. Credits are money; a balance that
        # changed with no record of who changed it is not auditable.
        ledger_table = dynamodb.Table(
            self,
            "CreditLedgerTable",
            table_name="Expenze-CreditLedger",
            partition_key=dynamodb.Attribute(
                name="org_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="ts", type=dynamodb.AttributeType.NUMBER
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        intake_table = dynamodb.Table(
            self,
            "IntakeTable",
            table_name="Expenze-Submissions",
            partition_key=dynamodb.Attribute(
                name="submission_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # The stream is what actually reads the receipts. Every channel
            # ends at a row here; the auditor picks them up from this stream
            # rather than being called by each adapter, because a webhook
            # cannot wait the tens of seconds two model passes take.
            stream=dynamodb.StreamViewType.NEW_IMAGE,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Machine credentials for the third-party intake endpoint. Keyed by the
        # hash so verification is one read of the exact item rather than a scan
        # comparing candidates - no timing signal, and no plaintext anywhere.
        api_keys_table = dynamodb.Table(
            self,
            "ApiKeysTable",
            table_name="Expenze-ApiKeys",
            partition_key=dynamodb.Attribute(
                name="key_hash", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # One row per idempotency key an integration has used, so a retried
        # submission returns the original claim instead of buying a second one.
        # They delete themselves: this is a short-term memory of recent
        # requests, not a permanent record of every receipt ever sent.
        idempotency_table = dynamodb.Table(
            self,
            "IdempotencyTable",
            table_name="Expenze-Idempotency",
            partition_key=dynamodb.Attribute(
                name="idem_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # One row per receipt fingerprint, so "have we seen this before?" is an
        # exact get rather than a scan of every claim the organisation has ever
        # made. Conditional writes make the first claimant the owner even when
        # two copies arrive at the same instant.
        fingerprints_table = dynamodb.Table(
            self,
            "FingerprintsTable",
            table_name="Expenze-Fingerprints",
            partition_key=dynamodb.Attribute(
                name="fingerprint", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Every decision a person made about somebody's money, appended and
        # never touched again.
        #
        # A table of its own rather than fields on the claim, because fields on
        # the claim are what we already had and they are not a record: there is
        # one set of them and each decision overwrites the last. Approve, then
        # reject, and the approval is gone - reopening a claim deletes it
        # outright. "Who approved this before it was refused" had no answer.
        #
        # Keyed by organisation and sorted by time, so a month of one
        # customer's decisions is a query rather than a scan, and one
        # customer's log cannot be read from another's grant. Entries expire
        # after seven years - long enough to outlive the financial year they
        # document and the audit that follows it.
        #
        # Point-in-time recovery is on here and nowhere else in this stack.
        # Everything else can be rebuilt from the receipts; this is the only
        # table whose whole value is that it cannot be rewritten after the
        # fact, and a restore is the last defence if something ever does.
        audit_table = dynamodb.Table(
            self,
            "AuditTable",
            table_name="Expenze-AuditLog",
            partition_key=dynamodb.Attribute(
                name="org_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="ts", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # "Everything that happened to this one claim", for the history on the
        # claim page. An index rather than a second copy of each entry written
        # under a per-claim key: two writes can half-fail and leave the two
        # views of one decision disagreeing, which in a table whose only job is
        # to be trustworthy is the worst failure available. An index is
        # maintained by DynamoDB from the row itself and cannot diverge from it.
        #
        # Not a filter on the org query either. That reads the whole partition
        # and discards most of it - fine this year, and a scan of seven years of
        # an organisation's decisions to render one claim's history by the time
        # it matters.
        audit_table.add_global_secondary_index(
            index_name="by-claim",
            partition_key=dynamodb.Attribute(
                name="submission_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="ts", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Platform-wide settings: channel numbers and credit pricing. A single
        # row, but a table of its own - these are commercial values that change
        # on a different cadence from anything else and want their own audit.
        settings_table = dynamodb.Table(
            self,
            "SettingsTable",
            table_name="Expenze-Settings",
            partition_key=dynamodb.Attribute(
                name="key", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Originals
        # ------------------------------------------------------------------
        # The receipt as it arrived, whatever channel it came down. Its own
        # bucket, not a prefix on the inbound-mail one, for two reasons:
        #
        # * Raw messages are transient evidence and expire at 90 days. A
        #   receipt is the document behind a payment and has to outlive that.
        # * The read path is reachable from the console. Pointing it at the
        #   mail bucket would put the full text of every message ever sent to
        #   receipts@expenze.ai inside the same grant.
        receipts_bucket = s3.Bucket(
            self,
            "ReceiptsBucket",
            bucket_name=f"expenze-receipts-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            # The browser PUTs an upload straight here against a presigned URL,
            # and reads the original back the same way, so the site's origin
            # has to be allowed on the bucket itself.
            cors=[
                s3.CorsRule(
                    allowed_origins=site_origins,
                    allowed_methods=[
                        s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.HEAD
                    ],
                    allowed_headers=["*"],
                    exposed_headers=["ETag"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    # Read constantly for a fortnight while a claim is settled,
                    # then almost never - but "almost never" is exactly when an
                    # auditor asks, so they move tier rather than disappear.
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(90),
                        )
                    ],
                    # Long enough to cover a tax audit, finite because these are
                    # photographs of people's meals and travel.
                    expiration=Duration.days(1095),
                )
            ],
        )

        # One row per attempted purchase, written when the order is created and
        # claimed exactly once when it is paid. Keyed by the Razorpay order id
        # because that is the identifier both the browser callback and the
        # webhook carry, and the conditional write on it is what makes
        # crediting exactly-once rather than probably-once.
        purchases_table = dynamodb.Table(
            self,
            "PurchasesTable",
            table_name="Expenze-Purchases",
            partition_key=dynamodb.Attribute(
                name="order_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Razorpay keys. The value is set out-of-band like every other
        # credential here, and test versus live is simply which key is in it -
        # so going live is a secret update, not a deploy.
        rzp_secret = secretsmanager.Secret(
            self,
            "RazorpayKeys",
            secret_name="expenze/razorpay",
            description=("Razorpay: key_id, key_secret, webhook_secret. "
                         "Test keys carry rzp_test_; live carry rzp_live_."),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Signs session tokens. Generated by CloudFormation and never seen by a
        # human, so it cannot leak through a terminal or a commit.
        session_secret = secretsmanager.Secret(
            self,
            "SessionSigningKey",
            secret_name="expenze/session-signing-key",
            description="HMAC key for Expenze session tokens",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64, exclude_punctuation=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        auth_fn = lambda_.Function(
            self,
            "AuthFunction",
            function_name="expenze-auth",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="auth.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            # No dependency layer: this handler needs only boto3, which the
            # runtime ships. Keeps the cold start off the sign-in path.
            memory_size=512,
            timeout=Duration.seconds(15),
            environment={
                "AUTH_TABLE": auth_table.table_name,
                "USERS_TABLE": users_table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "ADMINS_TABLE": admins_table.table_name,
                "INTAKE_TABLE": intake_table.table_name,
                "RECEIPTS_BUCKET": receipts_bucket.bucket_name,
                "ROOT_ADMIN_EMAIL": root_admin_email,
                "MOBILE_INDEX": "by-mobile",
                # SES is verified in us-east-1, not this region: ap-southeast-1
                # is still in the SES sandbox, and us-east-1 has production
                # access plus every other Mobil80 domain.
                "SES_REGION": "us-east-1",
                "OTP_SENDER": otp_sender,
                "SESSION_SECRET_ARN": session_secret.secret_arn,
                "ALLOWED_ORIGINS": ",".join(site_origins),
            },
        )
        auth_table.grant_read_write_data(auth_fn)
        users_table.grant_read_write_data(auth_fn)
        orgs_table.grant_read_write_data(auth_fn)
        admins_table.grant_read_data(auth_fn)   # read-only: auth never grants admin
        session_secret.grant_read(auth_fn)
        # Portal submissions run the same intake gate as every other channel,
        # and the receipt viewer reads the row that authorises the original.
        intake_table.grant_read_write_data(auth_fn)
        api_keys_table.grant_read_write_data(auth_fn)
        idempotency_table.grant_read_write_data(auth_fn)
        fingerprints_table.grant_read_write_data(auth_fn)
        auth_fn.add_environment("API_KEYS_TABLE", api_keys_table.table_name)
        auth_fn.add_environment("IDEMPOTENCY_TABLE", idempotency_table.table_name)
        auth_fn.add_environment("FINGERPRINTS_TABLE", fingerprints_table.table_name)
        # Append and read. Deliberately not `grant_read_write_data`, which would
        # hand this function UpdateItem and DeleteItem as well. Append-only is
        # the entire value of this table, and a property worth enforcing in IAM
        # rather than trusting to the fact that nothing in audit.py calls them:
        # a future edit that reached for an update here would fail at deploy
        # time instead of quietly rewriting somebody's audit trail.
        audit_table.grant(auth_fn, "dynamodb:PutItem", "dynamodb:Query")
        auth_fn.add_environment("AUDIT_TABLE", audit_table.table_name)
        receipts_bucket.grant_read_write(auth_fn)
        # Read-only: the console shows the channel addresses, BMS sets them.
        # Read/write, not read: auth.py converts foreign-currency claims for
        # the budget check and caches the rate table in this row - see fx.py.
        settings_table.grant_read_write_data(auth_fn)
        auth_fn.add_environment("SETTINGS_TABLE", settings_table.table_name)

        # Buying credits: create the order, and credit on the browser callback.
        rzp_secret.grant_read(auth_fn)
        purchases_table.grant_read_write_data(auth_fn)
        ledger_table.grant_read_write_data(auth_fn)
        auth_fn.add_environment("RZP_SECRET_ARN", rzp_secret.secret_arn)
        auth_fn.add_environment("PURCHASES_TABLE", purchases_table.table_name)
        auth_fn.add_environment("LEDGER_TABLE", ledger_table.table_name)
        # Crediting a paid order writes the balance, so payments.py needs the
        # orgs table under the name it looks for.
        auth_fn.add_environment("ORGS_TABLE", orgs_table.table_name)

        # ------------------------------------------------------------------
        # Razorpay webhook
        # ------------------------------------------------------------------
        # Its own function on purpose. The endpoint is public and
        # unauthenticated, so it is the one part of the system that must not
        # hold the session signing key: nothing reachable without a session
        # should be able to mint one.
        rzp_fn = lambda_.Function(
            self,
            "RazorpayWebhookFunction",
            function_name="expenze-razorpay-webhook",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="rzp_webhook.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            memory_size=512,
            timeout=Duration.seconds(20),
            environment={
                "RZP_SECRET_ARN": rzp_secret.secret_arn,
                "PURCHASES_TABLE": purchases_table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "LEDGER_TABLE": ledger_table.table_name,
            },
        )
        rzp_secret.grant_read(rzp_fn)
        purchases_table.grant_read_write_data(rzp_fn)
        orgs_table.grant_read_write_data(rzp_fn)
        ledger_table.grant_read_write_data(rzp_fn)

        rzp_res = api.root.add_resource("razorpay").add_resource("webhook")
        rzp_res.add_method("POST", apigw.LambdaIntegration(rzp_fn))

        CfnOutput(self, "RazorpayWebhook", value=api.url_for_path("/razorpay/webhook"))
        CfnOutput(self, "RazorpaySecret", value=rzp_secret.secret_name)

        # Scoped to the one verified identity, in the one region that has it.
        auth_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    f"arn:{Aws.PARTITION}:ses:us-east-1:{self.account}:identity/{zone_name}"
                ],
            )
        )

        # ------------------------------------------------------------------
        # BMS - the operator's back office
        # ------------------------------------------------------------------
        admin_fn = lambda_.Function(
            self,
            "AdminFunction",
            function_name="expenze-admin",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="admin.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            memory_size=512,
            timeout=Duration.seconds(20),
            environment={
                "USERS_TABLE": users_table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "ADMINS_TABLE": admins_table.table_name,
                "LEDGER_TABLE": ledger_table.table_name,
                "SETTINGS_TABLE": settings_table.table_name,
                "SESSION_SECRET_ARN": session_secret.secret_arn,
                "ROOT_ADMIN_EMAIL": root_admin_email,
                "SES_REGION": "us-east-1",
                "OTP_SENDER": otp_sender,
                "ALLOWED_ORIGINS": ",".join(site_origins),
            },
        )
        users_table.grant_read_data(admin_fn)
        # BMS emails an administrator when their access is granted.
        admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    f"arn:{Aws.PARTITION}:ses:us-east-1:{self.account}:identity/{zone_name}"
                ],
            )
        )
        orgs_table.grant_read_write_data(admin_fn)
        admins_table.grant_read_write_data(admin_fn)
        ledger_table.grant_read_write_data(admin_fn)
        settings_table.grant_read_write_data(admin_fn)
        session_secret.grant_read(admin_fn)
        # Write as well as read: BMS is where the keys are entered.
        rzp_secret.grant_read(admin_fn)
        rzp_secret.grant_write(admin_fn)
        admin_fn.add_environment("RZP_SECRET_ARN", rzp_secret.secret_arn)

        # ------------------------------------------------------------------
        # Inbound receipts - email, WhatsApp, API
        # ------------------------------------------------------------------

        # Its own function, and so its own execution role. Intake needs to read
        # memberships and move credits; it has no business holding the SES
        # sending rights or the auth-codes write access that the sign-in
        # function needs. (These briefly shared a role while the account sat at
        # the IAM 1000-role quota; that limit is now 2000.)
        intake_fn = lambda_.Function(
            self,
            "IntakeFunction",
            function_name="expenze-intake",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="intake.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            memory_size=512,
            timeout=Duration.seconds(20),
            environment={
                "USERS_TABLE": users_table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "INTAKE_TABLE": intake_table.table_name,
                "MOBILE_INDEX": "by-mobile",
            },
        )
        users_table.grant_read_data(intake_fn)
        orgs_table.grant_read_write_data(intake_fn)
        intake_table.grant_read_write_data(intake_fn)
        api_keys_table.grant_read_write_data(intake_fn)
        idempotency_table.grant_read_write_data(intake_fn)
        fingerprints_table.grant_read_write_data(intake_fn)
        intake_fn.add_environment("API_KEYS_TABLE", api_keys_table.table_name)
        intake_fn.add_environment("IDEMPOTENCY_TABLE", idempotency_table.table_name)
        intake_fn.add_environment("FINGERPRINTS_TABLE", fingerprints_table.table_name)
        intake_fn.add_environment("SETTINGS_TABLE", settings_table.table_name)
        settings_table.grant_read_data(intake_fn)
        users_table.grant_read_data(intake_fn)
        intake_fn.add_environment("USERS_TABLE", users_table.table_name)
        intake_fn.add_environment("OTP_SENDER", otp_sender)
        intake_fn.add_environment("INTAKE_ADDRESS", f"receipts@{zone_name}")
        intake_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    f"arn:{Aws.PARTITION}:ses:us-east-1:{self.account}:identity/{zone_name}"
                ],
            )
        )

        intake_integration = apigw.LambdaIntegration(intake_fn)
        intake_res = api.root.add_resource("intake")
        for channel in ("email", "whatsapp", "api"):
            intake_res.add_resource(channel).add_method("POST", intake_integration)

        CfnOutput(self, "IntakeEndpoint", value=api.url_for_path("/intake"))

        # ------------------------------------------------------------------
        # The auditor worker
        # ------------------------------------------------------------------
        # Reads what the channels queue. Its own function because it is the
        # only thing here that is slow and expensive: two model passes over a
        # photograph, on a timeout no webhook could tolerate.
        auditor_worker = lambda_.Function(
            self,
            "AuditorWorker",
            function_name="expenze-auditor-worker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="auditor_worker.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            layers=[deps_layer],
            memory_size=1024,
            # Two model passes over a photograph, and the slow one is a long
            # handwritten bill: a 23-line grocery receipt ran past 120s and was
            # killed mid-audit. A timeout here is not a retry - the function is
            # killed where it stands, so nothing releases the claim and the
            # receipt reads "Being read..." until somebody goes looking. The
            # headroom is cheap; the stranded claim is not.
            timeout=Duration.seconds(300),
            environment={
                "INTAKE_TABLE": intake_table.table_name,
                "RECEIPTS_BUCKET": receipts_bucket.bucket_name,
                "EXPENSES_TABLE": table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "OPENAI_SECRET_ARN": api_key_secret.secret_arn,
                "OPENAI_MODEL": model_id,
            },
        )
        intake_table.grant_read_write_data(auditor_worker)
        intake_table.grant_stream_read(auditor_worker)
        fingerprints_table.grant_read_write_data(auditor_worker)
        auditor_worker.add_environment("FINGERPRINTS_TABLE", fingerprints_table.table_name)

        # The worker is what finally knows the outcome, so it is what tells the
        # person who sent the receipt. That needs the identity tables to find
        # them, and the two channels to reach them on.
        users_table.grant_read_data(auditor_worker)
        auditor_worker.add_environment("USERS_TABLE", users_table.table_name)
        # The rate this claim is converted at for budget purposes is stamped
        # on it when it is audited, and the rate table is cached here.
        settings_table.grant_read_write_data(auditor_worker)
        auditor_worker.add_environment("SETTINGS_TABLE", settings_table.table_name)
        auditor_worker.add_environment("OTP_SENDER", otp_sender)
        auditor_worker.add_environment("INTAKE_ADDRESS", f"receipts@{zone_name}")
        auditor_worker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    f"arn:{Aws.PARTITION}:ses:us-east-1:{self.account}:identity/{zone_name}"
                ],
            )
        )
        receipts_bucket.grant_read(auditor_worker)
        orgs_table.grant_read_data(auditor_worker)
        table.grant_read_write_data(auditor_worker)
        api_key_secret.grant_read(auditor_worker)

        auditor_worker.add_event_source(
            lambda_sources.DynamoEventSource(
                intake_table,
                starting_position=lambda_.StartingPosition.TRIM_HORIZON,
                # One receipt per invocation: a batch that fails part-way would
                # otherwise re-run the model over the receipts that already
                # succeeded, and each of those costs money.
                batch_size=1,
                retry_attempts=2,
                report_batch_item_failures=False,
            )
        )

        # ------------------------------------------------------------------
        # The finance digest
        # ------------------------------------------------------------------
        # One email per window listing what came in, rather than one per
        # receipt. A team filing a month on a Friday would otherwise send forty
        # emails, and the fortieth is read by nobody - which makes the first
        # thirty-nine worthless too.
        #
        # The only thing in this stack on a clock, and deliberately so: every
        # other alert here fires on the write that changes the state, because
        # the event is the thing worth reporting. A digest is the opposite - its
        # purpose is to wait and see whether more arrives - so the last batch of
        # a quiet afternoon needs something other than the next receipt to push
        # it out.
        digest_fn = lambda_.Function(
            self,
            "FinanceDigest",
            function_name="expenze-digest",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="digest.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            layers=[deps_layer],
            memory_size=512,
            timeout=Duration.seconds(120),
            environment={
                "INTAKE_TABLE": intake_table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "USERS_TABLE": users_table.table_name,
                "OTP_SENDER": otp_sender,
                "INTAKE_ADDRESS": f"receipts@{zone_name}",
            },
        )
        intake_table.grant_read_data(digest_fn)
        users_table.grant_read_data(digest_fn)
        # Read to find the organisations, write to move each one's high-water
        # mark. The mark is the only thing this function changes.
        orgs_table.grant_read_write_data(digest_fn)
        digest_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    f"arn:{Aws.PARTITION}:ses:us-east-1:{self.account}:identity/{zone_name}"
                ],
            )
        )

        # Fifteen minutes: long enough that a burst arrives as one message,
        # short enough that a single receipt on a quiet morning is not stale by
        # the time anybody hears about it.
        events.Rule(
            self,
            "FinanceDigestSchedule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            targets=[events_targets.LambdaFunction(digest_fn)],
        )

        # ------------------------------------------------------------------
        # Inbound email - receipts@expenze.ai
        # ------------------------------------------------------------------
        # Deliberately in ap-southeast-1, not us-east-1. SES allows exactly one
        # ACTIVE receipt rule set per region, and us-east-1's belongs to
        # slotzapp (alumnye.com). Activating ours there would stop their mail
        # dead. Singapore had no active rule set, so Expenze owns inbound here
        # and displaces nobody.
        mail_bucket = s3.Bucket(
            self,
            "InboundMailBucket",
            bucket_name=f"expenze-inbound-mail-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                # Raw messages are evidence for "why was this claim created?",
                # not a permanent store. They contain personal data.
                s3.LifecycleRule(expiration=Duration.days(90))
            ],
        )

        mail_fn = lambda_.Function(
            self,
            "MailFunction",
            function_name="expenze-mail",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="mail.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            memory_size=1024,
            timeout=Duration.seconds(60),
            environment={
                "RECEIPTS_BUCKET": receipts_bucket.bucket_name,
                "INTAKE_ADDRESS": f"receipts@{zone_name}",
                # It calls the intake gate in-process, so it needs intake's view.
                "USERS_TABLE": users_table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "INTAKE_TABLE": intake_table.table_name,
                "MOBILE_INDEX": "by-mobile",
            },
        )
        # Read only on the mail bucket: it lifts the attachment out of a raw
        # message and writes it to the receipts bucket. It has no reason to be
        # able to alter the inbound record it is reading from.
        # It acknowledges the sender on their own thread, from the intake
        # address, so a reply carrying the right attachment is simply a new
        # submission. Scoped to the one verified identity, as everywhere else.
        mail_fn.add_environment("SES_REGION", "us-east-1")
        mail_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[
                    f"arn:{Aws.PARTITION}:ses:us-east-1:{self.account}:identity/{zone_name}"
                ],
            )
        )
        mail_bucket.grant_read(mail_fn)
        receipts_bucket.grant_read_write(mail_fn)
        users_table.grant_read_data(mail_fn)
        orgs_table.grant_read_write_data(mail_fn)
        intake_table.grant_read_write_data(mail_fn)
        api_keys_table.grant_read_write_data(mail_fn)
        idempotency_table.grant_read_write_data(mail_fn)
        fingerprints_table.grant_read_write_data(mail_fn)
        mail_fn.add_environment("API_KEYS_TABLE", api_keys_table.table_name)
        mail_fn.add_environment("IDEMPOTENCY_TABLE", idempotency_table.table_name)
        mail_fn.add_environment("FINGERPRINTS_TABLE", fingerprints_table.table_name)
        # A reply answering a reviewer's question is a step in the claim's
        # history, so it belongs in the log with the decisions around it.
        # Append only, exactly as the console has it - see the table above.
        audit_table.grant(mail_fn, "dynamodb:PutItem")
        mail_fn.add_environment("AUDIT_TABLE", audit_table.table_name)

        # S3 event rather than SES's direct Lambda action: that action caps the
        # message size it can hand over, and receipts are photographs.
        mail_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(mail_fn),
            s3.NotificationKeyFilter(prefix="inbox/"),
        )

        rule_set = ses.ReceiptRuleSet(
            self,
            "InboundRuleSet",
            receipt_rule_set_name="expenze-inbound",
            rules=[
                ses.ReceiptRuleOptions(
                    receipt_rule_name="expenze-receipts",
                    recipients=[f"receipts@{zone_name}", otp_sender],
                    scan_enabled=True,   # spam and virus verdicts from SES
                    actions=[
                        ses_actions.S3(bucket=mail_bucket, object_key_prefix="inbox/")
                    ],
                )
            ],
        )

        CfnOutput(self, "MailBucketName", value=mail_bucket.bucket_name)
        CfnOutput(self, "ReceiptsBucketName", value=receipts_bucket.bucket_name)
        # ------------------------------------------------------------------
        # WhatsApp intake
        # ------------------------------------------------------------------
        # Everything identifying the number lives in this secret, so moving
        # from the shared CloudMeter number to a dedicated Expenze one is a
        # secret update plus a webhook re-registration - not a deploy.
        wa_secret = secretsmanager.Secret(
            self,
            "WhatsAppConfig",
            secret_name="expenze/whatsapp",
            description="Meta WhatsApp Cloud API: wabaId, phoneNumberId, accessToken, appSecret, webhookVerifyToken",
            removal_policy=RemovalPolicy.DESTROY,
        )

        wa_fn = lambda_.Function(
            self,
            "WhatsAppFunction",
            function_name="expenze-whatsapp",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="whatsapp.lambda_handler",
            code=lambda_.Code.from_asset("lambda_src"),
            memory_size=1024,
            timeout=Duration.seconds(30),
            environment={
                "WA_SECRET_ARN": wa_secret.secret_arn,
                "RECEIPTS_BUCKET": receipts_bucket.bucket_name,
                # It runs the intake gate in-process.
                "USERS_TABLE": users_table.table_name,
                "ORGS_TABLE": orgs_table.table_name,
                "INTAKE_TABLE": intake_table.table_name,
                "MOBILE_INDEX": "by-mobile",
            },
        )
        wa_secret.grant_read(wa_fn)

        # auth_fn is declared earlier, so it picks the secret up here: it sends
        # the number-verification code over WhatsApp, to the number being
        # claimed - proving control of that account, which is the thing being
        # authorised.
        auth_fn.add_environment("WA_SECRET_ARN", wa_secret.secret_arn)
        wa_secret.grant_read(auth_fn)
        receipts_bucket.grant_read_write(wa_fn)
        users_table.grant_read_data(wa_fn)
        orgs_table.grant_read_write_data(wa_fn)
        intake_table.grant_read_write_data(wa_fn)
        api_keys_table.grant_read_write_data(wa_fn)
        idempotency_table.grant_read_write_data(wa_fn)
        fingerprints_table.grant_read_write_data(wa_fn)

        # Declared here rather than beside the worker's other grants, because
        # the secret does not exist until this point in the stack.
        wa_secret.grant_read(auditor_worker)
        auditor_worker.add_environment("WA_SECRET_ARN", wa_secret.secret_arn)
        wa_secret.grant_read(intake_fn)
        intake_fn.add_environment("WA_SECRET_ARN", wa_secret.secret_arn)
        wa_fn.add_environment("API_KEYS_TABLE", api_keys_table.table_name)
        wa_fn.add_environment("IDEMPOTENCY_TABLE", idempotency_table.table_name)
        wa_fn.add_environment("FINGERPRINTS_TABLE", fingerprints_table.table_name)
        # Same as the mail function: an answer sent back on WhatsApp is the
        # same event as one sent back by email, and lands in the same log.
        audit_table.grant(wa_fn, "dynamodb:PutItem")
        wa_fn.add_environment("AUDIT_TABLE", audit_table.table_name)

        wa_integration = apigw.LambdaIntegration(wa_fn)
        wa_res = api.root.add_resource("whatsapp").add_resource("webhook")
        wa_res.add_method("GET", wa_integration)    # Meta's subscription challenge
        wa_res.add_method("POST", wa_integration)   # inbound messages

        CfnOutput(self, "WhatsAppWebhook", value=api.url_for_path("/whatsapp/webhook"))
        CfnOutput(self, "WhatsAppSecret", value=wa_secret.secret_name)

        CfnOutput(self, "MailRuleSet", value=rule_set.receipt_rule_set_name)
        CfnOutput(self, "MailAddress", value=f"receipts@{zone_name}")

        admin_integration = apigw.LambdaIntegration(admin_fn)
        admin_res = api.root.add_resource(
            "admin",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=site_origins,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )
        admin_res.add_resource("orgs").add_method("GET", admin_integration)
        admin_res.add_resource("credits").add_method("POST", admin_integration)
        settings_res = admin_res.add_resource("settings")
        settings_res.add_method("GET", admin_integration)
        settings_res.add_method("POST", admin_integration)
        admins_res = admin_res.add_resource("admins")
        admins_res.add_method("GET", admin_integration)
        admins_res.add_method("POST", admin_integration)
        admin_res.add_resource("revoke").add_method("POST", admin_integration)
        # Payment keys are entered in BMS and written straight to Secrets
        # Manager - never into the settings table, which is returned in full
        # to the back office and would put a key secret in a response body.
        payments_res = admin_res.add_resource("payments")
        payments_res.add_method("GET", admin_integration)
        payments_res.add_method("POST", admin_integration)

        CfnOutput(self, "AdminEndpoint", value=api.url_for_path("/admin"))

        # One permission for the whole API, not one per method.
        #
        # LambdaIntegration adds an AWS::Lambda::Permission per method by
        # default, each naming that method's exact path. At around twenty-five
        # routes the function's resource policy passed AWS's 20 KB limit and
        # every deploy failed - on the *next* route added, with an error that
        # names a size rather than a cause. Importing the function with
        # `skip_permissions` stops CDK writing them, and a single wildcard
        # statement below covers every route this API will ever have.
        auth_for_api = lambda_.Function.from_function_attributes(
            self,
            "AuthFunctionForApi",
            function_arn=auth_fn.function_arn,
            same_environment=False,
            skip_permissions=True,
        )
        auth_integration = apigw.LambdaIntegration(auth_for_api)
        auth_fn.add_permission(
            "ApiGatewayInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=Fn.join("", [
                f"arn:{Aws.PARTITION}:execute-api:{self.region}:{self.account}:",
                api.rest_api_id, "/*/*/*",
            ]),
        )
        auth = api.root.add_resource(
            "auth",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=site_origins,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )
        auth.add_resource("signup").add_method("POST", auth_integration)
        auth.add_resource("invite").add_method("POST", auth_integration)
        auth.add_resource("me").add_method("POST", auth_integration)
        org_res = auth.add_resource("org")
        org_res.add_method("POST", auth_integration)
        org_res.add_resource("groups").add_method("POST", auth_integration)
        # Budgets are a reporting control, never an intake gate: nothing on the
        # receipt path reads them. See budgets.py.
        org_res.add_resource("budgets").add_method("POST", auth_integration)
        # The expense policy itself, which until now lived only in the browser.
        org_res.add_resource("rules").add_method("POST", auth_integration)

        # Settling or rejecting a claim, and telling the person who claimed.
        # A rejection carries a reason or it is refused - see notify.py.
        claim_res = auth.add_resource("claim")
        claim_res.add_resource("outcome").add_method("POST", auth_integration)
        claim_res.add_resource("review").add_method("POST", auth_integration)
        claim_res.add_resource("answer").add_method("POST", auth_integration)
        # A reviewer saying what an expense actually is, when nothing in the
        # policy covers what the model read off the bill.
        claim_res.add_resource("retype").add_method("POST", auth_integration)
        member_res = auth.add_resource("member")
        member_res.add_method("POST", auth_integration)
        member_res.add_resource("groups").add_method("POST", auth_integration)
        member_res.add_resource("transfer").add_method("POST", auth_integration)
        wa_auth = auth.add_resource("whatsapp")
        wa_auth.add_resource("add").add_method("POST", auth_integration)
        wa_auth.add_resource("verify").add_method("POST", auth_integration)
        # The original receipt: a link to read one, and the two steps that put
        # a new one in - the bytes go browser-to-S3 and never through here.
        # Buying credits. The order is created server-side so the amount in the
        # checkout dialog is one we chose, never one the page asked for.
        # What the console reads: the organisation's receipts and its people.
        auth.add_resource("pricing").add_method("POST", auth_integration)
        auth.add_resource("apikey").add_method("POST", auth_integration)
        auth.add_resource("submissions").add_method("POST", auth_integration)
        auth.add_resource("people").add_method("POST", auth_integration)
        auth.add_resource("audit").add_method("POST", auth_integration)

        credits_res = auth.add_resource("credits")
        credits_res.add_resource("order").add_method("POST", auth_integration)
        credits_res.add_resource("verify").add_method("POST", auth_integration)
        credits_res.add_resource("ledger").add_method("POST", auth_integration)

        receipt_res = auth.add_resource("receipt")
        receipt_res.add_resource("view").add_method("POST", auth_integration)
        receipt_res.add_resource("upload").add_method("POST", auth_integration)
        receipt_res.add_resource("submit").add_method("POST", auth_integration)
        auth.add_resource("request").add_method("POST", auth_integration)
        auth.add_resource("verify").add_method("POST", auth_integration)

        CfnOutput(self, "AuthEndpoint", value=api.url_for_path("/auth"))

        # ------------------------------------------------------------------
        # Custom domain
        # ------------------------------------------------------------------
        # `expenze.ai` is already delegated to zone Z08768761ELNU80IGOYLG in
        # this account, so a subdomain is a plain record in that zone - not a
        # zone of its own. A child zone only earns its keep when DNS control
        # for the subdomain has to be handed to a different team or account.
        #
        # The zone is IMPORTED by id, never declared: declaring a zone that
        # already exists is what produced the duplicate cloudmeter.io zone here.
        #
        # Enable with `-c api_domain=api.expenze.ai`. Without it the
        # execute-api URL below is a complete, working endpoint.
        if domain_name and hosted_zone_id:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "Zone", hosted_zone_id=hosted_zone_id, zone_name=zone_name
            )
            # Regional, not edge-optimized: an edge-optimized domain runs on
            # CloudFront and would force this certificate into us-east-1,
            # pulling a second region into the API stack for no benefit. The
            # static site is a separate us-east-1 stack for exactly that reason.
            certificate = acm.Certificate(
                self,
                "Certificate",
                domain_name=domain_name,
                validation=acm.CertificateValidation.from_dns(zone),
            )
            custom_domain = apigw.DomainName(
                self,
                "CustomDomain",
                domain_name=domain_name,
                certificate=certificate,
                endpoint_type=apigw.EndpointType.REGIONAL,
                security_policy=apigw.SecurityPolicy.TLS_1_2,
            )
            custom_domain.add_base_path_mapping(api, stage=api.deployment_stage)

            route53.ARecord(
                self,
                "ApiAlias",
                zone=zone,
                record_name=domain_name.removesuffix("." + zone_name),
                target=route53.RecordTarget.from_alias(
                    targets.ApiGatewayDomain(custom_domain)
                ),
            )
            CfnOutput(self, "CustomDomainUrl", value=f"https://{domain_name}/expenses")

        CfnOutput(self, "ApiEndpoint", value=api.url_for_path("/expenses"))
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "ModelId", value=model_id)
        CfnOutput(self, "ApiKeySecretName", value=api_key_secret.secret_name)
