#!/usr/bin/env python3
"""Move Expenze onto its own WhatsApp number, one reversible step at a time.

The adapter answers on every number in the secret at once (see `identities()`
in whatsapp.py), which is what makes this safe: there is never an instant when
a receipt sent to either number is dropped. What changes between steps is only
which number Expenze *starts* messages from.

    add        the new number joins, the old one stays in charge
    webhook    point its app at us and subscribe its WABA
    templates  submit the two notice templates to the new WABA
    promote    the new number takes over; the old one still receives
    finish     the old number is dropped
    check      what each configured number actually is, and what it can send

Run them in that order, testing between each. Every step is reversible by
running the previous one, because nothing is deleted until `finish`.

Secrets are never printed, never passed as arguments - so they cannot end up
in shell history or in a terminal scrollback - and never leave this machine
except to go back into Secrets Manager. The token and app secret are typed at
a hidden prompt, and the existing values are read out of the secret and put
straight back, so the old number's credentials never have to be retyped.

    python3 tools/wa_migrate.py check
    python3 tools/wa_migrate.py add --phone-number-id 1234 --waba-id 5678
    python3 tools/wa_migrate.py promote --phone-number-id 1234
    python3 tools/wa_migrate.py finish
"""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
import urllib.error
import urllib.request

import boto3

SECRET_ID = "expenze/whatsapp"
REGION = "ap-southeast-1"
GRAPH = "https://graph.facebook.com/v21.0"

# The notices that need an approved template to reach somebody outside the
# 24-hour service window. Everything else Expenze sends is a reply inside a
# conversation the person started, which needs no template.
# Needed on any number that still sends outcome notices.
NEEDED_TEMPLATES = ("expenze_claim_settled", "expenze_claim_rejected")

# The sign-in code for adding a WhatsApp number. It has to be a template and
# it has to be AUTHENTICATION: Meta delivers free-form text only to somebody
# who wrote to the business in the last 24 hours, and a person adding their
# number for the first time has by definition never written to it.
#
# Its own category, its own shape. `add_security_recommendation`, the
# expiry footer and the copy-code button are not decoration - an
# AUTHENTICATION template is rejected without them, and `_send_wa_code`
# already sends the matching body and button components.
AUTH_TEMPLATE = "expenze_verify_code"

# Copied from the two already approved, character for character, because the
# parameter count and order are not decoration: `notify._template_parameters`
# fills {{1}}..{{5}} in this exact sequence, and a template approved with a
# different number of placeholders is rejected at send time with an error
# nobody sees until somebody is waiting to be told they have been paid.
TEMPLATE_BODIES = {
    "expenze_claim_settled": {
        "text": ("Your expense claim for {{1}} has been reimbursed.\n\n"
                 "Amount paid: {{2}}\nReference: {{3}}\nPaid on: {{4}}\n"
                 "Settled by: {{5}}\n\nReply to your finance team if this "
                 "does not match your records."),
        "example": ["The Bombay Canteen", "INR 3,631.00", "UTR9912837465",
                    "09 Sep 2026", "Meera Iyer"],
    },
    AUTH_TEMPLATE: {
        "category": "AUTHENTICATION",
        "components": [
            {"type": "BODY", "add_security_recommendation": True},
            {"type": "FOOTER", "code_expiration_minutes": 10},
            {"type": "BUTTONS", "buttons": [
                {"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"}]},
        ],
    },
    "expenze_claim_rejected": {
        "text": ("Your expense claim for {{1}} ({{2}}) will not be reimbursed."
                 "\n\nReason: {{3}}\n\nDecided by {{4}}. If this looks wrong, "
                 "reply to your finance team - the decision can be reversed."),
        "example": ["Toit Brewpub", "INR 2,114.00",
                    "The cover count was four but only two people were on the trip.",
                    "Meera Iyer"],
    },
}


def _client():
    return boto3.client("secretsmanager", region_name=REGION)


def read() -> dict:
    raw = _client().get_secret_value(SecretId=SECRET_ID)["SecretString"]
    return json.loads(raw)


def write(cfg: dict) -> None:
    """Put the secret back, and say what changed without saying what it is."""
    _client().put_secret_value(SecretId=SECRET_ID,
                               SecretString=json.dumps(cfg, separators=(",", ":")))
    print("\nSecret updated. Numbers now configured, in order of preference:")
    for i, entry in enumerate(_identities(cfg)):
        role = "sends and receives" if i == 0 else "receives only"
        print(f"  {entry.get('phoneNumberId','?'):<20} {role}")
    print("\nLambdas cache the secret per container, so a cold start picks this "
          "up immediately and a warm one within a few minutes. Nothing needs "
          "redeploying.")


