from __future__ import annotations

from app.core.address import normalize_address


def _base(**overrides) -> dict:
    payload = {
        "address_line": "Av. Corrientes 1234",
        "street_name": "Av. Corrientes",
        "street_number": "1234",
        "comment": "",
        "zip_code": "C1043AAZ",
        "city": {"name": "Ciudad Autónoma de Buenos Aires"},
        "state": {"name": "Buenos Aires"},
        "country": {"name": "Argentina"},
        "latitude": -34.6037,
        "longitude": -58.3816,
    }
    payload.update(overrides)
    return payload


def test_none_returns_none():
    assert normalize_address(None) is None


def test_empty_dict_returns_none():
    # Empty payload is treated the same as None — nothing to normalize.
    assert normalize_address({}) is None


def test_happy_path_expands_abbreviations():
    n = normalize_address(_base())
    assert n is not None
    assert n.street_name == "Avenida Corrientes"
    assert n.street_number == "1234"
    assert n.city == "Ciudad Autónoma de Buenos Aires"
    assert n.country == "Argentina"
    assert n.latitude == -34.6037


def test_boulevard_abbreviation():
    n = normalize_address(_base(street_name="Bv. San Juan"))
    assert n.street_name == "Boulevard San Juan"


def test_strips_and_collapses_whitespace():
    n = normalize_address(_base(street_name="  Av.    Rivadavia   "))
    assert n.street_name == "Avenida Rivadavia"


def test_preserves_accents_via_nfkc():
    n = normalize_address(_base(street_name="Ñuñez 456", city={"name": "Martínez"}))
    assert n.street_name == "Ñuñez 456"
    assert n.city == "Martínez"


def test_parses_floor_and_apartment_from_comment():
    n = normalize_address(_base(comment="Piso 3 Depto B"))
    assert n.floor == "3"
    assert n.apartment == "B"


def test_parses_apartment_only():
    n = normalize_address(_base(comment="Depto 5A"))
    assert n.floor is None
    assert n.apartment == "5A"


def test_dpto_alternate_spelling_expands():
    # Dto./Dpto. abbreviations get expanded to Depto before parsing.
    n = normalize_address(_base(comment="Dto 2"))
    assert n.apartment == "2"


def test_missing_street_number_is_none():
    n = normalize_address(_base(street_number=""))
    assert n.street_number is None


def test_falls_back_to_address_line_when_no_street_name():
    n = normalize_address(_base(street_name="", address_line="Av. Santa Fe 3000"))
    assert n.street_name == "Avenida Santa Fe 3000"


def test_full_line_joins_all_components():
    n = normalize_address(_base(comment="Piso 5 Depto A"))
    line = n.full_line
    assert "Avenida Corrientes 1234" in line
    assert "piso 5" in line
    assert "depto A" in line
    assert "Ciudad Autónoma de Buenos Aires" in line
    assert "Argentina" in line


def test_invalid_latitude_becomes_none():
    n = normalize_address(_base(latitude="not-a-number"))
    assert n.latitude is None


def test_numero_sign_expansion():
    n = normalize_address(_base(street_name="Calle Falsa N° 123"))
    # "N°" expands to "número" regardless of casing.
    assert "número" in n.street_name.lower()
