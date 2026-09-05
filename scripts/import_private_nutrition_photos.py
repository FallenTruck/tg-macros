#!/usr/bin/env python3
"""Explicit opt-in import of authorized Telegram meal references into ignored local fixtures."""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.e2e_support import dev_resources, user_partition_items, get_parameter
from scripts.nutrition_corpus import DEFAULT_MANIFEST, digest, image_path, load_cases
from macro_bot.nutrition_lab import validate_image

PRIVATE_ROOT = DEFAULT_MANIFEST.parents[2] / "artifacts/nutrition/private"


def photo_references(items):
    """Only retained meal/detail metadata; no Telegram history or update polling."""
    found = {}
    def visit(value, caption="", created_at=""):
        if isinstance(value, dict):
            caption = str(value.get("caption", caption) or "")
            created_at = str(value.get("created_at", created_at) or "")
            if value.get("telegram_file_id"):
                found.setdefault(str(value["telegram_file_id"]), {"caption": caption, "created_at": created_at})
            for child in value.values():
                visit(child, caption, created_at)
        elif isinstance(value, list):
            for child in value:
                visit(child, caption, created_at)
    visit(items)
    return sorted(found.items(), key=lambda item: item[1]["created_at"], reverse=True)


async def import_photos(accounts, limit):
    from boto3.dynamodb.conditions import Attr
    from PIL import Image
    from telegram import Bot
    session, table, _outputs, _repo = dev_resources()
    # Read identities only; then query meal references in explicitly authorized partitions.
    filters = []
    if "pooja" in accounts:
        filters.append(Attr("display_name").contains("Pooja") | Attr("username").contains("pooja"))
    if "vaanavan" in accounts:
        filters.append(Attr("display_name").contains("Vaan") | Attr("username").contains("vaan"))
    name_filter = filters[0]
    for extra in filters[1:]:
        name_filter |= extra
    query = {"FilterExpression": Attr("entity_type").eq("identity") & Attr("PK").begins_with("IDENTITY#TELEGRAM#") & name_filter,
             "ProjectionExpression": "PK, user_id, display_name, username, telegram_user_id"}
    identities = []
    while True:
        page = table.scan(**query)
        identities.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    stack = session.client("cloudformation").describe_stacks(StackName="tg-macros-dev")["Stacks"][0]
    parameter = next(p["ParameterValue"] for p in stack["Parameters"] if p["ParameterKey"] == "BotTokenParameterName")
    token = get_parameter(session.client("ssm"), parameter)
    if not token:
        raise RuntimeError("Bot credential unavailable")
    manifest = PRIVATE_ROOT / "manifest.json"
    cases = json.loads(manifest.read_text()) if manifest.exists() else [
        {**case, "image": str(image_path(case))} for case in load_cases()]
    seen = {c["image_sha256"] for c in cases}
    imported = dict.fromkeys(accounts, 0)
    existing_refs = {c.get("source", {}).get("reference_hash") for c in cases}
    PRIVATE_ROOT.joinpath("images").mkdir(parents=True, exist_ok=True)
    async with Bot(token) as bot:
        for identity in identities:
            name = (identity.get("display_name", "") + " " + identity.get("username", "")).lower()
            account = "pooja" if "pooja" in name else "vaanavan"
            if account not in accounts or not identity.get("telegram_user_id"):
                continue
            for file_id, metadata in photo_references(user_partition_items(table, identity["user_id"])):
                if imported[account] >= limit:
                    break
                reference_hash = digest(file_id)
                if reference_hash in existing_refs:
                    continue
                try:
                    file = await bot.get_file(file_id)
                    buffer = io.BytesIO()
                    await file.download_to_memory(out=buffer)
                    raw = buffer.getvalue()
                    validate_image(raw)
                except Exception as err:
                    print(f"{account}: photo unavailable ({type(err).__name__})", flush=True)
                    continue
                sha = digest(raw)
                if sha in seen:
                    continue
                extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[Image.open(io.BytesIO(raw)).format]
                case_id = f"private-{account}-{sha[:12]}"
                relative = f"images/{case_id}.{extension}"
                PRIVATE_ROOT.joinpath(relative).write_bytes(raw)
                cases.append({"id": case_id, "image": relative, "image_sha256": sha,
                    "food_type": "personal_meal_pending_visual_review", "caption": metadata["caption"][:1000],
                    "label_status": "user_caption_only", "expected_visible_components": [],
                    "known_portions_g": {}, "acceptable_macro_range": {}, "expected_follow_up": None,
                    "variation_tags": ["personal_telegram_submission"],
                    "source": {"kind": "authorized_private_telegram_submission", "account": account,
                               "reference_hash": reference_hash, "submitted_at": metadata["created_at"],
                               "imported_at": datetime.now(timezone.utc).isoformat(), "redistribution": "private_local_only"}})
                seen.add(sha)
                imported[account] += 1
                manifest.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
                print(f"{account}: imported {imported[account]} private photo(s)", flush=True)
    print(f"Private manifest: {manifest}")
    print("Images and private metadata stay under ignored artifacts/nutrition/.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", choices=("pooja", "vaanavan"), action="append", required=True,
                        help="Use only with the account owner's explicit authorization")
    parser.add_argument("--limit", type=int, default=4, help="maximum new unique images per account")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be 1–20")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    try:
        asyncio.run(import_photos(set(args.account), args.limit))
    except Exception as err:
        print(f"Private photo import failed ({type(err).__name__}); no credentials are printed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
