from __future__ import annotations

ISO_TO_PROVIDER: dict[str, str] = {
    "ru": "russia",
    "kz": "kazakhstan",
    "ua": "ukraine",
    "by": "belarus",
    "uz": "uzbekistan",
    "kg": "kyrgyzstan",
    "tj": "tajikistan",
    "am": "armenia",
    "ge": "georgia",
    "az": "azerbaijan",
    "md": "moldova",
    "lt": "lithuania",
    "lv": "latvia",
    "ee": "estonia",
    "pl": "poland",
    "de": "germany",
    "fr": "france",
    "gb": "england",
    "uk": "england",
    "us": "usa",
    "ca": "canada",
    "br": "brazil",
    "mx": "mexico",
    "ar": "argentina",
    "cl": "chile",
    "co": "colombia",
    "es": "spain",
    "it": "italy",
    "pt": "portugal",
    "nl": "netherlands",
    "be": "belgium",
    "ch": "switzerland",
    "at": "austria",
    "se": "sweden",
    "no": "norway",
    "dk": "denmark",
    "fi": "finland",
    "tr": "turkey",
    "in": "india",
    "id": "indonesia",
    "ph": "philippines",
    "vn": "vietnam",
    "th": "thailand",
    "my": "malaysia",
    "sg": "singapore",
    "cn": "china",
    "jp": "japan",
    "kr": "southkorea",
    "ae": "uae",
    "il": "israel",
    "eg": "egypt",
    "za": "southafrica",
    "ng": "nigeria",
    "ke": "kenya",
    "au": "australia",
    "nz": "newzealand",
}

PROVIDER_TO_ISO: dict[str, str] = {}
for _iso, _provider in ISO_TO_PROVIDER.items():
    PROVIDER_TO_ISO.setdefault(_provider, _iso)


def to_provider_country(country: str) -> str:
    value = country.strip().lower()
    if value in ISO_TO_PROVIDER:
        return ISO_TO_PROVIDER[value]
    if value in PROVIDER_TO_ISO:
        return value
    return value


def to_iso(country: str) -> str:
    value = country.strip().lower()
    return PROVIDER_TO_ISO.get(value, value)