def _identities(cfg: dict) -> list[dict]:
    """The same view of the secret the adapter takes, for reporting."""
    out = [cfg]
    for extra in (cfg.get("alsoAccept") or []):
        if isinstance(extra, dict) and extra.get("phoneNumberId"):
            out.append({**cfg, **extra})
    return out


def _ask(label: str) -> str:
    """A secret, typed rather than passed, so it misses the shell history."""
    value = getpass.getpass(f"  {label} (input hidden): ").strip()
    if not value:
        sys.exit(f"No {label} given; nothing was changed.")
    return value


def _post(path: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(f"{GRAPH}/{path}",
                                 data=json.dumps(payload).encode("utf-8"))
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"error": json.loads(exc.read() or b"{}").get("error", {"message": str(exc)})}
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": str(exc)}}


def _graph(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{GRAPH}/{path}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"error": json.loads(exc.read() or b"{}").get("error", {"message": str(exc)})}
    except Exception as exc:  # noqa: BLE001 - this is a report, not a flow
        return {"error": {"message": str(exc)}}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_check(_args) -> None:
    """What is actually configured, asked of Meta rather than assumed."""
    cfg = read()
    for i, entry in enumerate(_identities(cfg)):
        pid = entry.get("phoneNumberId", "")
        token = entry.get("accessToken", "")
        role = "PRIMARY - sends and receives" if i == 0 else "also accepted - receives only"
        print(f"\n{pid}  ({role})")

        number = _graph(f"{pid}?fields=display_phone_number,verified_name,"
                        f"code_verification_status,quality_rating", token)
        if "error" in number:
            print(f"  ! Meta refused: {number['error'].get('message')}")
            print("    Either the token does not belong to this number's app, "
                  "or it has expired.")
            continue
        print(f"  number    {number.get('display_phone_number','?')}")
        print(f"  name      {number.get('verified_name','?')}")
        print(f"  verified  {number.get('code_verification_status','?')}")
        print(f"  quality   {number.get('quality_rating','?')}")

        waba = entry.get("wabaId", "")
        if not waba:
            print("  templates  (no wabaId on this entry; cannot check)")
            continue
        listed = _graph(f"{waba}/message_templates?limit=200", token)
        if "error" in listed:
            print(f"  templates  ! {listed['error'].get('message')}")
            continue
        # The real status, not merely whether it is usable yet. "MISSING"
        # against a template that was submitted ten minutes ago and is sitting
        # in review is the wrong answer to "what do I still have to do".
        status = {t["name"]: t.get("status", "?") for t in listed.get("data", [])}
        # The sign-in code is only ever sent from the primary - it starts a
        # conversation rather than answering one - so a number that merely
        # receives is not missing anything by not having it.
        wanted = list(NEEDED_TEMPLATES)
        if i == 0:
            wanted.append(entry.get("otpTemplate", "cloudmeter_verify_code"))
        for name in wanted:
            state = status.get(name, "NOT SUBMITTED")
            print(f"  template  {name:<26} "
                  + ("ok" if state == "APPROVED" else state.lower().replace("_", " ")))
        if not all(status.get(n) == "APPROVED" for n in wanted):
            waiting = any(status.get(n) == "PENDING" for n in wanted)
            print("    " + ("Submitted and in review with Meta - nothing to do but wait."
                            if waiting else
                            "Run `templates` to submit them."))
            print("    Until they are approved, a settlement or rejection notice "
                  "more than 24 hours after the person last wrote is not "
                  "delivered on this number. Email still goes either way.")

        subs = _graph(f"{waba}/subscribed_apps", token)
        apps = [a.get("whatsapp_business_api_data", {}).get("name", "?")
                for a in subs.get("data", [])]
        print(f"  webhook   {', '.join(apps) if apps else 'NO APP SUBSCRIBED - nothing reaches us'}")


def cmd_add(args) -> None:
    """The new number joins. The old one keeps sending; both receive."""
    cfg = read()
    if str(cfg.get("phoneNumberId")) == args.phone_number_id:
        sys.exit("That is already the primary number. Nothing to do.")
    if any(str(e.get("phoneNumberId")) == args.phone_number_id
           for e in (cfg.get("alsoAccept") or [])):
        sys.exit("That number is already configured. Run `check` to see it.")

    print("\nThe new number's credentials. If it is on the same Meta app as the "
          "old number, press Enter at the app secret and verify token to reuse "
          "the ones already stored.\n")
    entry = {"phoneNumberId": args.phone_number_id, "wabaId": args.waba_id}
    entry["accessToken"] = _ask("New access token")
    secret = getpass.getpass("  New app secret (Enter to reuse the current one): ").strip()
    if secret:
        entry["appSecret"] = secret
    verify = getpass.getpass("  New webhook verify token (Enter to reuse): ").strip()
    if verify:
        entry["webhookVerifyToken"] = verify

    cfg["alsoAccept"] = (cfg.get("alsoAccept") or []) + [entry]
    write(cfg)
    print("\nNext: point the new app's webhook at")
    print("  https://0w1qn4vso1.execute-api.ap-southeast-1.amazonaws.com/poc/whatsapp/webhook")
    print("  subscribed to the `messages` field, with the verify token above.")
    print("Then send a receipt to the new number and watch it land. The old "
          "number is untouched.")


