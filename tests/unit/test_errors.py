"""Pin the moonlit error hierarchy to the D3 / specs/01-cli.md §6 exit-code map."""

import pytest

from moonlit import errors

EXPECTED_EXIT_CODES: list[tuple[type["errors.MoonlitError"], int]] = [
    (errors.UvNotFoundError, 3),
    (errors.NoLockfileError, 4),
    (errors.NotAWorkspaceError, 5),
    (errors.UnknownPackageError, 5),
    (errors.MissingPackageError, 5),
    (errors.MalformedPyprojectError, 5),
    (errors.BadEntryPointError, 6),
    (errors.ConsoleScriptNotFoundError, 6),
    (errors.OutputExistsError, 7),
    (errors.OutputNotWritableError, 7),
    (errors.ExportError, 8),
    (errors.CompileError, 8),
    (errors.StagingError, 9),
    (errors.WheelArtifactError, 10),
    (errors.InternalError, 11),
]


def test_moonlit_error_is_an_exception() -> None:
    assert issubclass(errors.MoonlitError, Exception)


@pytest.mark.parametrize("cls,expected_code", EXPECTED_EXIT_CODES)
def test_subclass_has_stable_exit_code(
    cls: type["errors.MoonlitError"], expected_code: int
) -> None:
    assert issubclass(cls, errors.MoonlitError)
    assert cls.exit_code == expected_code
    # Inspectable on the class itself (no instantiation required).
    assert isinstance(cls.__dict__["exit_code"], int)


@pytest.mark.parametrize("cls,_expected", EXPECTED_EXIT_CODES)
def test_instance_carries_message_and_exit_code(
    cls: type["errors.MoonlitError"], _expected: int
) -> None:
    instance = cls("some message")
    assert instance.exit_code == cls.exit_code
    assert str(instance) == "some message"


def test_subclass_names_are_unique() -> None:
    names = [cls.__name__ for cls, _ in EXPECTED_EXIT_CODES]
    assert len(names) == len(set(names))


def test_no_subclass_uses_a_reserved_exit_code() -> None:
    # 0 = success, 1 = unhandled bug, 2 = parser-level usage error, 130 = SIGINT.
    # None of these are surfaced via MoonlitError per D3.
    reserved = {0, 1, 2, 130}
    used = {cls.exit_code for cls, _ in EXPECTED_EXIT_CODES}
    assert used.isdisjoint(reserved)


def test_exit_codes_cover_the_full_d3_map() -> None:
    # Every translatable build-time code (3..11) must be claimed by at least one class.
    expected = set(range(3, 12))
    used = {cls.exit_code for cls, _ in EXPECTED_EXIT_CODES}
    assert used == expected
