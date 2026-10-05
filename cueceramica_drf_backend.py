# ==============================================================================
# IMPORTS: Standard Library and External Frameworks
# ==============================================================================
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import html
import json
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List
import urllib.parse
import urllib.request

import django
from django.conf import settings

# ==============================================================================
# 1. STANDALONE DJANGO CONFIGURATION
# ==============================================================================
if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="local-dev-secret-key",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.staticfiles",
            "rest_framework",
        ],
        STATIC_URL="/static/",
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "APP_DIRS": True,
            }
        ],
        REST_FRAMEWORK={
            "UNAUTHENTICATED_USER": None,
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.BrowsableAPIRenderer",
                "rest_framework.renderers.JSONRenderer",
            ],
        },
    )
    django.setup()

from django.urls import path
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


# ==============================================================================
# 2. PERSISTENT CATEGORY MAP & IN-MEMORY RAM CACHE
# ==============================================================================
@dataclass
class RefObject:
    id: int


CATEGORY_CACHE_FILE = "cueceramica_category_map.json"
RAW_BACKUP_FILE = "cueceramica_raw_backup.json"
TRANSLATION_STORE_FILE = "cueceramica_translations_v7.json"

# Instant RAM cache so API requests respond in ~0.002 seconds
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


# ==============================================================================
# 3. HTML CLEANING & DESCRIPTION EXTRACTION
# ==============================================================================
def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|ul|h[1-6]|div)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{2,}", "\n", html.unescape(text)).strip()


