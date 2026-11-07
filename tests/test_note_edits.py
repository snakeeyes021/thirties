"""Structure edits on a checklist note: pure functions that touch only the lines they mean to."""

import unittest

from thirties_core import note_edits as ne

ERRANDS = """# Active
- [ ] Call the bank
    - [ ] Find the account number
- [ ] Buy stamps

# Probation
- [ ] Learn to juggle
"""


class TestOutline(unittest.TestCase):

    def test_sections_with_their_items_and_someday_ones_marked(self):
        out = ne.outline(ERRANDS, ["Probation"])
        self.assertEqual([s.name for s in out], ["Active", "Probation"])
        self.assertEqual(out[0].open_items, ["Call the bank", "Find the account number", "Buy stamps"])
        self.assertTrue(out[1].someday)
        self.assertFalse(out[0].someday)

    def test_someday_headings_are_known_without_settings(self):
        self.assertTrue(ne.is_someday_heading("Someday / maybe"))
        self.assertTrue(ne.is_someday_heading("Probation"))
        self.assertFalse(ne.is_someday_heading("Active"))
        self.assertTrue(ne.outline("# Done\n- [x] a\n")[0].done)

    def test_the_top_counts_only_when_it_has_items(self):
        self.assertEqual([s.name for s in ne.outline("- [ ] a\n# B\n- [ ] b\n")], ["", "B"])
        self.assertEqual([s.name for s in ne.outline("intro\n# B\n- [ ] b\n")], ["B"])


class TestEdits(unittest.TestCase):

    def test_rename_keeps_indent_tick_and_children(self):
        out = ne.rename_item(ERRANDS, "Call the bank", "Call the credit union")
        self.assertIn("- [ ] Call the credit union\n    - [ ] Find the account number", out)
        self.assertIsNone(ne.rename_item(ERRANDS, "Call the bnak", "x"))  # exact wording only

    def test_split_one_line_into_several(self):
        out = ne.split_item("- [ ] Call bank and dentist\n- [ ] Other\n", "Call bank and dentist",
                            ["Call the bank", "Call the dentist"])
        self.assertEqual(out, "- [ ] Call the bank\n- [ ] Call the dentist\n- [ ] Other\n")
        self.assertIsNone(ne.split_item("- [ ] a\n", "a", ["only one"]))

    def test_move_between_sections_takes_the_children_along(self):
        out = ne.move_within(ERRANDS, "Call the bank", "Probation")
        self.assertEqual(out, "# Active\n- [ ] Buy stamps\n\n# Probation\n- [ ] Learn to juggle\n- [ ] Call the bank\n"
                              "    - [ ] Find the account number\n")

    def test_take_and_insert_rebase_a_nested_item(self):
        rest, block, section = ne.take_item(ERRANDS, "Find the account number")
        self.assertEqual(block, ["- [ ] Find the account number"])
        self.assertEqual(section, "Active")
        self.assertNotIn("account number", rest)

    def test_insert_at_the_end_of_a_section_not_under_the_last_heading(self):
        # 10-24: a line with no section went after the note's last item, which was under Probation
        out = ne.insert_lines(ERRANDS, ["- [ ] Set an item for tomorrow"], "Active")
        self.assertIn("- [ ] Buy stamps\n- [ ] Set an item for tomorrow\n\n# Probation", out)

    def test_insert_at_the_top_before_the_first_heading(self):
        out = ne.insert_lines("Notes\n# Someday\n- [ ] x\n", ["- [ ] y"], "")
        self.assertEqual(out, "Notes\n- [ ] y\n\n# Someday\n- [ ] x\n")
        out = ne.insert_lines("- [ ] a\n# Someday\n- [ ] x\n", ["- [ ] y"], "")
        self.assertEqual(out, "- [ ] a\n- [ ] y\n# Someday\n- [ ] x\n")

    def test_insert_after_a_last_item_with_children(self):
        out = ne.insert_lines("# A\n- [ ] a\n    - [ ] a1\n# B\n", ["- [ ] new"], "A")
        self.assertEqual(out, "# A\n- [ ] a\n    - [ ] a1\n- [ ] new\n# B\n")

    def test_children_after_a_blank_line_go_with_their_item(self):
        body = "- [ ] Other\n- [ ] Parent\n    - [ ] c1\n\n    - [ ] c2\n\n- [ ] Next\n"
        rest, block, _ = ne.take_item(body, "Parent")
        self.assertEqual(block, ["- [ ] Parent", "    - [ ] c1", "", "    - [ ] c2"])
        self.assertEqual(rest, "- [ ] Other\n\n- [ ] Next\n")

    def test_sub_headings_of_done_and_someday_sections_count_as_them(self):
        out = ne.outline("## Errands\n- [ ] a\n## Done\n### October\n- [x] b\n# Someday\n## Home\n- [ ] c\n")
        self.assertEqual([(s.name, s.done, s.someday) for s in out],
                         [("Errands", False, False), ("Done", True, False), ("October", True, False),
                          ("Someday", False, True), ("Home", False, True)])
        self.assertFalse(ne.DONE_HEADING_RE.match("Done by Friday"))
        self.assertTrue(ne.DONE_HEADING_RE.match("Completed (October)"))

    def test_the_safe_section_when_nobody_chose(self):
        self.assertIsNone(ne.safe_section("- [ ] a\n"))
        self.assertEqual(ne.safe_section("# Active\n- [ ] a\n# Probation\n- [ ] b\n"), "Active")
        self.assertEqual(ne.safe_section("- [ ] a\n# Probation\n- [ ] b\n"), "")
        self.assertEqual(ne.safe_section("# Probation\n- [ ] b\n"), "")

    def test_ticked_only_finds_the_done_copy(self):
        body = "- [ ] Milk\n- [x] Milk\n"
        rest, block, _ = ne.take_item(body, "Milk", ticked_only=True)
        self.assertEqual((rest, block), ("- [ ] Milk\n", ["- [x] Milk"]))
        self.assertEqual(ne.untick(["- [x] Milk", "    - [x] a"]), ["- [ ] Milk", "    - [x] a"])


if __name__ == "__main__":
    unittest.main()
