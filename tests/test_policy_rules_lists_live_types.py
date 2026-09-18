"""A rule that is not in force, on the tab for setting rules.

Repairs & Maintenance was disabled. It appeared in the Policy rules list all
the same, with a DISABLED tag over its cap - and the panel under it said, in
effect, "you cannot enable this here, go to Organisation". So the tab showed a
rule that judges nothing, labelled with a state, and offered no way to change
the state it was labelled with. The reasonable next thing to do was to try, and
there was nothing to try.

Policy rules answers one question: what does this expense cost the company. A
disabled type costs it nothing - it judges no receipt, and every claim of that
type goes to a person whatever its cap says. So it does not belong in that
list, and once the list holds only live types the tag has nothing left to say:
it could only ever read "enabled".

Which leaves one place where a type is turned on and off, the place that also
names and deletes it - Organisation > Expense types - and that is the whole
subject of that tab.

The other half of the same complaint: a new type was born disabled, "off until
it has been configured". Nobody adds an expense type they do not want used, so
that put an extra step on the path everybody takes - and the switch that undoes
it is on a different tab from the cap, so a type added and then capped was
still off, wearing the tag above. New types are enabled.
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class OnlyLiveTypesAreListedAgainstTheirRules(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.rules = self.app.split("function renderRules() {", 1)[1].split(
            "\n}", 1)[0]
        self.types = self.app.split("function renderExpenseTypes() {", 1)[1].split(
            "\n}", 1)[0]

    def test_the_rules_tab_asks_for_enabled_types_only(self):
        self.assertIn("ensureSelectedType(true);", self.rules)
        self.assertIn('renderTypeIndex("rules-index"', self.rules)
        # The flag is the last argument to that call.
        call = self.rules.split('renderTypeIndex("rules-index"', 1)[1].split(
            ");", 1)[0]
        self.assertIn("true", call.rsplit(",", 1)[1])

    def test_the_organisation_tab_still_shows_every_type(self):
        # It is the tab that turns them on, so it has to list the ones that
        # are off. Not passing the flag is what makes it the full list.
        call = self.types.split('renderTypeIndex("et-index"', 1)[1].split(
            ");", 1)[0]
        self.assertNotIn("true", call.rsplit(",", 1)[1])

    def test_the_filter_that_decides_it(self):
        self.assertIn("return onlyEnabled ? rules.types.filter(t => t.enabled) "
                      ": rules.types;", self.app)

    def test_a_selection_made_on_the_other_tab_is_re_pointed(self):
        # Selecting a disabled type under Organisation and switching to Policy
        # rules would otherwise draw its rule with nothing highlighted beside
        # it in the list.
        body = self.app.split("function ensureSelectedType(onlyEnabled) {", 1)[1]
        body = body.split("\n}", 1)[0]
        self.assertIn("const list = typesFor(onlyEnabled);", body)
        self.assertIn("selectedTypeId = list.length ? list[0].id : null;", body)


class TheTagGoesWithTheList(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_no_state_chip_on_the_rule(self):
        detail = self.app.split("function renderRuleDetail(ro) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertNotIn('state.textContent = t.enabled ? "enabled" : "disabled"',
                         detail)
        self.assertNotIn('state.className = "state "', detail)

    def test_no_state_dot_in_the_rules_list(self):
        index = self.app.split("function renderTypeIndex(", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('(onlyEnabled ? "" :', index)

    def test_the_organisation_list_keeps_its_dot(self):
        index = self.app.split("function renderTypeIndex(", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn('<span class="dot ${t.enabled ? "" : "off"}"', index)

    def test_the_toggle_is_still_on_the_organisation_tab(self):
        # Removing the tag must not remove the control. This is the one place
        # a type is turned on, and the rules tab now points at it.
        detail = self.app.split("function renderTypeDetail(ro) {", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("t.enabled = pcb.checked;", detail)

    def test_enabling_is_saved(self):
        self.assertIn("enabled: !!t.enabled,", self.app)


class NothingEnabledSaysSoAndSaysWhereToGo(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")

    def test_the_empty_list_names_the_tab_that_fixes_it(self):
        self.assertIn("No expense type is enabled. Turn one on under "
                      "Organisation › ", self.app)

    def test_the_empty_panel_does_too(self):
        self.assertIn("No expense type is enabled, so no rule is in force.",
                      self.app)

    def test_a_filter_matching_nothing_is_still_its_own_message(self):
        # "No expense type is enabled" would be a lie about a policy that has
        # four, typed into a search box.
        self.assertIn('"No type matches that."', self.app)


class ANewTypeIsOn(unittest.TestCase):

    def setUp(self):
        self.app = read("../PORTAL/app.html")
        self.block = self.app.split('$("et-new").addEventListener', 1)[1][:1800]

    def test_it_is_created_enabled(self):
        self.assertIn("enabled: true,", self.block)

    def test_the_old_default_is_gone(self):
        self.assertNotIn("enabled: false,", self.block)

    def test_the_message_no_longer_promises_a_step_that_is_not_there(self):
        self.assertNotIn("disabled until configured", self.app)

    def test_it_still_gets_a_cap_in_the_organisation_currency(self):
        # Being on with a cap in a currency the company never receives is the
        # other way to make every claim of a new type go to a reviewer.
        self.assertIn("caps: { [orgCurrency()]: 1000000 }", self.block)

    def test_and_appears_immediately_on_the_rules_tab(self):
        # Which is the point of the default: born enabled, it is in the list
        # where its cap is set, instead of needing a visit to a second tab
        # before that tab will show it.
        self.assertIn("typeFilter = \"\";", self.block)


if __name__ == "__main__":
    unittest.main()
