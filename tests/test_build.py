"""Tests for the record-quality filter used when building the database (no database needed)."""
import unittest

from nosh.build import is_junk, is_junk_content, split_steps


class JunkRecords(unittest.TestCase):
    def test_placeholder_titles(self):
        for t in ("Title", "title:", "Tst", "QA", "Untitled", "N/A", "123"):
            self.assertTrue(is_junk(t, "Mix everything together and bake it."), t)

    def test_real_short_titles_are_kept(self):
        for t in ("XYZ", "BLD", "Pho", "Dal", "BLT"):
            self.assertFalse(is_junk(t, "Combine all ingredients and shake with ice."), t)

    def test_instructions_without_content(self):
        self.assertTrue(is_junk("Pasta", ";"))
        self.assertTrue(is_junk("Pasta", "..."))
        self.assertFalse(is_junk("Pasta", "Boil the pasta in salted water."))

    def test_title_repeated_as_body(self):
        t = "Blooming Color Centerpiece"
        self.assertTrue(is_junk_content(t, t, [t], 1))                     # ingredients and instructions are the title
        self.assertTrue(is_junk_content("no recipes available", "no recipes available", ["no recipes available"], 1))

    def test_ingredient_lines_without_any_name(self):
        self.assertTrue(is_junk_content("Rice", "Cook the rice in water until tender.", ["1 cup"], 0))

    def test_keyboard_mash_and_repeated_words(self):
        self.assertTrue(is_junk_content("Coolio", "1\nsdkfhiashf;hSAFASFASFAS", ["1 cup rice"], 1))
        self.assertTrue(is_junk_content("Rice", "method method method....;", ["1 cup rice"], 1))

    def test_real_recipes_pass(self):
        self.assertFalse(is_junk_content("Watermelon Juice", "Cut watermelon into chunks and press through a sieve into a bowl.", ["1 watermelon"], 1))
        self.assertFalse(is_junk_content("Grilled Steak", "Grill the steak for 6 minutes a side.", ["1 steak"], 1))

    def test_step_splitting(self):
        self.assertEqual(split_steps("Mix.\nBake."), ["Mix.", "Bake."])
        self.assertEqual(split_steps("Mix well. Bake for an hour."), ["Mix well.", "Bake for an hour."])


if __name__ == "__main__":
    unittest.main()
