#!/usr/bin/env python3
"""DropAlert — fetch, notify, claim. Run: python hunter.py --once"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sqlite3
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent
log = logging.getLogger("drop_alert")


# --- config -------------------------------------------------------------------

def _bool(k: str, d: bool = False) -> bool:
    return os.getenv(k, str(d)).lower() in ("1", "true", "yes", "on")


def _str(k: str, d: str = "") -> str:
    """Read env; treat missing or blank (e.g. empty GitHub secret) as default."""
    v = os.getenv(k)
    if v is None or not str(v).strip():
        return d
    return str(v).strip()


def _int(k: str, d: int) -> int:
    v = _str(k, "")
    if not v:
        return d
    return int(v)


def _list(k: str, d: str = "") -> list[str]:
    return [x.strip() for x in _str(k, d).split(",") if x.strip()]


CHECK_HOURS = _int("CHECK_INTERVAL_HOURS", 24)
CLOUD_MODE = _bool("CLOUD_MODE", False)
CLAIM_ENABLED = _bool("CLAIM_ENABLED", True)
MIN_DEAL_PCT = _int("MIN_DEAL_PERCENT", 95)
GP_TYPES = _list("GAMERPOWER_TYPES", "game")
GP_PLATFORMS = _list("GAMERPOWER_PLATFORMS", "steam,gog")
LOGIN_STORES = _list("LOGIN_STORES", "epic,steam,gog")
F2P_FETCH = _bool("F2P_FETCH", True)
F2P_STEAM_PAGES = _int("F2P_STEAM_PAGES", 15)
F2P_STEAM_COUNT = _int("F2P_STEAM_COUNT", 50)
F2P_STEAM_VERIFY = _bool("F2P_STEAM_VERIFY", True)
F2P_STEAM_WORKERS = _int("F2P_STEAM_WORKERS", 8)
STEAM_GIVEAWAY_PAGES = _int("STEAM_GIVEAWAY_PAGES", 3)
STEAM_FETCH_GIVEAWAYS = _bool("STEAM_FETCH_GIVEAWAYS", True)
F2P_EPIC_COUNT = _int("F2P_EPIC_COUNT", 80)

NOTIFY_EMAIL = _bool("NOTIFY_EMAIL", True)
SMTP_HOST = _str("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = _str("SMTP_USER", "")
SMTP_PASS = _str("SMTP_PASSWORD", "")
EMAIL_TO = _str("EMAIL_TO", SMTP_USER)
OWNER_EMAIL = _str("OWNER_EMAIL", EMAIL_TO).lower()
WEB_PORT = _int("WEB_PORT", 5050)
WEB_REFRESH_MIN = _int("WEB_REFRESH_MINUTES", 30)
BROWSER_CHANNEL = _str("BROWSER_CHANNEL", "chrome")
LOGIN_TIMEOUT = _int("LOGIN_TIMEOUT_MINUTES", 15)
EPIC_CDP = _bool("EPIC_USE_CDP", True)
EPIC_CDP_PORT = _int("EPIC_CDP_PORT", 9222)

BROWSER_HEADLESS = _bool("BROWSER_HEADLESS", True)

DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE / "data" / "games.db")))


def _local(name: str) -> Path:
    local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(local) / "DropAlert" / name


PROFILE = {
    "epic": Path(os.getenv("EPIC_CDP_PROFILE", str(_local("chrome-epic")))),
    "steam": Path(os.getenv("STEAM_BROWSER_PROFILE", str(_local("browser-steam")))),
    "gog": Path(os.getenv("GOG_BROWSER_PROFILE", str(_local("browser-gog")))),
}

EPIC_API = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
GP_API = "https://www.gamerpower.com/api/giveaways"
CHEAPSHARK = "https://www.cheapshark.com/api/1.0/deals"
STORE_IDS = {"Steam": "1", "GOG": "7", "Epic Games": "25"}

PLATFORM_MAP = {
    "steam": "Steam", "gog": "GOG", "ubisoft": "Ubisoft", "origin": "EA / Origin",
    "android": "Android", "ios": "iOS", "itch.io": "itch.io",
}


# --- model --------------------------------------------------------------------

@dataclass
class Game:
    id: str
    title: str
    platform: str
    url: str
    source: str
    kind: str = "free"  # free | upcoming | deal | f2p | beta | dlc
    description: str = ""
    price: str = ""
    discount: int = 100
    ends_at: Optional[datetime] = None
    starts_at: Optional[datetime] = None
    claimable: bool = True
    extra: dict = field(default_factory=dict)

    def fp(self) -> str:
        return f"{self.source}:{self.platform}:{self.id}"

    def line(self) -> str:
        bits = [f"• {self.title} ({self.platform})"]
        if self.discount >= MIN_DEAL_PCT:
            bits.append(f"{self.discount}% off")
        if self.price:
            bits.append(f"was {self.price}")
        if self.starts_at and not self.claimable:
            bits.append(f"available {self.starts_at.strftime('%Y-%m-%d %H:%M UTC')}")
        if self.ends_at:
            bits.append(f"ends {self.ends_at.strftime('%Y-%m-%d %H:%M UTC')}")
        bits.append(self.url)
        return " — ".join(bits)


SKIP_TITLE = ("demo", "playtest", "beta", "trial", "open beta", "key giveaway", "play test")


def is_free_listing(g: Game) -> bool:
    """Only true free giveaways, F2P, or upcoming free — no demos, deals, or paid."""
    if g.kind in ("deal", "beta", "dlc"):
        return False
    if g.kind not in ("free", "f2p", "upcoming"):
        return False
    t = g.title.lower()
    if any(w in t for w in SKIP_TITLE):
        return False
    if g.extra.get("gp_type") in ("beta", "demo", "dlc"):
        return False
    return True


def _steam_app(app_id: str) -> dict | None:
    try:
        raw = _get("https://store.steampowered.com/api/appdetails", {"appids": app_id})
        entry = raw.get(app_id, {}) if isinstance(raw, dict) else {}
        if not entry.get("success"):
            return None
        return entry["data"]
    except Exception:
        return None


def _steam_classify(app_id: str) -> tuple[str, str, str] | None:
    """Return (kind, name, img) — f2p=permanent, free=limited-time giveaway (keep forever once claimed)."""
    d = _steam_app(app_id)
    if not d or d.get("type") != "game" or not d.get("is_free"):
        return None
    name = d.get("name", "")
    if any(w in name.lower() for w in SKIP_TITLE):
        return None
    img = d.get("header_image", "") or f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg"
    genres = {g.get("description", "") for g in d.get("genres", [])}
    po = d.get("price_overview") or {}
    initial = po.get("initial") or 0
    final = po.get("final") or 0
    discount = po.get("discount_percent") or 0

    if "Free To Play" in genres:
        return ("f2p", name, img)

    # Limited-time Steam giveaway — free now, was paid; once claimed stays in library
    if initial > 0 and final == 0 and discount >= 100:
        return ("free", name, img)

    return None


def _steam_is_f2p(app_id: str) -> tuple[bool, str, str]:
    r = _steam_classify(app_id)
    if not r or r[0] != "f2p":
        return False, "", ""
    return True, r[1], r[2]


def _steam_is_giveaway(app_id: str) -> tuple[bool, str, str]:
    r = _steam_classify(app_id)
    if not r or r[0] != "free":
        return False, "", ""
    return True, r[1], r[2]


# --- http ---------------------------------------------------------------------

def _get(url: str, params: dict | None = None) -> object:
    full = f"{url}?{urlencode(params)}" if params else url
    req = Request(full, headers={"User-Agent": "FreeGameHunter/2.0"})
    with urlopen(req, timeout=30) as r:
        import json
        return json.loads(r.read().decode())


def _iso(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- database -----------------------------------------------------------------

class DB:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS seen (
                    fp TEXT PRIMARY KEY, title TEXT, platform TEXT, url TEXT DEFAULT '',
                    claimed INT DEFAULT 0, notified_at TEXT)"""
            )
            cols = {r[1] for r in c.execute("PRAGMA table_info(seen)")}
            if "url" not in cols:
                c.execute("ALTER TABLE seen ADD COLUMN url TEXT DEFAULT ''")
            if "claimed_at" not in cols:
                c.execute("ALTER TABLE seen ADD COLUMN claimed_at TEXT DEFAULT ''")
                c.execute(
                    "UPDATE seen SET claimed_at=notified_at WHERE claimed=1 "
                    "AND (claimed_at IS NULL OR claimed_at='')"
                )
            if "image" not in cols:
                c.execute("ALTER TABLE seen ADD COLUMN image TEXT DEFAULT ''")
            c.commit()
            self._backfill_images()
            # migrate old schema
            if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seen_games'").fetchone():
                c.execute(
                    "INSERT OR IGNORE INTO seen(fp,title,platform,claimed,notified_at) "
                    "SELECT fingerprint,title,platform,claimed,notified_at FROM seen_games"
                )
            c.execute("CREATE TABLE IF NOT EXISTS subscribers (email TEXT PRIMARY KEY, joined_at TEXT)")
            c.commit()

    def _backfill_images(self) -> None:
        with sqlite3.connect(self.path) as c:
            rows = c.execute(
                "SELECT fp, title, platform, url FROM seen "
                "WHERE claimed=1 AND (image IS NULL OR image='')"
            ).fetchall()
            if not rows:
                return
            for fp, title, platform, url in rows:
                img = resolve_game_image(fp, title, platform, url or "")
                if img:
                    c.execute("UPDATE seen SET image=? WHERE fp=?", (img, fp))
            c.commit()

    def set_image(self, fp: str, image: str, url: str = "") -> None:
        image = normalize_image_url(image)
        if not image:
            return
        with sqlite3.connect(self.path) as c:
            if url:
                c.execute(
                    "UPDATE seen SET image=?, url=COALESCE(NULLIF(url,''), ?) WHERE fp=?",
                    (image, url, fp),
                )
            else:
                c.execute("UPDATE seen SET image=? WHERE fp=?", (image, fp))
            c.commit()

    def sync_claimed_images(self, games: list[Game]) -> None:
        by_fp = {g.fp(): g for g in games}
        by_key = {(g.title, g.platform): g for g in games}
        with sqlite3.connect(self.path) as c:
            for fp, title, platform, url, img in c.execute(
                "SELECT fp, title, platform, url, image FROM seen WHERE claimed=1"
            ):
                if img:
                    continue
                g = by_fp.get(fp) or by_key.get((title, platform))
                resolved = resolve_game_image(fp, title, platform, url or "", game=g)
                if resolved:
                    nu = g.url if g and g.url else url
                    c.execute(
                        "UPDATE seen SET image=?, url=COALESCE(NULLIF(url,''), ?) WHERE fp=?",
                        (resolved, nu or "", fp),
                    )
            c.commit()

    def claimed(self, g: Game) -> bool:
        with sqlite3.connect(self.path) as c:
            row = c.execute(
                "SELECT 1 FROM seen WHERE claimed=1 AND (fp=? OR (title=? AND platform=?)) LIMIT 1",
                (g.fp(), g.title, g.platform),
            ).fetchone()
        return bool(row)

    def mark_notified(self, games: list[Game]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as c:
            for g in games:
                img = normalize_image_url(g.extra.get("image", ""))
                c.execute(
                    "INSERT INTO seen(fp,title,platform,url,claimed,notified_at,image) "
                    "VALUES(?,?,?,?,0,?,?) "
                    "ON CONFLICT(fp) DO UPDATE SET notified_at=excluded.notified_at, "
                    "url=COALESCE(NULLIF(excluded.url,''), seen.url), "
                    "image=COALESCE(NULLIF(excluded.image,''), seen.image)",
                    (g.fp(), g.title, g.platform, g.url, now, img),
                )
            c.commit()

    def mark_claimed(self, games: list[Game]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as c:
            for g in games:
                img = normalize_image_url(g.extra.get("image", ""))
                c.execute(
                    "INSERT INTO seen(fp,title,platform,url,claimed,notified_at,claimed_at,image) "
                    "VALUES(?,?,?,?,1,?,?,?) "
                    "ON CONFLICT(fp) DO UPDATE SET claimed=1, "
                    "url=COALESCE(NULLIF(excluded.url,''), seen.url), "
                    "title=excluded.title, claimed_at=excluded.claimed_at, "
                    "image=COALESCE(NULLIF(excluded.image,''), seen.image)",
                    (g.fp(), g.title, g.platform, g.url, now, now, img),
                )
                c.execute(
                    "UPDATE seen SET claimed=1, claimed_at=?, "
                    "url=COALESCE(NULLIF(url,''), ?), "
                    "image=COALESCE(NULLIF(image,''), ?) "
                    "WHERE title=? AND platform=?",
                    (now, g.url or "", img, g.title, g.platform),
                )
            c.commit()

    def filter_new(self, games: list[Game]) -> list[Game]:
        with sqlite3.connect(self.path) as c:
            known = {r[0] for r in c.execute("SELECT fp FROM seen")}
        return [g for g in games if g.fp() not in known]

    def to_claim(self, games: list[Game]) -> list[Game]:
        claimed_keys = self.claimed_keys()
        return [
            g for g in games
            if g.claimable and g.fp() not in self.claimed_fps()
            and (g.title, g.platform) not in claimed_keys
        ]

    def claimed_fps(self) -> set[str]:
        with sqlite3.connect(self.path) as c:
            return {r[0] for r in c.execute("SELECT fp FROM seen WHERE claimed=1")}

    def claimed_keys(self) -> set[tuple[str, str]]:
        """Title+platform pairs for claimed games (handles old fp formats)."""
        with sqlite3.connect(self.path) as c:
            return {(r[0], r[1]) for r in c.execute("SELECT title, platform FROM seen WHERE claimed=1")}

    def all_rows(self) -> list[dict]:
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM seen ORDER BY notified_at DESC LIMIT 200")]

    def add_subscriber(self, email: str) -> bool:
        email = email.strip().lower()
        if not email or "@" not in email:
            return False
        with sqlite3.connect(self.path) as c:
            try:
                c.execute(
                    "INSERT INTO subscribers(email,joined_at) VALUES(?,?)",
                    (email, datetime.now(timezone.utc).isoformat()),
                )
                c.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def claimed_list(self) -> list[dict]:
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                "SELECT title, platform, fp, url, image, "
                "COALESCE(NULLIF(claimed_at,''), notified_at) AS claimed_at "
                "FROM seen WHERE claimed=1 "
                "ORDER BY claimed_at DESC"
            )]
        best: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["title"], row["platform"])
            if key not in best or (row.get("claimed_at") or "") > (best[key].get("claimed_at") or ""):
                best[key] = row
        return sorted(best.values(), key=lambda r: r.get("claimed_at") or "", reverse=True)

    def subscribers(self) -> list[str]:
        with sqlite3.connect(self.path) as c:
            return [r[0] for r in c.execute("SELECT email FROM subscribers")]


