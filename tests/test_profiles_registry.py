"""Named profile registry: CRUD, exact resolve, ambiguity, lock, CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browserctl.cli import main  # noqa: E402
from browserctl.errors import InvalidRequest, LeaseConflict  # noqa: E402
from browserctl.profiles import (  # noqa: E402
    ProfileAmbiguous,
    ProfileLockTimeout,
    ProfileNotFound,
    ProfileRegistry,
    launch_to_argv,
    validate_launch,
    validate_profile_name,
    validate_site,
)


@pytest.fixture
def reg_root(tmp_path: Path) -> Path:
    (tmp_path / "profiles").mkdir()
    return tmp_path


def test_validate_name_and_site():
    assert validate_profile_name("coal-hattie") == "coal-hattie"
    with pytest.raises(InvalidRequest):
        validate_profile_name("Bad Name")
    with pytest.raises(InvalidRequest):
        validate_profile_name("1leading")
    assert validate_site("x.ai") == "x.ai"
    assert validate_site("CPA-Manager") == "cpa-manager"
    with pytest.raises(InvalidRequest):
        validate_site("")


def test_validate_launch_minimal_no_lease_runtime_fields():
    out = validate_launch({"kind": "xai", "email": "a@b.com"})
    assert out == {"kind": "xai", "email": "a@b.com"}
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "xai"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "xai", "email": "a@b.com", "password": "x"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "xai", "email": "a@b.com", "owner": "orch"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "xai", "email": "a@b.com", "mode": "persistent"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "xai", "email": "a@b.com", "ttl": 60})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "vpn"})


def test_register_show_list_atomic(reg_root: Path):
    r = ProfileRegistry(reg_root)
    p = r.register(
        "coal-demo",
        launch={"kind": "xai", "email": "demo@example.com"},
    )
    assert p["name"] == "coal-demo"
    assert p["launch"]["email"] == "demo@example.com"
    assert "notes" not in p
    path = reg_root / "profiles" / "PROFILES.json"
    assert path.is_file()
    raw = json.loads(path.read_text())
    assert raw["version"] == 1
    assert "coal-demo" in raw["profiles"]

    shown = r.show("coal-demo")
    assert shown["launch"]["kind"] == "xai"
    listed = r.list_profiles()
    assert [x["name"] for x in listed] == ["coal-demo"]

    with pytest.raises(InvalidRequest):
        r.register("coal-demo", launch={"kind": "xai", "email": "other@example.com"})

    p2 = r.register(
        "coal-demo",
        launch={"kind": "xai", "email": "other@example.com"},
        replace=True,
    )
    assert p2["launch"]["email"] == "other@example.com"


def test_associate_and_resolve_strict_accountless(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register("coal-a", launch={"kind": "xai", "email": "a@example.com"})
    r.associate("coal-a", "x.ai", "a@example.com")
    r.associate("coal-a", "cpa-manager")  # site-only / accountless

    hit = r.resolve("x.ai", account="a@example.com")
    assert hit["name"] == "coal-a"
    assert hit["launch_argv"][:3] == ["launch", "--kind", "xai"]
    assert "--email" in hit["launch_argv"]
    assert "note" not in hit

    # accountless association resolves without --account
    hit2 = r.resolve("cpa-manager")
    assert hit2["name"] == "coal-a"

    # account-scoped only: without --account → NOT_FOUND + hint
    with pytest.raises(ProfileNotFound) as ei:
        r.resolve("x.ai")
    assert ei.value.details.get("hint")
    assert "account" in (ei.value.message or "").lower() or "account" in str(
        ei.value.details.get("hint")
    ).lower()

    with pytest.raises(ProfileNotFound):
        r.resolve("x.ai", account="missing@example.com")

    with pytest.raises(ProfileNotFound):
        r.resolve("unknown.site")


def test_resolve_accountless_does_not_match_account_scoped(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register("p1", launch={"kind": "xai", "email": "1@example.com"})
    r.associate("p1", "x.ai", "1@example.com")
    # even unique account-scoped row must not win without --account
    with pytest.raises(ProfileNotFound) as ei:
        r.resolve("x.ai")
    assert ei.value.code == "PROFILE_NOT_FOUND"
    assert "hint" in ei.value.details


def test_resolve_ambiguity_refused(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register("p1", launch={"kind": "xai", "email": "1@example.com"})
    r.register("p2", launch={"kind": "xai", "email": "2@example.com"})
    r.associate("p1", "x.ai")
    r.associate("p2", "x.ai")

    with pytest.raises(ProfileAmbiguous) as ei:
        r.resolve("x.ai")
    assert set(ei.value.details["matches"]) == {"p1", "p2"}

    r.associate("p1", "x.ai", "1@example.com")
    r.associate("p2", "x.ai", "2@example.com")
    assert r.resolve("x.ai", account="2@example.com")["name"] == "p2"


def test_resolve_never_creates_scratch(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register(
        "scratch-named",
        launch={"kind": "scratch", "label": "demo"},
    )
    r.associate("scratch-named", "example.com")
    out = r.resolve("example.com")
    assert out["launch"]["kind"] == "scratch"
    assert out["launch_argv"][0] == "launch"
    assert not (reg_root / "profiles" / "scratch").exists()


def test_launch_to_argv_deterministic():
    argv = launch_to_argv(
        {
            "kind": "vpn",
            "worker": "vpn-se-sto",
            "country": "Sweden",
            "attach_only": True,
        }
    )
    assert argv == [
        "launch",
        "--kind",
        "vpn",
        "--worker",
        "vpn-se-sto",
        "--country",
        "Sweden",
        "--attach-only",
    ]


def test_registry_lock_timeout_is_profile_not_lease(reg_root: Path):
    r = ProfileRegistry(reg_root)

    def boom(*_a, **_k):
        raise LeaseConflict("profiles", {"reason": "mutex_timeout"})

    with mock.patch("browserctl.profiles.mkdir_lock", side_effect=boom):
        with pytest.raises(ProfileLockTimeout) as ei:
            r.register("x", launch={"kind": "xai", "email": "a@b.com"})
    assert ei.value.code == "PROFILE_LOCK_TIMEOUT"
    assert ei.value.code != "LEASE_CONFLICT"


def test_cli_profiles_crud_and_resolve(reg_root: Path, capsys):
    root = str(reg_root)
    assert (
        main(
            [
                "--root",
                root,
                "profiles",
                "register",
                "coal-cli",
                "--kind",
                "xai",
                "--email",
                "cli@example.com",
                "--json",
            ]
        )
        == 0
    )
    reg_out = json.loads(capsys.readouterr().out)
    assert reg_out["ok"] is True
    assert reg_out["profile"]["name"] == "coal-cli"

    assert (
        main(
            [
                "--root",
                root,
                "profiles",
                "associate",
                "coal-cli",
                "x.ai",
                "cli@example.com",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--root",
                root,
                "--json",
                "profiles",
                "resolve",
                "x.ai",
                "--account",
                "cli@example.com",
            ]
        )
        == 0
    )
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is True
    assert resolved["name"] == "coal-cli"
    assert "launch_argv" in resolved

    # without --account, account-scoped only → not found + hint
    code = main(["--root", root, "profiles", "resolve", "x.ai", "--json"])
    assert code == 2
    missing = json.loads(capsys.readouterr().out)
    assert missing["error"]["code"] == "PROFILE_NOT_FOUND"
    assert missing["error"]["details"].get("hint")

    assert main(["--root", root, "profiles", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["profiles"][0]["name"] == "coal-cli"

    assert main(["--root", root, "profiles", "show", "missing", "--json"]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False
    assert err["error"]["code"] == "PROFILE_NOT_FOUND"


def test_cli_resolve_ambiguous(reg_root: Path, capsys):
    root = str(reg_root)
    for name, email in (("aa", "a@e.com"), ("bb", "b@e.com")):
        assert (
            main(
                [
                    "--root",
                    root,
                    "profiles",
                    "register",
                    name,
                    "--kind",
                    "xai",
                    "--email",
                    email,
                    "--json",
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "--root",
                    root,
                    "profiles",
                    "associate",
                    name,
                    "x.ai",
                    "--json",
                ]
            )
            == 0
        )
    capsys.readouterr()
    code = main(["--root", root, "profiles", "resolve", "x.ai", "--json"])
    assert code == 2
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "PROFILE_AMBIGUOUS"


def test_cli_lock_timeout_code(reg_root: Path, capsys):
    root = str(reg_root)

    def boom(*_a, **_k):
        raise LeaseConflict("profiles", {"reason": "mutex_timeout"})

    with mock.patch("browserctl.profiles.mkdir_lock", side_effect=boom):
        code = main(
            [
                "--root",
                root,
                "profiles",
                "register",
                "locked",
                "--kind",
                "xai",
                "--email",
                "l@e.com",
                "--json",
            ]
        )
    assert code == 1
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "PROFILE_LOCK_TIMEOUT"