def cmd_promote(args) -> None:
    """The new number takes over sending. The old one still receives."""
    cfg = read()
    also = cfg.get("alsoAccept") or []
    incoming = next((e for e in also
                     if str(e.get("phoneNumberId")) == args.phone_number_id), None)
    if not incoming:
        sys.exit("That number is not configured yet. Run `add` first.")

    # What the old primary needs in order to keep receiving: its own id, and
    # any credential the new primary does not share with it.
    demoted = {"phoneNumberId": cfg["phoneNumberId"]}
    for key in ("wabaId", "accessToken", "appSecret", "webhookVerifyToken"):
        if cfg.get(key) and cfg.get(key) != incoming.get(key):
            demoted[key] = cfg[key]

    promoted = {**cfg, **incoming}
    promoted["alsoAccept"] = [demoted] + [
        e for e in also if str(e.get("phoneNumberId")) != args.phone_number_id]
    write(promoted)
    print("\nOutbound notices now come from the new number. The old number "
          "still receives, so anyone who has it saved is not cut off.")
    print("Update the number shown to customers in BMS > Settings.")


def cmd_finish(_args) -> None:
    """The old number is dropped. Only run this once nothing arrives on it."""
    cfg = read()
    if not cfg.get("alsoAccept"):
        sys.exit("Only one number is configured; nothing to drop.")
    dropped = [e.get("phoneNumberId") for e in cfg["alsoAccept"]]
    print("About to stop accepting: " + ", ".join(str(d) for d in dropped))
    print("Anything sent to those numbers from now on is ignored in silence.")
    if input("Type 'drop' to confirm: ").strip() != "drop":
        sys.exit("Nothing was changed.")
    cfg.pop("alsoAccept", None)
    # The flat-form leftovers from the earlier shape go with it.
    cfg.pop("previousAppSecret", None)
    cfg.pop("previousWebhookVerifyToken", None)
    write(cfg)


def cmd_templates(args) -> None:
    """Submit the two notice templates to the new number's WABA.

    Meta approves templates per WABA, not per business, so moving to a
    dedicated number means the settlement and rejection notices have to be
    approved again before they can reach anybody outside the 24-hour service
    window. Submitted here rather than retyped into Business Manager, because
    the body text has to match what the code fills in exactly.
    """
    cfg = read()
    entry = next((e for e in _identities(cfg)
                  if str(e.get("phoneNumberId")) == args.phone_number_id), None)
    if not entry:
        sys.exit("That number is not configured. Run `add` first.")
    waba, token = entry.get("wabaId"), entry.get("accessToken")
    if not waba:
        sys.exit("That entry has no wabaId, so there is nothing to submit to.")

    listed = _graph(f"{waba}/message_templates?limit=200", token)
    if "error" in listed:
        sys.exit(f"Could not read the existing templates: {listed['error'].get('message')}")
    existing = {t["name"] for t in listed.get("data", [])}

    for name, body in TEMPLATE_BODIES.items():
        if name in existing:
            print(f"{name}: already submitted; leaving it alone.")
            continue
        if body.get("category") == "AUTHENTICATION":
            # Meta writes the copy for an authentication template itself; the
            # caller supplies the shape, not the words.
            payload = {"name": name, "language": "en",
                       "category": "AUTHENTICATION",
                       "components": body["components"]}
        else:
            payload = {
                "name": name,
                "language": "en",
                "category": "UTILITY",
                "components": [
                    {"type": "BODY", "text": body["text"],
                     "example": {"body_text": [body["example"]]}},
                    {"type": "FOOTER", "text": "Expenze - expenze.ai"},
                ],
            }
        result = _post(f"{waba}/message_templates", token, payload)
        if "error" in result:
            print(f"{name}: refused - {result['error'].get('message')}")
        else:
            print(f"{name}: submitted, status {result.get('status', 'PENDING')}")
    # Point the number at the authentication template we just created for it.
    # `auth.py` reads the name from the secret precisely so that a dedicated
    # number can have its own - and without this the sign-in code is still
    # sent under the *old* WABA's template name, which its own WABA has never
    # heard of and answers 404.
    cfg = read()
    raw = (cfg if str(cfg.get("phoneNumberId")) == args.phone_number_id
           else next((e for e in (cfg.get("alsoAccept") or [])
                      if str(e.get("phoneNumberId")) == args.phone_number_id), None))
    if raw is not None and raw.get("otpTemplate") != AUTH_TEMPLATE:
        raw["otpTemplate"] = AUTH_TEMPLATE
        raw["otpTemplateLanguage"] = "en"
        _client().put_secret_value(SecretId=SECRET_ID,
                                   SecretString=json.dumps(cfg, separators=(",", ":")))
        print(f"\nSign-in codes for {args.phone_number_id} now use {AUTH_TEMPLATE}.")

    print("\nApproval is usually minutes and occasionally a day. Run `check` "
          "to see where they are, and do not `promote` until all three say ok.")


