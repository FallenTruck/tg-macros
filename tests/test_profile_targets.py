import unittest

from macro_bot.models import QuestionnaireAnswers
from macro_bot.profile_targets import derive_daily_target


class ProfileTargetTests(unittest.TestCase):
    def test_maintain_target_uses_lower_protein_and_higher_fat_defaults(self):
        answers = QuestionnaireAnswers(
            sex="male",
            age_years=27,
            height_cm=189.0,
            weight_kg=85.0,
            activity_level="moderate",
            goal="maintain",
        )

        target = derive_daily_target(answers)

        self.assertEqual(target.calories, 2950.0)
        self.assertEqual(target.protein_g, 136.0)
        self.assertEqual(target.fat_g, 76.0)
        self.assertEqual(target.carbs_g, 430.0)

    def test_cut_target_uses_1_point_8g_per_kg_protein(self):
        answers = QuestionnaireAnswers(
            sex="female",
            age_years=30,
            height_cm=160.0,
            weight_kg=60.0,
            activity_level="light",
            goal="lose",
        )

        target = derive_daily_target(answers)

        self.assertEqual(target.protein_g, 108.0)
        self.assertEqual(target.fat_g, 54.0)


if __name__ == "__main__":
    unittest.main()
