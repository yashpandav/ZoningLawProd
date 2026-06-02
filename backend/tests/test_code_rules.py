"""Tests for the single-source OBC constants in packgen.rules.code_rules."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from packgen.rules.code_rules import (
    ROOM_MIN_AREA_M2,
    ROOM_MIN_DIM_M,
    ROOM_MAX_AREA_M2,
    VALID_ROLES,
    EGRESS_ROLES,
    ROLE_ALIASES,
    normalize_role,
)


def test_bedroom_obc_area():
    assert ROOM_MIN_AREA_M2["bedroom"] == 7.0       # OBC §9.8.3.2
    assert ROOM_MIN_AREA_M2["kitchen"] == 4.5       # OBC §9.8.3.2
    assert ROOM_MIN_AREA_M2["bathroom"] == 3.0      # OBC §9.8.3.4


def test_bedroom_obc_dim():
    assert ROOM_MIN_DIM_M["bedroom"] == 2.1         # OBC §9.8.3.2
    assert ROOM_MIN_DIM_M["stair"] == 0.86          # OBC §9.8.2


def test_valid_roles_covers_all_15():
    # Updated to 16 when "garage" was added as a first-class parking-space role
    assert len(VALID_ROLES) == 16
    assert "master_bedroom" in VALID_ROLES
    assert "garage" in VALID_ROLES


def test_egress_roles():
    assert EGRESS_ROLES == {"bedroom", "master_bedroom"}


def test_normalize_role_aliases():
    assert normalize_role("lounge") == "living"
    assert normalize_role("WC") == "powder_room"
    assert normalize_role("Foyer") == "entry"
    assert normalize_role("stairs") == "stair"
    assert normalize_role("unknown_xyz") == "storage"


def test_max_exceeds_min():
    for role in ROOM_MIN_AREA_M2:
        if role in ROOM_MAX_AREA_M2:
            assert ROOM_MAX_AREA_M2[role] >= ROOM_MIN_AREA_M2[role], (
                f"{role}: max {ROOM_MAX_AREA_M2[role]} < min {ROOM_MIN_AREA_M2[role]}"
            )


def test_all_valid_roles_have_min_area():
    for role in VALID_ROLES:
        assert role in ROOM_MIN_AREA_M2, f"Role {role!r} missing from ROOM_MIN_AREA_M2"


def test_all_valid_roles_have_min_dim():
    for role in VALID_ROLES:
        assert role in ROOM_MIN_DIM_M, f"Role {role!r} missing from ROOM_MIN_DIM_M"
