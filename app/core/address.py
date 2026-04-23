"""Argentina-flavored address normalization.

The input is the `shipping.receiver_address` payload from a ML order. We
produce a `NormalizedAddress` that:
- Trims whitespace and normalizes Unicode (NFKC).
- Expands common abbreviations (Av., Bv., Dto., etc.).
- Pulls street name and number into dedicated fields so SimpliRoute can
  geocode reliably even when the raw `address_line` is noisy.
- Returns a single `address_line` joining street + number + floor + dept,
  which is what SimpliRoute displays.

The goal is NOT perfect parsing — just good-enough cleanup so the downstream
geocoder gets a reasonable string, and otherwise the order is flagged for
manual review.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Trailing lookahead: abbreviation must end at whitespace, string end, or punctuation.
# We cannot use `\b` after `\.?` because `.` is a non-word char and the pattern fails
# to match the period in e.g. "Av. Corrientes".
_WORD_END = r"(?=\s|$|[,;.)])"
_ABBREVIATIONS = {
    rf"\bAv\.{_WORD_END}": "Avenida",
    rf"\bAv{_WORD_END}": "Avenida",
    rf"\bAvda\.?{_WORD_END}": "Avenida",
    rf"\bBv\.?{_WORD_END}": "Boulevard",
    rf"\bBlv\.?{_WORD_END}": "Boulevard",
    rf"\bCno\.?{_WORD_END}": "Camino",
    rf"\bPje\.?{_WORD_END}": "Pasaje",
    rf"\bN°{_WORD_END}": "número",
    rf"\bNº{_WORD_END}": "número",
    rf"\bDto\.?{_WORD_END}": "Depto",
    rf"\bDpto\.?{_WORD_END}": "Depto",
}


@dataclass
class NormalizedAddress:
    street_name: str
    street_number: str | None
    floor: str | None
    apartment: str | None
    city: str | None
    state: str | None
    zip_code: str | None
    country: str | None
    latitude: float | None
    longitude: float | None

    @property
    def address_line(self) -> str:
        parts = [self.street_name]
        if self.street_number:
            parts.append(self.street_number)
        extras: list[str] = []
        if self.floor:
            extras.append(f"piso {self.floor}")
        if self.apartment:
            extras.append(f"depto {self.apartment}")
        base = " ".join(parts).strip()
        if extras:
            base = f"{base} ({', '.join(extras)})"
        return base

    @property
    def full_line(self) -> str:
        """Address with city/state/zip/country for geocoding."""
        parts = [self.address_line]
        if self.city:
            parts.append(self.city)
        if self.state:
            parts.append(self.state)
        if self.zip_code:
            parts.append(f"CP {self.zip_code}")
        if self.country:
            parts.append(self.country)
        return ", ".join(p for p in parts if p)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    # NFKC for compatibility composition, strip weird whitespace.
    value = unicodedata.normalize("NFKC", value).strip()
    if not value:
        return None
    # Collapse internal whitespace.
    value = re.sub(r"\s+", " ", value)
    for pattern, replacement in _ABBREVIATIONS.items():
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value


def normalize_address(receiver_address: dict | None) -> NormalizedAddress | None:
    """Normalize a ML `shipping.receiver_address` payload.

    Returns `None` if the input is empty (nothing to work with).
    """
    if not receiver_address:
        return None

    street_name = _clean_text(receiver_address.get("street_name"))
    street_number = receiver_address.get("street_number")
    if street_number is not None:
        street_number = str(street_number).strip() or None

    # `comment` often holds "Piso 3 Depto B" etc. Try to parse it.
    comment = _clean_text(receiver_address.get("comment"))
    floor, apartment = _parse_floor_apartment(comment)

    # Some payloads carry explicit fields too.
    floor = floor or _clean_text(receiver_address.get("floor"))
    apartment = apartment or _clean_text(receiver_address.get("apartment"))

    city = _clean_text((receiver_address.get("city") or {}).get("name"))
    state = _clean_text((receiver_address.get("state") or {}).get("name"))
    country = _clean_text((receiver_address.get("country") or {}).get("name"))
    zip_code = _clean_text(receiver_address.get("zip_code"))

    # If we have no street_name but `address_line` is there, fall back.
    if not street_name:
        line = _clean_text(receiver_address.get("address_line"))
        if line:
            street_name = line

    return NormalizedAddress(
        street_name=street_name or "",
        street_number=street_number,
        floor=floor,
        apartment=apartment,
        city=city,
        state=state,
        zip_code=zip_code,
        country=country,
        latitude=_float(receiver_address.get("latitude")),
        longitude=_float(receiver_address.get("longitude")),
    )


def _parse_floor_apartment(comment: str | None) -> tuple[str | None, str | None]:
    if not comment:
        return None, None
    floor = None
    apartment = None
    m = re.search(r"piso\s*([0-9A-Za-z]+)", comment, flags=re.IGNORECASE)
    if m:
        floor = m.group(1)
    m = re.search(r"depto\s*([0-9A-Za-z]+)", comment, flags=re.IGNORECASE)
    if m:
        apartment = m.group(1)
    return floor, apartment


def _float(value) -> float | None:  # noqa: ANN001
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
