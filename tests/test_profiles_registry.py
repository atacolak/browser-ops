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
    out = validate_launch({"kind": "scratch", "label": "demo"})
    assert out == {"kind": "scratch", "label": "demo"}
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "xai", "email": "a@b.com"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "scratch", "label": "x", "password": "x"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "scratch", "label": "x", "owner": "orch"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "scratch", "label": "x", "mode": "persistent"})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "scratch", "label": "x", "ttl": 60})
    with pytest.raises(InvalidRequest):
        validate_launch({"kind": "vpn"})


def test_register_show_list_atomic(reg_root: Path):
    r = ProfileRegistry(reg_root)
    p = r.register(
        "lab-demo",
        launch={"kind": "scratch", "label": "demo"},
        description="lab-demo test face",
    )
    assert p["name"] == "lab-demo"
    assert p["description"] == "lab-demo test face"
    assert p["launch"]["label"] == "demo"
    assert "notes" not in p
    path = reg_root / "profiles" / "PROFILES.json"
    assert path.is_file()
    raw = json.loads(path.read_text())
    assert raw["version"] == 2
    assert "lab-demo" in raw["profiles"]

    shown = r.show("lab-demo")
    assert shown["launch"]["kind"] == "scratch"
    listed = r.list_profiles()
    assert [x["name"] for x in listed] == ["lab-demo"]

    with pytest.raises(InvalidRequest):
        r.register("lab-demo", launch={"kind": "scratch", "label": "other"})

    with pytest.raises(InvalidRequest):
        r.register("no-desc", launch={"kind": "scratch", "label": "x"})

    p2 = r.register(
        "lab-demo",
        launch={"kind": "scratch", "label": "other"},
        replace=True,
    )
    assert p2["launch"]["label"] == "other"
    assert p2["description"] == "lab-demo test face"


def test_associate_and_resolve_strict_accountless(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register("lab-a", launch={"kind": "scratch", "label": "a"}, description="lab-a test face")
    r.associate("lab-a", "github.com", "atacolak")
    r.associate("lab-a", "example.com")  # site-only / accountless

    hit = r.resolve("github.com", account="atacolak")
    assert hit["name"] == "lab-a"
    assert hit["launch_argv"][:3] == ["launch", "--kind", "scratch"]
    assert "--label" in hit["launch_argv"]
    assert "note" not in hit

    hit2 = r.resolve("example.com")
    assert hit2["name"] == "lab-a"

    with pytest.raises(ProfileNotFound) as ei:
        r.resolve("github.com")
    assert ei.value.details.get("hint")
    assert "account" in (ei.value.message or "").lower() or "account" in str(
        ei.value.details.get("hint")
    ).lower()

    with pytest.raises(ProfileNotFound):
        r.resolve("github.com", account="missing")

    with pytest.raises(ProfileNotFound):
        r.resolve("unknown.site")


def test_resolve_accountless_does_not_match_account_scoped(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register("p1", launch={"kind": "scratch", "label": "p1"}, description="p1 test face")
    r.associate("p1", "github.com", "one")
    with pytest.raises(ProfileNotFound) as ei:
        r.resolve("github.com")
    assert ei.value.code == "PROFILE_NOT_FOUND"
    assert "hint" in ei.value.details


def test_resolve_ambiguity_refused(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register("p1", launch={"kind": "scratch", "label": "p1"}, description="p1 test face")
    r.register("p2", launch={"kind": "scratch", "label": "p2"}, description="p2 test face")
    r.associate("p1", "github.com")
    r.associate("p2", "github.com")

    with pytest.raises(ProfileAmbiguous) as ei:
        r.resolve("github.com")
    assert set(ei.value.details["matches"]) == {"p1", "p2"}

    r.associate("p1", "github.com", "one")
    r.associate("p2", "github.com", "two")
    assert r.resolve("github.com", account="two")["name"] == "p2"


def test_resolve_never_creates_scratch(reg_root: Path):
    r = ProfileRegistry(reg_root)
    r.register(
        "scratch-named",
        launch={"kind": "scratch", "label": "demo"},
        description="scratch-named test face",
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
            r.register("x", launch={"kind": "scratch", "label": "x"}, description="x test face")
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
                "lab-cli",
                "--kind",
                "scratch",
                "--label",
                "cli",
                "--description",
                "lab-cli test face",
                "--json",
            ]
        )
        == 0
    )
    reg_out = json.loads(capsys.readouterr().out)
    assert reg_out["ok"] is True
    assert reg_out["profile"]["name"] == "lab-cli"

    assert (
        main(
            [
                "--root",
                root,
                "profiles",
                "associate",
                "lab-cli",
                "github.com",
                "atacolak",
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
                "github.com",
                "--account",
                "atacolak",
            ]
        )
        == 0
    )
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["ok"] is True
    assert resolved["name"] == "lab-cli"
    assert "launch_argv" in resolved

    code = main(["--root", root, "profiles", "resolve", "github.com", "--json"])
    assert code == 2
    missing = json.loads(capsys.readouterr().out)
    assert missing["error"]["code"] == "PROFILE_NOT_FOUND"
    assert missing["error"]["details"].get("hint")

    assert main(["--root", root, "profiles", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["profiles"][0]["name"] == "lab-cli"

    assert main(["--root", root, "profiles", "show", "missing", "--json"]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False
    assert err["error"]["code"] == "PROFILE_NOT_FOUND"


def test_cli_resolve_ambiguous(reg_root: Path, capsys):
    root = str(reg_root)
    for name, label in (("aa", "a"), ("bb", "b")):
        assert (
            main(
                [
                    "--root",
                    root,
                    "profiles",
                    "register",
                    name,
                    "--kind",
                    "scratch",
                    "--label",
                    label,
                    "--description",
                    f"{name} test face",
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
                    "github.com",
                    "--json",
                ]
            )
            == 0
        )
    capsys.readouterr()
    code = main(["--root", root, "profiles", "resolve", "github.com", "--json"])
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
                "scratch",
                "--label",
                "locked",
                "--description",
                "locked test face",
                "--json",
            ]
        )
    assert code == 1
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "PROFILE_LOCK_TIMEOUT"


def test_cards_stamp_and_egress(reg_root: Path, capsys):
    r = ProfileRegistry(reg_root)
    r.register(
        "github-ata",
        launch={"kind": "scratch", "label": "github-ata", "egress": "direct"},
        description="GitHub as atacolak. Issues, PRs.",
    )
    r.associate("github-ata", "github.com", "atacolak")
    stamped = r.stamp_verified("github-ata")
    assert stamped["last_verified_at"].endswith("Z")
    assert "GitHub as atacolak" in r.card("github-ata")
    cards = r.cards()
    assert "## github-ata" in cards
    assert "last_verified_at:" in cards

    scratch_vpn = validate_launch(
        {"kind": "scratch", "label": "x", "egress": {"type": "vpn", "country": "SE"}}
    )
    assert scratch_vpn["kind"] == "scratch"
    argv = launch_to_argv(scratch_vpn)
    assert argv[:3] == ["launch", "--kind", "scratch"]
    assert "--country" not in argv

    assert (
        main(
            [
                "--root",
                str(reg_root),
                "profiles",
                "cards",
                "--json",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert "github-ata" in out["markdown"]

    assert (
        main(["--root", str(reg_root), "profiles", "stamp", "github-ata", "--json"]) == 0
    )
    stamp_out = json.loads(capsys.readouterr().out)
    assert stamp_out["profile"]["last_verified_at"]
