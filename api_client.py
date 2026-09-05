"""
Wrappers around the external APIs used by "Get Info" and "Blast".
Both are simple GET requests expected to return JSON. Adjust as needed
for your real API (headers, auth token, POST body, etc).
"""

import requests

CODE_INFO_API_URL = None  # set from bot.py at startup
BLAST_API_URL = None  # set from bot.py at startup


def configure(code_info_url: str, blast_url: str):
    global CODE_INFO_API_URL, BLAST_API_URL
    CODE_INFO_API_URL = code_info_url
    BLAST_API_URL = blast_url


def fetch_number_info(number: str) -> dict:
    if not CODE_INFO_API_URL:
        raise RuntimeError("CODE_INFO_API_URL is not configured.")
    url = CODE_INFO_API_URL.format(number=number)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def blast_hit(number: str) -> dict:
    if not BLAST_API_URL:
        raise RuntimeError("BLAST_API_URL is not configured.")
    url = BLAST_API_URL.format(number=number)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()
