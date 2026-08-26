"""SecretMasker — 로그에 시크릿 평문이 남지 않는지 (AC-13)."""
from __future__ import annotations

import pytest

from autodeploy.masking import SecretMasker, mask_url_secrets


def test_masks_a_registered_secret():
    mask = SecretMasker(["hvs.CAESIJKLMNOP"])
    assert mask("token is hvs.CAESIJKLMNOP done") == "token is *** done"


def test_masks_every_occurrence():
    mask = SecretMasker(["s3cret-value"])
    assert mask("s3cret-value and s3cret-value") == "*** and ***"


def test_leaves_unrelated_text_alone():
    mask = SecretMasker(["s3cret-value"])
    assert mask("nothing to hide") == "nothing to hide"


def test_longer_secret_wins_when_one_contains_the_other():
    """짧은 값이 먼저 지워지면 긴 값의 잔여물이 남는다."""
    mask = SecretMasker(["abcdef", "abcdef123456"])
    assert mask("x abcdef123456 y") == "x *** y"


def test_short_values_are_not_registered():
    """짧은 비밀번호를 마스킹하면 로그의 모든 우연한 일치가 지워져 진단이 불가능해진다."""
    mask = SecretMasker(["1234"])
    assert mask.values == ()
    assert mask("rc: 1234") == "rc: 1234"


def test_boundary_length_is_registered():
    mask = SecretMasker(["a" * SecretMasker.MIN_LEN])
    assert len(mask.values) == 1


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_values_are_ignored(value):
    assert SecretMasker([value]).values == ()


def test_duplicates_registered_once():
    assert SecretMasker(["repeated-value", "repeated-value"]).values == ("repeated-value",)


def test_surrounding_whitespace_is_stripped_before_registering():
    mask = SecretMasker(["  padded-secret  "])
    assert mask("here is padded-secret") == "here is ***"


def test_url_credentials_masked_without_registration():
    """git clone 이 토큰 박힌 URL 을 그대로 되뱉는 경로 (QA D-3)."""
    mask = SecretMasker()
    assert mask("git clone https://user:ATBBtoken@bitbucket.org/x.git") == (
        "git clone https://user:***@bitbucket.org/x.git"
    )


def test_both_rules_apply_together():
    mask = SecretMasker(["hvs.VAULTTOKEN"])
    line = "hvs.VAULTTOKEN https://u:pw@host"
    assert mask(line) == "*** https://u:***@host"


def test_mask_url_secrets_is_idempotent():
    once = mask_url_secrets("https://u:pw@host")
    assert mask_url_secrets(once) == once


def test_empty_masker_is_a_passthrough():
    assert SecretMasker()("plain line") == "plain line"
