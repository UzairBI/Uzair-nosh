"""Unit tests for parsing / normalization / dietary groups (no database needed).  Run: python -m unittest discover -s tests"""
import unittest

from nosh import ingredients as ing
from nosh import nlu


def names(line):
    return ing.parse_line(line)[2]


class Normalization(unittest.TestCase):
    def test_quantity_unit_and_name(self):
        q, u, a = ing.parse_line("1 1/2 cups all-purpose flour")
        self.assertEqual((q, u), (1.5, "cup"))
        self.assertEqual(a, ("allpurpose_flour",))

    def test_fraction_only(self):
        self.assertEqual(ing.parse_line("1/2 cup butter")[0], 0.5)

    def test_plural_prep_and_advert(self):
        self.assertEqual(names("2 tomatoes, chopped ADVERTISEMENT"), ("tomato",))

    def test_adjective_only_first_segment(self):
        self.assertEqual(names("4 skinless, boneless chicken breast halves"), ("chicken breast half",))

    def test_alternatives(self):
        self.assertEqual(names("1/2 cup butter or margarine, softened"), ("butter", "margarine"))

    def test_salt_and_pepper_split(self):
        self.assertEqual(names("kosher salt and ground black pepper to taste"), ("salt", "black_pepper"))

    def test_different_products_stay_different(self):
        self.assertEqual(names("1 can tomato paste"), ("tomato_paste",))
        self.assertEqual(names("1 cup peanut butter"), ("peanut_butter",))
        self.assertEqual(names("1 bunch green onions"), ("green_onion",))
        self.assertEqual(names("1 cup coconut milk"), ("coconut_milk",))

    def test_synonyms(self):
        self.assertEqual(names("3 scallions"), names("3 green onions"))
        self.assertEqual(names("1 cup heavy whipping cream"), ("heavy cream",))


class Matching(unittest.TestCase):
    def test_contiguous_match(self):
        self.assertTrue(ing.name_matches("tomato", "cherry tomato"))
        self.assertFalse(ing.name_matches("tomato", "tomato_paste"))
        self.assertFalse(ing.name_matches("butter", "peanut_butter"))
        self.assertTrue(ing.name_matches("butter", "butter"))
        self.assertFalse(ing.name_matches("onion", "green_onion"))

    def test_generic_expansion(self):
        self.assertTrue(ing.name_matches("oil", "olive_oil"))
        self.assertFalse(ing.name_matches("olive", "olive_oil"))

    def test_user_terms_normalize_like_recipe_lines(self):
        self.assertEqual(ing.query_name("Tomatoes"), "tomato")
        self.assertEqual(ing.query_name("eggs"), "egg")

    def test_like_patterns_escape_underscore(self):
        self.assertEqual(ing.sql_like_patterns("green_onion"), ["% green\\_onion %"])


class Groups(unittest.TestCase):
    def sure(self, line):
        return ing.line_groups(names(line))[0]

    def test_dairy(self):
        self.assertTrue(self.sure("1 cup heavy cream") & ing.G_DAIRY)
        self.assertTrue(self.sure("2 tbsp butter") & ing.G_DAIRY)
        self.assertTrue(self.sure("1/2 cup grated Parmesan cheese") & ing.G_DAIRY)

    def test_not_dairy(self):
        for l in ("1 cup coconut milk", "1/2 tsp cream of tartar", "2 tbsp peanut butter", "1 cup almond milk"):
            self.assertFalse(self.sure(l) & ing.G_DAIRY, l)

    def test_uncertain_is_not_definite(self):
        s, m = ing.line_groups(names("2 tbsp margarine"))
        self.assertFalse(s & ing.G_DAIRY)
        self.assertTrue(m & ing.G_DAIRY)

    def test_alternative_flagged_conservatively(self):
        self.assertTrue(self.sure("1/2 cup butter or margarine") & ing.G_DAIRY)

    def test_egg_peanut_gluten_meat(self):
        self.assertTrue(self.sure("3 eggs") & ing.G_EGG)
        self.assertTrue(self.sure("1 cup mayonnaise") & ing.G_EGG)
        self.assertFalse(self.sure("1 eggplant") & ing.G_EGG)
        self.assertTrue(self.sure("1/2 cup peanuts") & ing.G_PEANUT)
        self.assertTrue(self.sure("2 cups all-purpose flour") & ing.G_GLUTEN)
        self.assertFalse(self.sure("1 cup rice flour") & ing.G_GLUTEN)
        self.assertTrue(self.sure("2 cups chicken broth") & ing.G_MEAT)


class NLU(unittest.TestCase):
    def test_negations(self):
        mask, terms, _ = nlu.extract_negations("I cannot have dairy")
        self.assertEqual(mask, ing.G_DAIRY)
        mask, terms, _ = nlu.extract_negations("no peanuts and without mushrooms")
        self.assertEqual(mask, ing.G_PEANUT)
        self.assertEqual(terms, ["mushroom"])

    def test_steps(self):
        self.assertEqual(nlu.extract_steps("at most six instruction steps")[0], 6)
        self.assertEqual(nlu.extract_steps("fewer than 5 steps")[0], 4)

    def test_intents(self):
        self.assertEqual(nlu.parse("How many recipes contain both garlic and onion?").kind, "stats")
        self.assertEqual(nlu.parse("I have eggs, onions and rice").kind, "match")
        self.assertEqual(nlu.parse("Take this banana bread recipe and remove eggs").kind, "substitute")
        self.assertEqual(nlu.parse("How many calories does it have?").kind, "unsupported")
        it = nlu.parse("How many recipes contain both garlic and onion?")
        self.assertEqual(sorted(it.include), ["garlic", "onion"])


if __name__ == "__main__":
    unittest.main()
