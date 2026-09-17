"""Removing somebody, and what happens to what they left behind.

Two things are true at once. Nothing further sent from their address or number
should be accepted - that part already worked. And anything of theirs still
waiting for a decision or a payment has to stop waiting, because the person who
could answer a question about it is gone, and a claim sitting in a queue nobody
can act on is worse than one that is visibly closed.

What must not happen is losing the history. A payment made last quarter does
not stop having been made because somebody left, and the receipt behind it is
evidence an auditor may want years later.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


class InFlightClaimsAreClosed(unittest.TestCase):
    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"), ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())
        self.withdraw = self.auth.split("def _withdraw_open_claims(", 1)[1].split("\ndef ", 1)[0]
        self.open_fn = self.auth.split("def open_claims_for(", 1)[1].split("\ndef ", 1)[0]

    def test_removal_closes_what_is_still_open(self):
        update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if body.get("status") == "removed":', update)
        self.assertIn("_withdraw_open_claims(", update)

    def test_settled_and_rejected_claims_are_left_alone(self):
        # History. A payment made last quarter does not stop having been made.
        self.assertIn('OPEN_OUTCOMES', self.open_fn)
        self.assertIn('r.get("review_action") not in ("rejected", "withdrawn")', self.open_fn)

    def test_nothing_is_deleted(self):
        # The claim, the receipt and the reasoning are all evidence. What has
        # to stop is the claim being payable and being in a queue.
        self.assertNotIn("delete_item", self.withdraw)
        self.assertIn('":w": "withdrawn"', self.withdraw)

    def test_the_reason_names_what_happened(self):
        self.assertIn("was removed from the organisation.", self.withdraw)

    def test_the_receipt_stops_holding_its_fingerprint(self):
        # Nothing was paid, so somebody legitimately re-submitting that bill
        # later must not be told it duplicates a claim cancelled when a
        # colleague left.
        self.assertIn("duplicates.release", self.withdraw)

    def test_a_failure_on_one_claim_does_not_abandon_the_rest(self):
        self.assertIn("except Exception:", self.withdraw)
        self.assertIn("continue", self.withdraw)

    def test_the_membership_is_written_first(self):
        # If the second half fails they are still removed - nothing they send
        # is accepted - and a claim left in a queue is visible, where a removal
        # that silently did not happen is not.
        update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(update.index("_users.update_item"), update.index("_withdraw_open_claims("))


class YouAreToldBeforeYouAreAsked(unittest.TestCase):
    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"), ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_the_count_reaches_the_console(self):
        people = self.auth.split("def _people(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"open_claims"', people)
        self.assertIn("openClaims: p.open_claims || 0", self.app)

    def test_removal_asks_first_and_says_what_it_will_do(self):
        # "Remove them?" and "remove them and cancel four waiting claims?" are
        # different questions.
        self.assertIn("window.confirm(", self.app)
        self.assertIn("still waiting for a decision or a payment", self.app)
        self.assertIn("not paid, not deleted", self.app)

    def test_it_says_what_survives(self):
        self.assertIn("Anything already settled or rejected is kept.", self.app)

    def test_reinstating_does_not_ask(self):
        # Giving access back takes nothing away.
        block = self.app.split("act.addEventListener", 1)[1].split("});", 1)[0]
        self.assertIn("if (!removed && !window.confirm(", block)


if __name__ == "__main__":
    unittest.main()


class YouCannotRemoveYourself(unittest.TestCase):
    """The button was there, and the server refused it.

    The console decided whose row it was by comparing display names - and an
    owner whose membership carries no name is shown the local part of their
    address, which never equals the name the session knows them by. So Remove
    appeared on their own row and failed when pressed.

    An action that cannot work should not be on screen, and the reason it
    cannot is worth saying: an organisation with no owner is one nobody can
    buy credits for or change policy in.
    """

    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"), ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_the_console_decides_by_email(self):
        self.assertIn("person.email.toLowerCase() === String(LIVE.email).toLowerCase()", self.app)
        self.assertNotIn("if (person.name !== currentUser.name) {", self.app)

    def test_the_server_still_refuses_it(self):
        update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('if target == actor and status != "active"', update)

    def test_it_says_what_to_do_instead(self):
        # "Make somebody else an Owner" described an act that no longer
        # exists: there is one owner, and it moves by transfer.
        self.assertIn("Transfer ownership first", self.app)
        self.assertNotIn("Make somebody else an Owner", self.app)

    def test_the_last_owner_cannot_be_demoted_either(self):
        # The guard that was here asked the wrong question through a call boto3
        # rejects outright - `IndexName=None` - so it raised instead of
        # refusing, and *every* attempt to demote *any* owner came back "Could
        # not change the role". It now counts the organisation's owners.
        update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("This is the only owner. Transfer ownership first.", update)
        self.assertIn('if len(owners_of(org["org_id"])) <= 1:', update)
        # Code only: the comment above the fix names the call it replaced.
        code = "\n".join(l for l in self.auth.splitlines()
                         if not l.strip().startswith("#"))
        self.assertNotIn("IndexName=None", code)


class TheReceiptColumnAnswersTheQuestionBeingAsked(unittest.TestCase):
    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"), ("app", "../PORTAL/app.html")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_settled_and_pending_are_counted_separately(self):
        people = self.auth.split("def _people(", 1)[1].split("\ndef ", 1)[0]
        for field in ('"open_claims"', '"settled_claims"', '"total_claims"'):
            self.assertIn(field, people, field)

    def test_one_scan_serves_all_three(self):
        # A count per person would be one scan each for a list of twenty.
        people = self.auth.split("def _people(", 1)[1].split("\ndef ", 1)[0]
        self.assertEqual(people.count("_submissions_tbl_scan(org_id)"), 1)

    def test_the_column_shows_both(self):
        self.assertIn("person.settled || 0", self.app)
        self.assertIn("pending</span>", self.app)

    def test_nothing_pending_shows_nothing(self):
        # A "0 pending" on every row is noise.
        self.assertIn("person.openClaims\n        ?", self.app)


class TheManagePanelIsOneRow(unittest.TestCase):
    """Name, staff id, role, save and remove are all the same job.

    "Who is this person and what may they do" was split across two rows with
    generous padding, which made a short form taller than the list it hangs
    off - so opening one person pushed everybody else off the screen.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "../PORTAL/app.html"), encoding="utf-8") as h:
            self.app = h.read()
        self.css = "\n".join(__import__("re").findall(
            r"<style[^>]*>(.*?)</style>", self.app, __import__("re").S))

    def test_the_fields_share_a_row(self):
        self.assertIn("const ident = foot;", self.app)

    def test_save_comes_after_the_three_things_it_saves(self):
        # Sitting between Staff ID and Role, it read as though the role were
        # outside whatever Save was for. Identity first, then what they may
        # do, then the button that commits it.
        panel = self.app.split("const ident = foot;", 1)[1].split("</script>", 1)[0]
        role = panel.index("foot.appendChild(rl);")
        save = panel.index("ident.appendChild(saveIdent);")
        self.assertLess(role, save, "Save is rendered before the Role select")

    def test_save_reads_as_a_control(self):
        # A plain grey Save beside a filled-in field reads as decoration, and
        # the edit gets abandoned in the box.
        self.assertIn('saveIdent.className = "btn save"', self.app)
        self.assertIn(".btn.save {", self.css)

    def test_removal_sits_at_the_far_end(self):
        # The one thing on the row nobody is looking for.
        self.assertIn(".grpfoot .btn.danger { margin-left:auto; }", self.css)

    def test_the_explanation_wraps_rather_than_squeezing_the_fields(self):
        self.assertIn(".grpfoot .sf-hint { flex:1 1 100%;", self.css)


