#!/usr/bin/env python3
"""Export the leads imported into Warmbly to VCF files for vdirsyncer.

iCloud only exposes a single CardDAV address book, so the cards are written
into the synced collection (~/.contacts/card) and gathered under an Apple
"Leads" group card instead of a separate address book.
"""

import os
import sys
import json
import subprocess
import psycopg2
import psycopg2.extras
import vobject
from pathlib import Path


OUTPUT_DIR = Path(os.environ.get("VCF_OUTPUT_DIR", Path.home() / ".contacts" / "card"))
WARMBLY_BIN = os.environ.get("WARMBLY_BIN", "warmbly")
GROUP_NAME = os.environ.get("LEADS_GROUP_NAME", "Leads")
GROUP_UID = "leads-group@leads"
UID_PREFIX = "leads-business-"


def make_uid(row_id) -> str:
    return f"{UID_PREFIX}{row_id}@leads"


def safe_str(val) -> str:
    return str(val).strip() if val else ""


def fetch_warmbly_contacts() -> dict:
    """Return {leads_business_id: warmbly contact} for every linked contact."""
    out = subprocess.run(
        [WARMBLY_BIN, "contact", "list", "--all", "--limit", "100",
         "--max-pages", "1000", "--json"],
        check=True, capture_output=True, text=True,
    ).stdout
    contacts = {}
    for c in json.loads(out)["data"]:
        business_id = safe_str((c.get("custom_fields") or {}).get("leads_business_id"))
        if business_id:
            contacts[business_id] = c
    return contacts


def build_vcard(row: dict, contact: dict) -> vobject.vCard:
    card = vobject.vCard()
    fields = contact.get("custom_fields") or {}

    name = safe_str(row.get("name"))
    phone = safe_str(row.get("phone")) or safe_str(contact.get("phone"))
    website = safe_str(row.get("website"))
    address = safe_str(row.get("address"))
    city = safe_str(row.get("city"))
    state = safe_str(row.get("state"))
    zip_code = safe_str(row.get("zip"))
    first = safe_str(contact.get("first_name"))
    last = safe_str(contact.get("last_name"))
    email = safe_str(contact.get("email"))
    title = safe_str(fields.get("decision_maker_title"))

    card.add("uid").value = make_uid(row["id"])

    # List under the company name in Apple Contacts, keep the person in N
    card.add("fn").value = name
    card.add("n").value = vobject.vcard.Name(family=last, given=first)
    card.add("org").value = [name]
    card.add("x-abshowas").value = "COMPANY"

    if title:
        card.add("title").value = title

    if email:
        em = card.add("email")
        em.value = email
        em.type_param = "WORK"

    if phone:
        tel = card.add("tel")
        tel.value = phone
        tel.type_param = "WORK"

    if website:
        url = card.add("url")
        url.value = website
        url.type_param = "HOMEPAGE"

    if any([address, city, state, zip_code]):
        adr = card.add("adr")
        adr.type_param = "WORK"
        # vobject Address: box, extended, street, city, region, code, country
        adr.value = vobject.vcard.Address(
            street=address,
            city=city,
            region=state,
            code=zip_code,
            country="",
        )

    return card


def build_group(member_uids: list) -> vobject.vCard:
    group = vobject.vCard()
    group.add("uid").value = GROUP_UID
    group.add("fn").value = GROUP_NAME
    group.add("n").value = vobject.vcard.Name(family=GROUP_NAME)
    group.add("x-addressbookserver-kind").value = "group"
    for uid in member_uids:
        group.add("x-addressbookserver-member").value = f"urn:uuid:{uid}"
    return group


def write_if_changed(path: Path, text: str) -> bool:
    # Rewriting identical cards would make vdirsyncer re-upload them
    # Compare bytes: read_text() would translate vobject's CRLF and never match
    data = text.encode()
    if path.exists() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def main():
    db_url = os.environ.get("LEADS_DB_URL")
    if not db_url:
        print("ERROR: LEADS_DB_URL environment variable not set", file=sys.stderr)
        sys.exit(1)

    contacts = fetch_warmbly_contacts()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, phone, website, address, city, state, zip"
                " FROM leads.businesses"
                " WHERE name IS NOT NULL"
                "   AND id::text = ANY(%s)"
                " ORDER BY id",
                (list(contacts),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    uids = []
    changed = 0
    for row in rows:
        if not safe_str(row.get("name")):
            continue
        uid = make_uid(row["id"])
        card = build_vcard(row, contacts[str(row["id"])])
        changed += write_if_changed(OUTPUT_DIR / f"{uid}.vcf", card.serialize())
        uids.append(uid)

    write_if_changed(OUTPUT_DIR / f"{GROUP_UID}.vcf", build_group(uids).serialize())

    # Drop cards for leads that are no longer in Warmbly
    keep = {f"{uid}.vcf" for uid in uids}
    removed = 0
    for path in OUTPUT_DIR.glob(f"{UID_PREFIX}*.vcf"):
        if path.name not in keep:
            path.unlink()
            removed += 1

    print(
        f"{len(uids)} leads in group '{GROUP_NAME}' at {OUTPUT_DIR}"
        f" ({changed} written, {removed} removed,"
        f" {len(contacts) - len(uids)} Warmbly contacts with no matching business)"
    )


if __name__ == "__main__":
    main()