def game_dict(g: Game, *, claimed: bool = False) -> dict:
    return {
        "fp": g.fp(),
        "title": g.title, "platform": g.platform, "kind": g.kind, "url": g.url,
        "price": g.price, "discount": g.discount, "claimed": claimed,
        "claimable": g.claimable and g.kind != "upcoming",
        "image": normalize_image_url(g.extra.get("image", "")),
        "ends": g.ends_at.strftime("%Y-%m-%d %H:%M") if g.ends_at else "",
        "starts": g.starts_at.strftime("%Y-%m-%d %H:%M") if g.starts_at else "",
    }


# --- fetchers -----------------------------------------------------------------

def normalize_image_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def steam_header_image(app_id: str) -> str:
    return (
        f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
        f"{app_id}/header.jpg"
    )


def _epic_key_image(item: dict) -> str:
    """Pick best cover from Epic catalog keyImages."""
    preferred = ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail", "OfferImageTall")
    images = item.get("keyImages") or []
    by_type = {ki.get("type"): ki.get("url", "") for ki in images if ki.get("url")}
    for t in preferred:
        if by_type.get(t):
            return normalize_image_url(by_type[t])
    if images:
        return normalize_image_url(images[0].get("url", ""))
    return ""


def fetch_epic() -> list[Game]:
    data = _get(EPIC_API)
    now = datetime.now(timezone.utc)
    out: list[Game] = []
    for item in data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", []):
        promos = item.get("promotions") or {}
        active = upcoming = None
        for grp in promos.get("promotionalOffers") or []:
            for o in grp.get("promotionalOffers", []):
                s, e = _iso(o.get("startDate")), _iso(o.get("endDate"))
                if s and e and s <= now <= e:
                    active = o
                    break
        for grp in promos.get("upcomingPromotionalOffers") or []:
            for o in grp.get("promotionalOffers", []):
                s = _iso(o.get("startDate"))
                if s and s > now:
                    upcoming = o
                    break
        if not active and not upcoming:
            continue
        promo = active or upcoming
        slug = item.get("productSlug") or item.get("urlSlug")
        for m in item.get("offerMappings") or item.get("catalogNs", {}).get("mappings") or []:
            slug = m.get("pageSlug") or slug
        price = item.get("price", {}).get("totalPrice", {})
        orig = price.get("fmtPrice", {}).get("originalPrice", "")
        disc = price.get("discount", 0) // 100 if price.get("discount") else 100
        out.append(Game(
            id=item.get("id", slug or "x"),
            title=item.get("title", "?"),
            platform="Epic Games",
            url=f"https://store.epicgames.com/en-US/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games",
            source="epic",
            kind="free" if active else "upcoming",
            description=(item.get("description") or "")[:300],
            price=orig,
            discount=disc or 100,
            starts_at=_iso(promo.get("startDate")),
            ends_at=_iso(promo.get("endDate")),
            claimable=active is not None,
            extra={"image": _epic_key_image(item)},
        ))
    log.info("Epic: %d games (%d claimable)", len(out), sum(1 for g in out if g.claimable))
    return out


