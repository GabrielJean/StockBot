import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.management import BaseCommand, CommandError

from core.models import AppleCatalogItem


class Command(BaseCommand):
    help = "Imports approved EveryMac iPhone specification pages into the Apple catalogue."

    def add_arguments(self, parser):
        parser.add_argument("source_urls", nargs="+", help="EveryMac index or specification page URLs covered by permission.")

    def handle(self, *args, **options):
        pages = []
        for source_url in options["source_urls"]:
            response = requests.get(source_url, timeout=30, headers={"User-Agent": settings.STOCKBOT_USER_AGENT})
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            links = [urljoin(source_url, link["href"]) for link in soup.select('a[href*="-specs.html"]')]
            pages.extend(links or [source_url])
        imported = 0
        for page_url in dict.fromkeys(pages):
            response = requests.get(page_url, timeout=30, headers={"User-Agent": settings.STOCKBOT_USER_AGENT})
            if response.status_code != 200:
                self.stderr.write(f"Skipped {page_url}: HTTP {response.status_code}")
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            title = (soup.select_one("h3") or soup.title)
            title_text = title.get_text(" ", strip=True) if title else "Apple configuration"
            text = soup.get_text(" ", strip=True)
            order_section = text.split("Apple Order No:", 1)[-1].split("Apple Model No:", 1)[0]
            order_numbers = list(dict.fromkeys(re.findall(r"\b[A-Z0-9]{4,}VC/A\b", order_section)))
            colors = list(dict.fromkeys(re.findall(r"(?:in |--)\s*([A-Z][A-Za-z ]+?)(?:,| the order| with)", order_section)))
            storage = list(dict.fromkeys(re.findall(r"\b(\d+\s*(?:GB|TB))\b", order_section)))
            configurations = [f"{color.strip()} · {capacity}" for color in colors for capacity in storage]
            for index, order_number in enumerate(order_numbers):
                configuration = configurations[index] if index < len(configurations) else ""
                AppleCatalogItem.objects.update_or_create(order_number=order_number, defaults={"title": title_text[:255], "configuration": configuration, "source_url": page_url, "active": True})
                imported += 1
        if not imported:
            raise CommandError("No Canada Apple Order Nos. were found in the approved pages.")
        self.stdout.write(self.style.SUCCESS(f"Imported {imported} Apple configurations."))