class AnInvitedPersonCanStillBeAdministered(unittest.TestCase):
    """Invited, listed in People, and impossible to do anything to.

    Every administrative endpoint checked existence with
    `identity.resolve_by_email`, which gates on `status == "active"` - because
    its actual job is deciding whether a receipt from an address may be
    accepted, and an unaccepted invitation must not be.

    So somebody invited to a mistyped address sat on the roll for ever:
    removing them, renaming them or changing their role all answered "that
    person is not in your organisation", which is both wrong and the opposite
    of what the screen in front of you shows.
    """

    def setUp(self):
        for name, path in (("auth", "lambda_src/auth.py"),
                           ("identity", "lambda_src/identity.py")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as h:
                setattr(self, name, h.read())

    def test_administration_has_a_lookup_of_its_own(self):
        self.assertIn("def membership_in(", self.identity)
        fn = self.identity.split("def membership_in(", 1)[1].split("\ndef ", 1)[0]
        # No status filter at all: that is the entire point of it.
        self.assertNotIn('status', fn.split('"""', 2)[-1])

    def test_removing_and_renaming_use_it(self):
        update = self.auth.split("def _member_update(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('identity.membership_in(org["org_id"], target)', update)
        # The call, not the word - the comment above it names the resolver it
        # deliberately does not use.
        self.assertNotIn("identity.resolve_by_email(target", update)

    def test_assigning_groups_uses_it(self):
        groups = self.auth.split("def _member_groups(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('identity.membership_in(org["org_id"], target)', groups)
        self.assertNotIn("identity.resolve_by_email(target", groups)

    def test_it_cannot_reach_into_another_organisation(self):
        # One address can hold memberships of several organisations, so the
        # org is part of the lookup rather than a check made afterwards.
        fn = self.identity.split("def membership_in(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('r.get("org_id") == org_id', fn)

    def test_intake_still_refuses_an_unaccepted_invitation(self):
        # The gate this loosens for administration must stay shut for receipts:
        # a mistyped address must never start claiming on somebody's behalf.
        latest = self.identity.split("def _latest_active(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('r.get("status") == "active"', latest)
        for intake_path in ("resolve_by_mobile", "resolve_sender"):
            self.assertIn(intake_path, self.identity)
