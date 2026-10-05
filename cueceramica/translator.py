from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
from typing import Any, Dict, List
import urllib.parse
import urllib.request

from .utils import extract_clean_description

TRANSLATION_STORE_FILE = "cueceramica_translations_v7.json"
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