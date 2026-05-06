import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re

class SoftwareResolver:
    def __init__(self, search_fn=None):
        """
        search_fn(query) -> list of URLs
        You can plug in SerpAPI / Brave / custom search here
        """
        self.search_fn = search_fn or self.simple_search

    # --- 1. SEARCH ---
    def simple_search(self, query):
        # naive fallback (replace with real search API)
        return [
            f"https://www.google.com/search?q={query}"
        ]

    # --- 2. RANK ---
    def score_url(self, url, app_name):
        score = 0
        url_l = url.lower()

        if "download" in url_l:
            score += 2
        if "linux" in url_l:
            score += 2
        if app_name.lower() in url_l:
            score += 2
        if "github.com" in url_l:
            score += 2

        # crude "official domain" heuristic
        if re.search(rf"{app_name.replace(' ', '')}\.", url_l):
            score += 3

        return score

    # --- 3. FETCH PAGE ---
    def fetch(self, url):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return r.text
        except:
            return None

    # --- 4. EXTRACT DOWNLOAD LINKS ---
    def extract_links(self, html, base_url):
        soup = BeautifulSoup(html, "html.parser")
        links = []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            full_url = urljoin(base_url, href)

            if any(ext in href for ext in [".AppImage", ".tar.gz", ".deb"]):
                links.append(full_url)

            # heuristic: download buttons
            text = a.get_text().lower()
            if "download" in text and "linux" in text:
                links.append(full_url)

        return list(set(links))

    # --- 5. VALIDATE ---
    def validate(self, url):
        try:
            r = requests.head(url, timeout=5, allow_redirects=True)
            return r.status_code == 200
        except:
            return False

    # --- MAIN PIPELINE ---
    def resolve(self, app_name):
        query = f"{app_name} linux download"
        candidates = self.search_fn(query)

        ranked = sorted(
            candidates,
            key=lambda u: self.score_url(u, app_name),
            reverse=True
        )

        results = []

        for url in ranked[:5]:  # limit scope
            html = self.fetch(url)
            if not html:
                continue

            links = self.extract_links(html, url)

            for link in links:
                if self.validate(link):
                    results.append(link)

        return results