def fetch_gamerpower() -> list[Game]:
    seen: set[int] = set()
    raw: list[dict] = []
    for t in GP_TYPES:
        try:
            batch = _get(GP_API, {"type": t})
        except Exception:
            continue
        if not isinstance(batch, list):
            continue
        for item in batch:
            iid = item.get("id")
            if iid in seen:
                continue
            seen.add(iid)
            if item.get("status", "").lower() != "active":
                continue
            plats = item.get("platforms", "").lower()
            if GP_PLATFORMS and not any(p in plats for p in GP_PLATFORMS):
                continue
            raw.append(item)

    out: list[Game] = []
    for item in raw:
        plats = item.get("platforms", "")
        platform = "PC"
        for k, label in PLATFORM_MAP.items():
            if k in plats.lower():
                platform = label
                break
        ends = None
        if item.get("end_date") and item["end_date"] != "N/A":
            try:
                ends = datetime.strptime(item["end_date"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        typ = (item.get("type") or "Game").lower()
        if typ != "game":
            continue
        title = html.unescape(item.get("title", "?"))
        if any(w in title.lower() for w in SKIP_TITLE):
            continue
        kind = "f2p" if "free to play" in title.lower() or "f2p" in title.lower() else "free"
        img = normalize_image_url(item.get("thumbnail") or item.get("image") or "")
        out.append(Game(
            id=str(item.get("id")),
            title=title,
            platform=platform,
            url=item.get("open_giveaway") or item.get("gamerpower_url", ""),
            source="gamerpower",
            kind=kind,
            description=(item.get("description") or "")[:300],
            price=item.get("worth", ""),
            discount=100,
            ends_at=ends,
            claimable=True,
            extra={"gp_type": typ, "image": img},
        ))
    log.info("GamerPower: %d active", len(out))
    return out


def fetch_deals() -> list[Game]:
    out: list[Game] = []
    for platform, sid in STORE_IDS.items():
        try:
            deals = _get(CHEAPSHARK, {"storeID": sid, "pageNumber": 0, "pageSize": 40})
        except Exception:
            continue
        if not isinstance(deals, list):
            continue
        for d in deals:
            savings = d.get("savings", "0")
            try:
                pct = int(float(savings))
            except ValueError:
                continue
            if pct < MIN_DEAL_PCT:
                continue
            title = d.get("title", "?")
            deal_id = d.get("dealID", "")
            game_id = d.get("gameID", deal_id)
            out.append(Game(
                id=f"cs-{game_id}-{deal_id}",
                title=title,
                platform=platform,
                url=f"https://www.cheapshark.com/redirect/{deal_id}",
                source="cheapshark",
                kind="deal",
                price=f"${d.get('normalPrice', '?')}",
                discount=pct,
                claimable=False,
                extra={"sale_price": d.get("salePrice")},
            ))
    log.info("Deals >=%d%%: %d", MIN_DEAL_PCT, len(out))
    return out


def fetch_steam_f2p() -> list[Game]:
    """Permanent Free-to-Play titles only (verified via Steam API)."""
    if not F2P_FETCH:
        return []
    candidates = _steam_search_pages(F2P_STEAM_PAGES, f2p_only=True)
    out = _steam_verify_batch(candidates, want_kind="f2p")
    log.info("Steam F2P: %d verified / %d candidates", len(out), len(candidates))
    return out


def _steam_search_pages(pages: int, *, f2p_only: bool) -> list[tuple[str, str]]:
    """Collect app ids from Steam search (f2p_only adds category 998)."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        params = {
            "json": "1", "filter": "free", "page": str(page),
            "count": str(F2P_STEAM_COUNT), "cc": "us", "l": "english",
        }
        if f2p_only:
            params["category1"] = "998"
        try:
            data = _get("https://store.steampowered.com/search/results/", params)
        except Exception:
            log.warning("Steam search page %d failed (%s)", page, "f2p" if f2p_only else "giveaway")
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            m = re.search(r"/apps/(\d+)/", item.get("logo", ""))
            if not m:
                continue
            app_id = m.group(1)
            if app_id in seen:
                continue
            title = html.unescape((item.get("name") or "").strip())
            if not title or any(w in title.lower() for w in SKIP_TITLE):
                continue
            seen.add(app_id)
            candidates.append((app_id, title))
    return candidates


def _steam_verify_batch(
    candidates: list[tuple[str, str]],
    *,
    want_kind: str,
) -> list[Game]:
    if not candidates:
        return []

    check = _steam_is_f2p if want_kind == "f2p" else _steam_is_giveaway

    def _one(pair: tuple[str, str]) -> Game | None:
        app_id, _ = pair
        if F2P_STEAM_VERIFY:
            ok, name, img = check(app_id)
            if not ok:
                return None
            title = name
        else:
            title = pair[1]
            img = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg"
        src = "steam_f2p" if want_kind == "f2p" else "steam_giveaway"
        price = "Free to Play" if want_kind == "f2p" else "Free giveaway"
        return Game(
            id=f"{want_kind}-{app_id}",
            title=title,
            platform="Steam",
            url=f"https://store.steampowered.com/app/{app_id}/",
            source=src,
            kind=want_kind,
            price=price,
            discount=100,
            claimable=True,
            extra={"image": img},
        )

    out: list[Game] = []
    workers = min(F2P_STEAM_WORKERS, len(candidates))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed(pool.submit(_one, c) for c in candidates):
            g = fut.result()
            if g:
                out.append(g)
    return out


def fetch_steam_giveaways() -> list[Game]:
    """Limited-time Steam giveaways — free to claim now, permanent once added."""
    if not F2P_FETCH or not STEAM_FETCH_GIVEAWAYS:
        return []
    candidates = _steam_search_pages(STEAM_GIVEAWAY_PAGES, f2p_only=False)
    out = _steam_verify_batch(candidates, want_kind="free")
    log.info("Steam giveaways: %d verified / %d candidates", len(out), len(candidates))
    return out


DEFAULT_EPIC_F2P = [
    ("Fortnite", "fortnite"),
    ("Fall Guys", "fall-guys"),
    ("Rocket League", "rocket-league"),
    ("Destiny 2", "destiny-2"),
    ("Genshin Impact", "genshin-impact"),
    ("Honkai Star Rail", "honkai-star-rail"),
    ("Warframe", "warframe"),
    ("Path of Exile", "path-of-exile"),
    ("The Sims 4", "the-sims-4"),
]


def _epic_image(slug: str) -> str:
    """Fetch Epic store cover image for a product slug."""
    try:
        data = _get(
            f"https://store-site-backend-static-ipv4.ak.epicgames.com/api/en-US/product/{slug}/home"
        )
        page = (data.get("data") or data).get("Page") or (data.get("data") or data)
        for src in (
            (page.get("hero") or {}).get("backgroundImageUrl"),
            (page.get("hero") or {}).get("backgroundImage"),
        ):
            if isinstance(src, str) and src.startswith("http"):
                return normalize_image_url(src)
            if isinstance(src, dict) and src.get("url"):
                return normalize_image_url(src["url"])
        for sec in page.get("catalog", {}).get("catalogOffers", []) or []:
            for ki in sec.get("keyImages", []) or []:
                if ki.get("type") in ("Thumbnail", "OfferImageWide", "DieselStoreFrontWide") and ki.get("url"):
                    return normalize_image_url(ki["url"])
        for sec in page.get("catalog", {}).get("catalogOffers", []) or []:
            for ki in sec.get("keyImages", []) or []:
                if ki.get("url"):
                    return normalize_image_url(ki["url"])
    except Exception:
        pass
    return "https://cdn1.epicgames.com/epic/static/img/logo-epic.png"


def _gamerpower_image(giveaway_id: str) -> str:
    try:
        item = _get("https://www.gamerpower.com/api/giveaway", {"id": giveaway_id})
        if isinstance(item, dict):
            return normalize_image_url(item.get("thumbnail") or item.get("image") or "")
    except Exception:
        pass
    return ""


def _epic_image_by_id(offer_id: str) -> str:
    try:
        data = _get(EPIC_API)
        for item in data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", []):
            if item.get("id") == offer_id:
                return _epic_key_image(item)
    except Exception:
        pass
    return ""


def resolve_game_image(
    fp: str = "",
    title: str = "",
    platform: str = "",
    url: str = "",
    stored: str = "",
    game: Game | None = None,
) -> str:
    if stored:
        return normalize_image_url(stored)
    if game:
        img = normalize_image_url(game.extra.get("image", ""))
        if img:
            return img
    url = normalize_image_url(url)
    if url:
        m = re.search(r"steampowered\.com/app/(\d+)", url)
        if m:
            return steam_header_image(m.group(1))
        if "store.epicgames.com" in url and "/p/" in url:
            slug = url.split("/p/")[-1].split("?")[0].strip("/")
            if slug:
                return normalize_image_url(_epic_image(slug))
    fp = fp or ""
    m = re.search(r"f2p-(\d+)$", fp)
    if m:
        return steam_header_image(m.group(1))
    m = re.search(r"^gamerpower:[^:]+:(\d+)$", fp)
    if m:
        return _gamerpower_image(m.group(1))
    m = re.search(r":Epic Games:([a-f0-9]{32})$", fp)
    if m:
        img = _epic_image_by_id(m.group(1))
        if img:
            return img
    m = re.search(r":f2p-(.+)$", fp)
    if m and "Epic" in (platform or ""):
        return normalize_image_url(_epic_image(m.group(1)))
    return ""


def fetch_epic_f2p() -> list[Game]:
    """Epic Free-to-Play titles — uses curated list + optional CDP browse."""
    if not F2P_FETCH or CLOUD_MODE:
        return []

    custom = _list("F2P_EPIC_SLUGS", "")
    slugs: list[tuple[str, str]] = [(s.replace("-", " ").title(), s) for s in custom] if custom else list(DEFAULT_EPIC_F2P)

    if EPIC_CDP and not custom:
        try:
            from claim import scrape_epic_f2p_slugs
            extra = scrape_epic_f2p_slugs(F2P_EPIC_COUNT)
            known = {s for _, s in slugs}
            for title, slug in extra:
                if slug not in known and not any(w in title.lower() for w in SKIP_TITLE):
                    slugs.append((title, slug))
                    known.add(slug)
        except Exception:
            log.debug("Epic F2P CDP scrape skipped", exc_info=True)

    out = []
    for title, slug in slugs[:F2P_EPIC_COUNT]:
        if any(w in title.lower() for w in SKIP_TITLE):
            continue
        out.append(Game(
            id=f"f2p-{slug}",
            title=title,
            platform="Epic Games",
            url=f"https://store.epicgames.com/en-US/p/{slug}",
            source="epic_f2p",
            kind="f2p",
            price="Free to Play",
            discount=100,
            claimable=True,
            extra={"image": _epic_image(slug)},
        ))
    log.info("Epic F2P: %d", len(out))
    return out


def fetch_all() -> list[Game]:
    seen: set[str] = set()
    all_g: list[Game] = []
    for fn in (fetch_epic, fetch_gamerpower, fetch_steam_f2p, fetch_steam_giveaways, fetch_epic_f2p):
        try:
            for g in fn():
                if g.fp() not in seen and is_free_listing(g):
                    seen.add(g.fp())
                    all_g.append(g)
        except Exception:
            log.exception("%s failed", fn.__name__)
    return all_g


# --- notify -------------------------------------------------------------------

def email_ok() -> bool:
    return NOTIFY_EMAIL and bool(SMTP_USER and SMTP_PASS and EMAIL_TO)


def send_email(subject: str, body: str) -> bool:
    return send_email_to(EMAIL_TO, subject, body)


def send_email_to(to_addr: str, subject: str, body: str) -> bool:
    if not NOTIFY_EMAIL or not SMTP_USER or not SMTP_PASS or not to_addr:
        return False
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [to_addr], msg.as_string())
    return True


def subscriber_digest(games: list[Game], header: str = "DropAlert") -> tuple[str, str]:
    free = [g for g in games if is_free_listing(g) and g.claimable and g.kind == "free"]
    f2p = [g for g in games if is_free_listing(g) and g.kind == "f2p"]
    upcoming = [g for g in games if is_free_listing(g) and g.kind == "upcoming"]
    parts = [header]
    if free:
        parts.append("\nFREE NOW\n" + "\n".join(g.line() for g in free))
    if f2p:
        parts.append("\nFREE TO PLAY\n" + "\n".join(g.line() for g in f2p))
    if upcoming:
        parts.append("\nUPCOMING\n" + "\n".join(g.line() for g in upcoming))
    body = "\n".join(parts)
    return f"{header} — {len(free)} free, {len(f2p)} F2P", body


def notify_subscribers(games: list[Game], new_games: list[Game]) -> None:
    subs = [e for e in DB().subscribers() if e.lower() != OWNER_EMAIL]
    if not subs or not new_games:
        return
    subj = f"New free games ({len(new_games)})"
    body = "New on DropAlert:\n\n" + "\n".join(g.line() for g in new_games)
    for email in subs:
        send_email_to(email, subj, body)


def build_digest(games: list[Game], claimed: list[Game], failed: list[Game]) -> tuple[str, str]:
    games = [g for g in games if is_free_listing(g)]
    free = [g for g in games if g.claimable and g.kind == "free"]
    f2p = [g for g in games if g.kind == "f2p"]
    upcoming = [g for g in games if g.kind == "upcoming"]
    gog = [g for g in free if g.platform == "GOG"]

    sections = []
    if free:
        sections.append("FREE NOW (limited-time giveaways)\n" + "\n".join(g.line() for g in free))
    if f2p:
        sections.append("FREE TO PLAY (add to library)\n" + "\n".join(g.line() for g in f2p))
    if upcoming:
        sections.append("UPCOMING FREE\n" + "\n".join(g.line() for g in upcoming))
    if not gog:
        sections.append("GOG: no active free giveaways on GamerPower right now")
    if claimed:
        sections.append("CLAIMED TO YOUR ACCOUNT\n" + "\n".join(g.line() for g in claimed))
    if failed:
        sections.append("CLAIM FAILED (retry next run)\n" + "\n".join(g.line() for g in failed))

    body = "\n\n".join(sections) or "No new games this run."
    subject = f"DropAlert — {len(free)} free, {len(f2p)} F2P, {len(upcoming)} upcoming"
    return subject, body


# --- run ----------------------------------------------------------------------

def run(*, dry_run: bool = False, login_only: bool = False, force: bool = False, stores: list[str] | None = None) -> list[Game]:
    log.info("Run started %s (cloud=%s, headless=%s)", datetime.now(timezone.utc).isoformat(), CLOUD_MODE, BROWSER_HEADLESS)

    if dry_run:
        games = fetch_all()
        log.info("Dry run: %d games", len(games))
        return games

    from claim import ensure_logins, run_claims, stores_for_games

    store_list = stores or LOGIN_STORES
    verified: set[str] = set()

    if login_only:
        ensure_logins(store_list, interactive=True)
        return []

    if CLAIM_ENABLED and not CLOUD_MODE:
        verified = ensure_logins(stores_for_games(to_claim) if to_claim else store_list, interactive=False)

    games = fetch_all()
    db = DB()
    notify_set = games if force else db.filter_new(games)
    to_claim = db.to_claim(games) if CLAIM_ENABLED and not CLOUD_MODE else []

    claimed: list[Game] = []
    failed: list[Game] = []
    if to_claim and verified:
        needed = set(stores_for_games(to_claim))
        claimed, failed = run_claims(to_claim, verified & needed)
        if claimed:
            db.mark_claimed(claimed)

    if notify_set or claimed or failed:
        subject, body = build_digest(games, claimed, failed)
        if send_email(subject, body):
            log.info("Email sent")
    elif not notify_set:
        log.info("No new games to notify")

    if notify_set:
        db.mark_notified(notify_set)
        notify_subscribers(games, notify_set)

    log.info("Done — %d games, %d claimed, %d failed", len(games), len(claimed), len(failed))
    return games


def claim_specific(
    games: list[Game],
    fps: list[str],
    *,
    stores: list[str] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    email: bool = True,
) -> tuple[list[Game], list[Game]]:
    """Claim only games matching the given fingerprints."""
    from claim import ensure_logins, run_claims, stores_for_games

    if not CLAIM_ENABLED or CLOUD_MODE:
        return [], []

    by_fp = {g.fp(): g for g in games}
    db = DB()
    to_claim = [
        by_fp[fp] for fp in fps
        if fp in by_fp and by_fp[fp].claimable and not db.claimed(by_fp[fp])
    ]
    if not to_claim:
        log.warning("Nothing to claim — %d requested, all already claimed or not claimable", len(fps))
        return [], []

    store_list = stores or stores_for_games(to_claim)
    verified = ensure_logins(store_list, interactive=False)
    needed = set(stores_for_games(to_claim))
    active = verified & needed
    if not active:
        missing = sorted(needed - verified)
        hint = ",".join(missing)
        msg = f"Store login required ({', '.join(missing)}) — run: python hunter.py --login-only --stores {hint}"
        log.warning(msg)
        if progress:
            progress(msg, 0, len(to_claim))
        return [], to_claim

    runnable = [g for g in to_claim if _game_store(g) in active]
    skipped = [g for g in to_claim if g not in runnable]
    if skipped:
        log.warning("Skipping %d game(s) — store login missing", len(skipped))

    claimed, failed = run_claims(runnable, active, progress=progress)
    failed.extend(skipped)
    if claimed:
        db.mark_claimed(claimed)
    if email and (claimed or failed):
        subject, body = build_digest(games, claimed, failed)
        if send_email(subject, body):
            log.info("Claim email sent")
    log.info("Claimed %d, failed %d", len(claimed), len(failed))
    return claimed, failed


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    p = argparse.ArgumentParser(description="DropAlert")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--login-only", action="store_true")
    p.add_argument("--force-notify", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--stores", default="", help="epic,steam,gog")
    args = p.parse_args()
    stores = [s.strip() for s in args.stores.split(",") if s.strip()] or None

    if args.list:
        for g in fetch_all():
            print(g.line())
        return 0

    if not args.dry_run and not email_ok():
        missing = [
            name
            for name, ok in (
                ("SMTP_USER", bool(SMTP_USER)),
                ("SMTP_PASSWORD", bool(SMTP_PASS)),
                ("EMAIL_TO", bool(EMAIL_TO)),
            )
            if not ok
        ]
        if CLOUD_MODE:
            # Scheduled CI should still fetch; email waits on repo secrets.
            log.warning(
                "SMTP incomplete (%s) — fetch continues without email. "
                "Add repo Secrets: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO",
                ", ".join(missing) or "NOTIFY_EMAIL=false",
            )
        else:
            log.error(
                "Set NOTIFY_EMAIL=true and SMTP credentials in .env (missing: %s)",
                ", ".join(missing) or "unknown",
            )
            return 1

    if args.once or args.dry_run or args.login_only:
        run(dry_run=args.dry_run, login_only=args.login_only, force=args.force_notify, stores=stores)
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    sched = BlockingScheduler()
    sched.add_job(lambda: run(stores=stores), IntervalTrigger(hours=CHECK_HOURS), id="drop_alert")
    log.info("Scheduler every %dh — Ctrl+C to stop", CHECK_HOURS)
    sched.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