def extract_clean_description(item: Dict[str, Any]) -> str:
    raw_html = item.get("excerpt") or item.get("body") or ""
    if not raw_html:
        return (item.get("seoData") or {}).get("seoDescription", "")

    without_ficha = re.split(
        r"<h[1-6][^>]*>\s*Ficha\s+t[eé]cnica\s*</h[1-6]>",
        raw_html,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    without_ul = re.sub(r"<ul[^>]*>.*?</ul>", "", without_ficha, flags=re.I | re.S)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", without_ul, flags=re.I | re.S)
    narrative_paragraphs: List[str] = []
    for p in paragraphs:
        if re.match(r"^\s*<strong>[^<]{1,35}</strong>\s*:?\s*[^<]+$", p.strip(), flags=re.I):
            continue
        clean_p = clean_html(p)
        if clean_p:
            narrative_paragraphs.append(clean_p)

    if narrative_paragraphs:
        return "\n\n".join(narrative_paragraphs)

    return clean_html(without_ul) or clean_html(raw_html)


# ==============================================================================
# 4. PARALLEL MULTI-QUERY BATCH TRANSLATOR
# ==============================================================================
translation_store: Dict[str, Dict[str, Dict[str, str]]] = {"fr": {}, "en": {}}

if os.path.exists(TRANSLATION_STORE_FILE):
    try:
        with open(TRANSLATION_STORE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                translation_store.update(loaded)
    except Exception:
        pass


def save_translation_store() -> None:
    try:
        with open(TRANSLATION_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(translation_store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def translate_multi_q_batch(args: tuple[List[tuple[str, str]], str]) -> List[str]:
    pairs, lang = args
    url = f"https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=es&tl={lang}"
    data = urllib.parse.urlencode(pairs).encode("utf-8")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode("utf-8"))
                return [str(x[0] if isinstance(x, list) else x).strip() for x in res]
        except Exception:
            time.sleep(0.5 * (attempt + 1))

    return [text for _, text in pairs]


def translate_catalog_delta(items: List[Dict[str, Any]], lang: str) -> List[Dict[str, str]]:
    extracted = [
        {
            "title": (it.get("title") or "").strip(),
            "description": extract_clean_description(it),
        }
        for it in items
    ]

    if lang == "es":
        return extracted

    lang_cache = translation_store.setdefault(lang, {})
    results: List[Dict[str, str]] = [None] * len(extracted)  # type: ignore
    missing_indices: List[int] = []

    for idx, entry in enumerate(extracted):
        cache_key = f"{entry['title']}|||{entry['description']}"
        cached_val = lang_cache.get(cache_key)
        if cached_val and cached_val.get("title") and cached_val["title"] != entry["title"]:
            results[idx] = cached_val
        else:
            missing_indices.append(idx)

    # Run all missing batches in parallel at the same time!
    if missing_indices:
        batch_size = 25
        batches_idxs = [
            missing_indices[i : i + batch_size]
            for i in range(0, len(missing_indices), batch_size)
        ]
        batch_tasks = []
        for b_idxs in batches_idxs:
            pairs: List[tuple[str, str]] = []
            for idx in b_idxs:
                pairs.append(("q", extracted[idx]["title"] or "-"))
                pairs.append(("q", extracted[idx]["description"] or "-"))
            batch_tasks.append((pairs, lang))

        with ThreadPoolExecutor(max_workers=len(batch_tasks)) as executor:
            batch_outputs = list(executor.map(translate_multi_q_batch, batch_tasks))

        for b_idxs, translated_strings in zip(batches_idxs, batch_outputs):
            for pos, idx in enumerate(b_idxs):
                t_title = translated_strings[pos * 2]
                t_desc = translated_strings[pos * 2 + 1]
                if t_desc == "-":
                    t_desc = ""
                trans_dict = {"title": t_title, "description": t_desc}
                results[idx] = trans_dict

                if t_title != extracted[idx]["title"]:
                    cache_key = f"{extracted[idx]['title']}|||{extracted[idx]['description']}"
                    lang_cache[cache_key] = trans_dict

        save_translation_store()

    return results


# ==============================================================================
# 5. DRF SERIALIZER
# ==============================================================================
class ProductBodySerializer(serializers.Serializer):
    expected_images = serializers.IntegerField(min_value=0)
    translations = serializers.CharField()
    is_visible = serializers.BooleanField(default=True)
    inspiration_country = serializers.ListField(child=serializers.CharField())
    category = serializers.IntegerField()
    shop = serializers.IntegerField()
    shipping_profiles = serializers.ListField(child=serializers.IntegerField())
    product_attributes = serializers.ListField(child=serializers.DictField(), default=list)
    inventory = serializers.ListField(child=serializers.DictField())
    customizable = serializers.BooleanField(default=False)


# ==============================================================================
# 6. PRODUCT ITEM BUILDER & INSTANT CACHED LOADER
# ==============================================================================
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

    product_body = {
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

    return product_body


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
    """Silently checks Squarespace in the background for new products/categories."""
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
    """
    1. Returns from RAM in 0.001s if available.
    2. Otherwise loads from cueceramica_<lang>_product_body.json on disk in 0.005s.
    3. Triggers a background sync if cache is older than 5 minutes so the user never waits!
    """
    global LAST_SYNC_TIME
    output_file = f"cueceramica_{lang}_product_body.json"

    if not force_refresh:
        # 1. Check RAM first (0.001s)
        if lang in RAM_CATALOG_CACHE:
            if time.time() - LAST_SYNC_TIME > 300:
                threading.Thread(target=sync_all_in_background, daemon=True).start()
            return RAM_CATALOG_CACHE[lang]

        # 2. Check Disk second (0.005s)
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

    # 3. First run or ?refresh=1: fetch and build live
    items = fetch_squarespace_items()
    LAST_SYNC_TIME = time.time()
    return build_catalog_from_items(items, lang)


# ==============================================================================
# 7. DJANGO REST FRAMEWORK API VIEW (Instant 0.002s Response)
# ==============================================================================
class CueCeramicaProductsAPIView(APIView):
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        raw_lang = (
            request.query_params.get("lang")
            or request.query_params.get("wglang")
            or "fr"
        ).lower()
        target_lang = "en" if "en" in raw_lang else ("es" if "es" in raw_lang else "fr")
        force_refresh = bool(request.query_params.get("refresh"))

        results = get_instant_catalog(target_lang, force_refresh=force_refresh)
        return Response(results, status=status.HTTP_200_OK)


urlpatterns = [
    path("api/cueceramica-products/", CueCeramicaProductsAPIView.as_view()),
]


# ==============================================================================
# 8. CLI PRE-BUILDER (Downloads Squarespace ONCE, builds FR & EN in parallel)
# ==============================================================================
def prewarm_translations() -> None:
    start_t = time.time()
    print("1/2 Fetching catalog from Squarespace (single download)...", end=" ", flush=True)
    t_dl = time.time()
    items = fetch_squarespace_items()
    print(f"Done in {round(time.time() - t_dl, 2)}s! ({len(items)} products)")

    print("2/2 Building 'FR' and 'EN' product_body catalogs...")
    for lang in ["fr", "en"]:
        t0 = time.time()
        catalog = build_catalog_from_items(items, lang)
        sample_title = json.loads(catalog[0]["translations"])[lang]["title"]
        print(f" -> [{lang.upper()}] Built in {round(time.time() - t0, 2)}s | Sample: {sample_title}")

    print(f"\nAll done in {round(time.time() - start_t, 2)}s! API will now respond in 0.002 seconds.")


if __name__ == "__main__":
    if "--build" in sys.argv or "--warm-cache" in sys.argv:
        prewarm_translations()
    else:
        from django.core.management import execute_from_command_line

        if len(sys.argv) == 1:
            sys.argv.extend(["runserver", "8000"])
        execute_from_command_line(sys.argv)