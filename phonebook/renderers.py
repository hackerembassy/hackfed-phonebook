from __future__ import annotations

import logging
import xml.etree.ElementTree as ET


LOGGER = logging.getLogger("phonebook")
POLY_C60_MAX_CONTACTS = 500
POLY_C60_MAX_BYTES = 4 * 1024 * 1024


def _xml_text(value: str) -> str:
    # XML 1.0 forbids control characters and surrogate code points.
    return "".join(
        char for char in value
        if char in "\t\n\r" or "\u0020" <= char <= "\ud7ff"
        or "\ue000" <= char <= "\ufffd" or "\U00010000" <= char <= "\U0010ffff"
    )


def render_yealink_phonebook(extensions: list[dict[str, str]]) -> bytes:
    root = ET.Element("YealinkIPPhoneDirectory")
    for extension in extensions:
        entry = ET.SubElement(root, "DirectoryEntry")
        ET.SubElement(entry, "Name").text = _xml_text(extension["name"])[:99]
        ET.SubElement(entry, "Telephone").text = _xml_text(extension["extension"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _poly_name(value: str) -> str:
    # Limit encoded bytes without cutting a multi-byte UTF-8 character.
    return _xml_text(value).encode("utf-8")[:40].decode("utf-8", errors="ignore")


def render_c60_phonebook(extensions: list[dict[str, str]]) -> bytes:
    root = ET.Element("directory")
    items = ET.SubElement(root, "item_list")
    size = len(ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=False))
    contacts: set[str] = set()
    for extension in extensions:
        contact = extension["extension"].strip()
        # Never truncate or repair a dial string into a different destination.
        if (
            not contact or len(contact) > 128 or _xml_text(contact) != contact
            or contact in contacts
        ):
            continue
        name = _xml_text(extension["name"]).strip() or contact
        parts = name.split(maxsplit=1)
        entry = ET.Element("item")
        for tag, value in (
            ("fn", _poly_name(parts[0])),
            ("ln", _poly_name(parts[1]) if len(parts) > 1 else ""),
            ("ct", contact),
            ("lb", _poly_name(name)),
        ):
            ET.SubElement(entry, tag).text = value
        entry_size = len(ET.tostring(entry, encoding="utf-8", short_empty_elements=False))
        if len(items) >= POLY_C60_MAX_CONTACTS or size + entry_size > POLY_C60_MAX_BYTES:
            break
        items.append(entry)
        contacts.add(contact)
        size += entry_size
    if len(items) < len(extensions):
        LOGGER.warning("C60 directory omitted %d contacts due to invalid/duplicate dial strings or device limits", len(extensions) - len(items))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=False)