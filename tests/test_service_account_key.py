"""The service-account private key survives every way it gets pasted.

The key reaches us through a deploy UI text box, and it arrives mangled in a
handful of predictable ways. Quick Meet failed in production because the deploy
UI expanded each ``\n`` escape only half way: the ``n`` became a real newline and
the backslash stayed at the end of the line. Every marker still looked right, so
the key read as valid and was not, and the base64 body silently failed to decode.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.meeting_service import _describe_pem, _normalise_pem


@pytest.fixture(scope="module")
def pem() -> str:
    """A real 2048-bit key, so the assertions are about parsing, not a fixture."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _loads(candidate: str) -> bool:
    try:
        serialization.load_pem_private_key(candidate.encode(), password=None)
    except (ValueError, TypeError):
        return False
    return True


def test_a_clean_key_is_left_working(pem: str) -> None:
    assert _loads(_normalise_pem(pem))


def test_the_single_line_json_form_works(pem: str) -> None:
    """Straight out of the service-account JSON, escapes intact."""
    assert _loads(_normalise_pem(pem.replace("\n", "\\n")))


def test_wrapping_quotes_are_stripped(pem: str) -> None:
    assert _loads(_normalise_pem(f'"{pem}"'))
    assert _loads(_normalise_pem(f"'{pem.replace(chr(10), chr(92) + 'n')}'"))


def test_the_half_expanded_form_that_broke_production(pem: str) -> None:
    """Each line ends with a literal backslash before a real newline: the `n` was
    consumed and the backslash was not. This is the exact shape the live key had,
    down to 65-character body lines where PEM uses 64."""
    mangled = pem.replace("\n", "\\\n")
    assert not _loads(mangled), "fixture is not actually broken"
    assert _loads(_normalise_pem(mangled))


def test_trailing_spaces_after_the_backslash_are_tolerated(pem: str) -> None:
    assert _loads(_normalise_pem(pem.replace("\n", "\\  \n")))


def test_a_genuinely_broken_key_still_fails(pem: str) -> None:
    """Normalisation must not paper over a truncated or wrong key."""
    truncated = "\n".join(pem.split("\n")[:5])
    assert not _loads(_normalise_pem(truncated))


def test_the_diagnosis_never_contains_the_key(pem: str) -> None:
    """`_describe_pem` reaches an API response, so it reports shape only."""
    body = pem.split("\n")[1]
    for candidate in (pem, pem.replace("\n", "\\\n"), "", "not a key"):
        message = _describe_pem(candidate)
        assert body not in message
        assert "BEGIN" not in message or "no BEGIN line" in message


def test_an_empty_value_says_so() -> None:
    assert _describe_pem("") == "it is empty"
    assert _describe_pem("   ") == "it is empty"
