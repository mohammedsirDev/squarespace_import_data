import json
import time
from django.core.management.base import BaseCommand
from cueceramica.services import build_catalog_from_items, fetch_squarespace_items


class Command(BaseCommand):
    help = "Downloads Squarespace catalog once and pre-builds FR and EN product_body caches."

    def handle(self, *args, **options):
        start_t = time.time()
        self.stdout.write("1/2 Fetching catalog from Squarespace...")
        items = fetch_squarespace_items()
        self.stdout.write(f"Fetched {len(items)} products.")

        for lang in ["fr", "en"]:
            t0 = time.time()
            catalog = build_catalog_from_items(items, lang)
            sample_title = json.loads(catalog[0]["translations"])[lang]["title"]
            self.stdout.write(
                self.style.SUCCESS(
                    f" -> [{lang.upper()}] Built in {round(time.time() - t0, 2)}s | Sample: {sample_title}"
                )
            )

        self.stdout.write(self.style.SUCCESS(f"All done in {round(time.time() - start_t, 2)}s!"))