from dataclasses import dataclass
import json
import os
import threading
import time
from typing import Any, Dict, List
import urllib.request

from .translator import translate_catalog_delta


@dataclass
class RefObject:
    id: int


CATEGORY_CACHE_FILE = "cueceramica_category_map.json"
RAW_BACKUP_FILE = "cueceramica_raw_backup.json"

RAM_CATALOG_CACHE: Dict[str, List[Dict[str, Any]]] = {}
LAST_SYNC_TIME: float = 0.0
SYNC_LOCK = threading.Lock()


def get_or_update_category_map(items: List[Dict[str, Any]], start_id: int = 201) -> Dict[str, int]:
    cat_map: Dict[str, int] = {}
    if os.path.exists(CATEGORY_CACHE_FILE):
        try:
            with open(CATEGORY_CACHE_FILE, "r", encoding="utf-8") as f:
                cat_map = json.load(f)
        except Exception:
            cat_map = {}

    next_id = max(cat_map.values(), default=start_id - 1) + 1
    updated = False

    for item in items:
        cat_ids = (item.get("categoryIds") or []) + (
            item.get("structuredContent", {}).get("relatedCategoryIds") or []
        )
        for cid in cat_ids:
            if cid not in cat_map:
                cat_map[cid] = next_id
                next_id += 1
                updated = True

    if updated:
        try:
            with open(CATEGORY_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cat_map, f, indent=2)
        except Exception:
            pass

    return cat_map


def build_product_body(
    item: Dict[str, Any],
    translated_fields: Dict[str, str],
    cat: RefObject,
    shop: RefObject,
    sp: RefObject,
    lang: str,
) -> Dict[str, Any]:
    images: List[str] = [
        img["assetUrl"]
        for img in (item.get("items") or [])
        if img.get("assetUrl")
    ]
    if not images and item.get("assetUrl"):
        images = [item["assetUrl"]]

    raw_variants = (
        item.get("variants")
        or item.get("structuredContent", {}).get("variants")
        or []
    )
    inventory: List[Dict[str, Any]] = []

    for v in raw_variants:
        attrs = v.get("attributes") or {}
        price_money = v.get("priceMoney") or {}
        price = (
            float(price_money["value"])
            if "value" in price_money
            else round(float(v.get("price", 0)) / 100.0, 2)
        )

        inventory.append(
            {
                "sku": v.get("sku", ""),
                "price": price,
                "currency": price_money.get("currency", "EUR"),
                "quantity": int(v.get("qtyInStock", 0)),
                "attributes": attrs,
            }
        )

    translations = {
        lang: {
            "title": translated_fields["title"],
            "description": translated_fields["description"],
        }
    }

    return {
        "expected_images": len(images),
        "translations": json.dumps(translations, ensure_ascii=False),
        "is_visible": True,
        "inspiration_country": ["FR"],
        "category": cat.id,
        "shop": shop.id,
        "shipping_profiles": [sp.id],
        "product_attributes": [],
        "inventory": inventory,
        "customizable": False,
    }


def fetch_squarespace_items() -> List[Dict[str, Any]]:
    url = "https://www.cueceramica.com/tienda?format=json-pretty"
    last_err = None

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                items = json.loads(r.read().decode("utf-8")).get("items", [])
                if items:
                    try:
                        with open(RAW_BACKUP_FILE, "w", encoding="utf-8") as f:
                            json.dump(items, f, ensure_ascii=False)
                    except Exception:
                        pass
                    return items
        except Exception as exc:
            last_err = exc
            time.sleep(0.6 * (attempt + 1))

    if os.path.exists(RAW_BACKUP_FILE):
        with open(RAW_BACKUP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError(f"Cannot reach www.cueceramica.com: {last_err}")


def build_catalog_from_items(
    items: List[Dict[str, Any]],
    lang: str,
    shop_id: int = 101,
    sp_id: int = 501,
) -> List[Dict[str, Any]]:
    cat_map = get_or_update_category_map(items)
    translated_list = translate_catalog_delta(items, lang)

    shop = RefObject(id=shop_id)
    sp = RefObject(id=sp_id)
    results: List[Dict[str, Any]] = []

    for item, trans_fields in zip(items, translated_list):
        cat_ids = (item.get("categoryIds") or []) + (
            item.get("structuredContent", {}).get("relatedCategoryIds") or []
        )
        matched_cat_id = next(
            (cat_map[cid] for cid in cat_ids if cid in cat_map),
            200,
        )
        cat = RefObject(id=matched_cat_id)
        results.append(build_product_body(item, trans_fields, cat, shop, sp, lang))

    output_file = f"cueceramica_{lang}_product_body.json"
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    RAM_CATALOG_CACHE[lang] = results
    return results


def sync_all_in_background() -> None:
    global LAST_SYNC_TIME
    if not SYNC_LOCK.acquire(blocking=False):
        return
    try:
        items = fetch_squarespace_items()
        for lang in ["fr", "en"]:
            build_catalog_from_items(items, lang)
        LAST_SYNC_TIME = time.time()
    except Exception:
        pass
    finally:
        SYNC_LOCK.release()


def get_instant_catalog(lang: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
    global LAST_SYNC_TIME
    output_file = f"cueceramica_{lang}_product_body.json"

    if not force_refresh:
        if lang in RAM_CATALOG_CACHE:
            if time.time() - LAST_SYNC_TIME > 300:
                threading.Thread(target=sync_all_in_background, daemon=True).start()
            return RAM_CATALOG_CACHE[lang]

        if os.path.exists(output_file):
            try:
                with open(output_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data:
                    RAM_CATALOG_CACHE[lang] = data
                    LAST_SYNC_TIME = os.path.getmtime(output_file)
                    if time.time() - LAST_SYNC_TIME > 300:
                        threading.Thread(target=sync_all_in_background, daemon=True).start()
                    return data
            except Exception:
                pass

    items = fetch_squarespace_items()
    LAST_SYNC_TIME = time.time()
    return build_catalog_from_items(items, lang)