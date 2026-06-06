from ci_dashboard.jobs.retest_parser import is_supported_retest_command


def test_retest_parser_accepts_exact_supported_commands() -> None:
    assert is_supported_retest_command("/retest") is True
    assert is_supported_retest_command("  /retest-required  ") is True


def test_retest_parser_rejects_substring_mentions() -> None:
    assert is_supported_retest_command("say /retest to rerun") is False
    assert is_supported_retest_command("/ReTeSt") is False
    assert is_supported_retest_command(None) is False
