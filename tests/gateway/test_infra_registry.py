from pathlib import Path

from gateway.infra_registry import aws_profiles, parse_ssh_aliases, redact_infra_output


def test_parse_ssh_aliases_ignores_wildcards_and_keeps_names(tmp_path):
    config = tmp_path / "config"
    config.write_text(
        """
Host *
  ServerAliveInterval 30
Host jesus-openclaw pratica
  HostName example.com
Host *.internal
  User ubuntu
""",
        encoding="utf-8",
    )

    assert parse_ssh_aliases(config) == ["jesus-openclaw", "pratica"]


def test_aws_profiles_reads_names_without_values(tmp_path):
    config = tmp_path / "config"
    credentials = tmp_path / "credentials"
    config.write_text("[profile prod]\nregion=us-east-1\n", encoding="utf-8")
    credentials.write_text(
        "[default]\naws_access_key_id=AKIAIOSFODNN7EXAMPLE\naws_secret_access_key=secret\n",
        encoding="utf-8",
    )

    assert aws_profiles(config, credentials) == ["default", "prod"]


def test_redact_infra_output_hides_common_secrets():
    output = redact_infra_output(
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE "
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456 "
        "https://x.test/path?token=abcd1234"
    )

    assert "AKIAIOSFODNN7EXAMPLE" not in output
    assert "wJalrXUtn" not in output
    assert "abcdefghijklmnopqrstuvwxyz" not in output
    assert "abcd1234" not in output
