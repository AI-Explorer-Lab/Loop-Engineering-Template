from config.config import load_environment, validate_settings


def test_test_environment_is_valid() -> None:
    test_settings = load_environment("test")
    validate_settings(test_settings)
    assert test_settings.get("environment.name") == "test"
    assert str(test_settings.get("db.url")).startswith("sqlite+")
