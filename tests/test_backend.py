import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from pydantic import ValidationError

from backend.main import (
    AccountRequest,
    GenerateRequest,
    LoginRequest,
    build_composition_analysis,
    hash_password,
    sanitize_text,
    verify_password,
)
from backend.main import make_psd


class GenerateRequestValidationTests(unittest.TestCase):
    def test_prompt_cannot_be_blank(self):
        with self.assertRaises(ValidationError):
            GenerateRequest(prompt="   ", width=1080, height=1350)

    def test_dimensions_must_be_valid(self):
        with self.assertRaises(ValidationError):
            GenerateRequest(prompt="test", width=200, height=1350)

        with self.assertRaises(ValidationError):
            GenerateRequest(prompt="test", width=1080, height=200)

    def test_sanitize_text_trims_and_limits_length(self):
        self.assertEqual(sanitize_text("  Bonjour monde  "), "Bonjour monde")
        self.assertEqual(len(sanitize_text("x" * 500, max_length=40)), 40)

    def test_output_format_is_limited_to_supported_formats(self):
        for output_format in ("png", "jpeg", "psd"):
            request = GenerateRequest(prompt="test", output_format=output_format)
            self.assertEqual(request.output_format, output_format)

        with self.assertRaises(ValidationError):
            GenerateRequest(prompt="test", output_format="gif")

    def test_psd_contains_requested_composition_layers(self):
        from psd_tools import PSDImage

        with TemporaryDirectory() as directory:
            output = Path(directory) / "composition.psd"
            make_psd(Image.new("RGB", (64, 64), "red"), "Titre", "Sous-titre", output)
            layer_names = [layer.name for layer in PSDImage.open(output)]

        self.assertEqual(
            layer_names,
            ["Background", "Objets", "Text Layer - Titre", "Text Layer - Sous-titre"],
        )

    def test_composition_analysis_describes_next_pipeline_step(self):
        analysis = build_composition_analysis("poster", "Titre", "Sous", 1080, 1350)
        self.assertEqual(analysis["next_step"], "segmentation-and-ocr")
        self.assertEqual([layer["name"] for layer in analysis["layers"][:2]], ["Background", "Objets"])


class AccountValidationTests(unittest.TestCase):
    def test_account_requires_valid_email_and_password(self):
        with self.assertRaises(ValidationError):
            AccountRequest(name="Ada", email="invalid", password="password")

        with self.assertRaises(ValidationError):
            AccountRequest(name="Ada", email="ada@example.com", password="short")

    def test_account_normalizes_email(self):
        account = AccountRequest(
            name=" Ada ", email="ADA@EXAMPLE.COM", password="password123"
        )
        self.assertEqual(account.name, "Ada")
        self.assertEqual(account.email, "ada@example.com")

    def test_password_hash_can_be_verified(self):
        stored_hash = hash_password("password123")
        self.assertTrue(verify_password("password123", stored_hash))
        self.assertFalse(verify_password("wrong-password", stored_hash))

    def test_login_normalizes_email(self):
        credentials = LoginRequest(email=" ADA@EXAMPLE.COM ", password="password123")
        self.assertEqual(credentials.email, "ada@example.com")


if __name__ == "__main__":
    unittest.main()