WEBHOOK_URL = ("https://0w1qn4vso1.execute-api.ap-southeast-1.amazonaws.com"
               "/poc/whatsapp/webhook")


def cmd_webhook(args) -> None:
    """Point a number's app at us, and subscribe its WABA to that app.

    Both over the API rather than through Business Manager, because the order
    matters and the screens do not enforce it: Meta verifies a callback URL by
    calling it immediately with the verify token, so the token has to be in
    our secret *before* the URL is registered. Doing it here means storing it
    and registering it in the same breath.

    The verify token is generated rather than asked for. It is a shared string
    with one job - proving a subscription request came from us - and nobody
    needs to know it, so it goes from here into the secret and into Meta
    without being typed, displayed or written down.
    """
    cfg = read()
    entry = next((e for e in _identities(cfg)
                  if str(e.get("phoneNumberId")) == args.phone_number_id), None)
    if not entry:
        sys.exit("That number is not configured. Run `add` first.")
    token, waba = entry.get("accessToken"), entry.get("wabaId")
    if not waba:
        sys.exit("That entry has no wabaId.")

    debug = _graph(f"debug_token?input_token={token}&access_token={token}", token)
    app_id = (debug.get("data") or {}).get("app_id")
    app_name = (debug.get("data") or {}).get("application", "?")
    if not app_id:
        sys.exit("Could not work out which app that token belongs to.")

    app_secret = entry.get("appSecret")

    # The *raw* entry, not the merged view. `_identities` fills gaps from the
    # top level, so asking the merged copy whether this number has a verify
    # token of its own always answers yes - and the new app would quietly be
    # registered under the old app's token, which is the coupling this whole
    # migration exists to remove.
    raw = (cfg if str(cfg.get("phoneNumberId")) == args.phone_number_id
           else next((e for e in (cfg.get("alsoAccept") or [])
                      if str(e.get("phoneNumberId")) == args.phone_number_id), None))
    verify = (raw or {}).get("webhookVerifyToken")
    if not verify:
        # Stored before the URL is registered: Meta calls the callback the
        # moment it is set, and an endpoint that does not yet know the token
        # answers 403.
        verify = secrets.token_urlsafe(24)
        raw["webhookVerifyToken"] = verify
        _client().put_secret_value(SecretId=SECRET_ID,
                                   SecretString=json.dumps(cfg, separators=(",", ":")))
        print(f"Verify token generated and stored for {args.phone_number_id}.")

    print(f"Registering the callback on app {app_name} ({app_id})...")
    registered = _post(f"{app_id}/subscriptions?access_token={app_id}|{app_secret}", token, {
        "object": "whatsapp_business_account",
        "callback_url": WEBHOOK_URL,
        "verify_token": verify,
        "fields": "messages",
    })
    if "error" in registered:
        sys.exit("  refused: " + str(registered["error"].get("message")))
    print("  callback registered and verified by Meta.")

    print(f"Subscribing WABA {waba} to it...")
    joined = _post(f"{waba}/subscribed_apps", token, {})
    if "error" in joined:
        sys.exit("  refused: " + str(joined["error"].get("message")))
    print("  subscribed.")
    print("\nSend a receipt to this number now. Run `check` to confirm the "
          "subscription, and watch the expenze-whatsapp log if nothing lands.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="what each configured number is, and what it can send")

    add = sub.add_parser("add", help="add the new number alongside the old one")
    add.add_argument("--phone-number-id", required=True)
    add.add_argument("--waba-id", required=True)

    promote = sub.add_parser("promote", help="make the new number the one Expenze sends from")
    promote.add_argument("--phone-number-id", required=True)

    webhook = sub.add_parser(
        "webhook", help="register the callback and subscribe the WABA to its app")
    webhook.add_argument("--phone-number-id", required=True)

    templates = sub.add_parser(
        "templates", help="submit the two notice templates to a number's WABA")
    templates.add_argument("--phone-number-id", required=True)

    sub.add_parser("finish", help="stop accepting the old number")

    args = parser.parse_args()
    {"check": cmd_check, "add": cmd_add, "webhook": cmd_webhook,
     "templates": cmd_templates, "promote": cmd_promote,
     "finish": cmd_finish}[args.command](args)


if __name__ == "__main__":
    main()
