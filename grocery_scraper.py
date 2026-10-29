#!/usr/bin/env python3
"""
BD Grocery Price Scraper v2  (selector / inspect lagbe na)
----------------------------------------------------------
Shudhu category page er URL dao. Script nijei product name + price khuje ber kore
CSV / JSON e export korbe.

Install:
    pip install requests beautifulsoup4 lxml
    # JS diye load hoy emon site (Chaldal etc.) er jonno:
    pip install playwright && playwright install chromium

Use:
    python grocery_scraper.py --url "https://site.com/category/rice"
    python grocery_scraper.py --url URL1 URL2 URL3 --pages 3 --out prices
    python grocery_scraper.py --url URL --render playwright      # JS site
    python grocery_scraper.py --url "https://site.com/rice?page={page}" --pages 5

Detection order (automatic):
    1. JSON-LD (schema.org Product)
    2. Embedded JSON (__NEXT_DATA__ / __NUXT__ etc.)
    3. Visual heuristic: repeated blocks jeikhane ৳/Tk price ache
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, NavigableString

VERSION = "v21-no-product-diagnose-2026-10-25"  # ei nam run er shuru te print hoy: kon file chalachho bujhte
USER_AGENT = "Mozilla/5.0 (compatible; GroceryPriceBot/2.0; personal price comparison)"
DELAY_RANGE = (1.5, 3.5)
TIMEOUT = 20
RETRIES = 3
DEBUG = False

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
NUM = r"\d[\d,]*(?:\.\d+)?"
PRICE_RE = re.compile(
    rf"(?:৳|Tk\.?|BDT|Taka|টাকা)\s*{NUM}|{NUM}\s*(?:৳|Tk\b|BDT|Taka|টাকা)", re.I
)
OLD_PRICE_HINT = re.compile(r"old|regular|original|strike|line-through|mrp|was|cut", re.I)
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "svg"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def norm(text):
    return (text or "").translate(BN_DIGITS)


def to_number(text):
    m = re.search(NUM, norm(str(text)).replace(",", "") if text is not None else "")
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def clean_text(s):
    return re.sub(r"\s+", " ", s or "").strip()


class Robots:
    """robots.txt parser: wildcard (*) ar ($) support kore, longest-match rule (Google er moto)."""

    def __init__(self, text):
        self.rules = []  # (allow, compiled_regex, specificity)
        agents, in_rules = [], False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = [x.strip() for x in line.split(":", 1)]
            key = key.lower()
            if key == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                agents.append(val.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if any(a == "*" or a in USER_AGENT.lower() for a in agents) and val:
                    pat = re.escape(val).replace(r"\*", ".*")
                    if pat.endswith(r"\$"):
                        pat = pat[:-2] + "$"
                    self.rules.append((key == "allow", re.compile(pat), len(val)))

    def can_fetch(self, url):
        p = urlparse(url)
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        best = None
        for allow, rx, spec in self.rules:
            if rx.match(path) and (best is None or spec > best[1] or (spec == best[1] and allow)):
                best = (allow, spec)
        return True if best is None else best[0]


_robots_cache = {}
ROBOTS_FILE = None  # --robots-file: browser theke save kora robots.txt


def _fetch_robots_text(root, fetcher):
    """(status, text) ba None. Prothome requests (retry shoho), na hole browser diye."""
    url = root + "/robots.txt"
    for attempt in range(1, 4):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            return r.status_code, r.text
        except requests.RequestException as e:
            print(f"  ! robots.txt attempt {attempt}/3: {type(e).__name__}")
            time.sleep(2 * attempt)
    ctx = getattr(fetcher, "_ctx", None)  # Playwright browser context
    if ctx is not None:
        try:
            print("  robots.txt browser diye try kora hocche...")
            resp = ctx.request.get(url, timeout=45000)
            return resp.status, resp.text()
        except Exception as e:
            print(f"  ! browser diye-o hoyni: {type(e).__name__}")
    return None


def allowed_by_robots(url, fetcher=None):
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots_cache:
        rf = ROBOTS_FILE
        if rf and not os.path.exists(rf) and os.path.exists(rf + ".txt"):
            rf += ".txt"  # Windows e Notepad .txt.txt banay
        if rf and not os.path.exists(rf):
            print(f"  ! robots file pawa jayni: {rf}  (ei folder e ache: {os.getcwd()})")
            print("    Tai site theke robots.txt fetch kora hocche.")
            rf = None
        if rf:
            with open(rf, encoding="utf-8", errors="replace") as f:
                _robots_cache[root] = Robots(f.read())
            print(f"  robots.txt local file theke nea hoyeche: {rf}")
        else:
            res = _fetch_robots_text(root, fetcher)
            if res is None or res[0] >= 500:
                print(f"  ! {root}/robots.txt pawa jayni -- rules na jene scrape korbo na.")
                print(f"    Browser e {root}/robots.txt khule content ta robots_{p.netloc}.txt e save koro,")
                print(f"    tarpor --robots-file robots_{p.netloc}.txt dao.")
                _robots_cache[root] = Robots("User-agent: *\nDisallow: /")
            elif res[0] == 200:
                _robots_cache[root] = Robots(res[1])
            else:
                _robots_cache[root] = Robots("")  # 4xx = kono restriction nai
    return _robots_cache[root].can_fetch(url)


def page_url(template, page):
    if "{page}" in template:
        return template.format(page=page)
    if page == 1:
        return template
    p = urlparse(template)
    q = dict(parse_qsl(p.query))
    q["page"] = str(page)
    return urlunparse(p._replace(query=urlencode(q)))


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
class RequestsFetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en,bn;q=0.8"})

    def get(self, url):
        for attempt in range(1, RETRIES + 1):
            try:
                r = self.s.get(url, timeout=TIMEOUT)
                if r.status_code == 200:
                    return r.text
                if r.status_code in (403, 404):
                    print(f"  ! HTTP {r.status_code}")
                    return None
            except requests.RequestException as e:
                print(f"  ! attempt {attempt}: {e}")
            time.sleep(2 * attempt)
        return None

    def close(self):
        pass


# site er nijer location-set API (browser e location select korle page eta call kore). Notun site e eta add kora jay.
LOCATION_APIS = {
    "chaldal.com": "/yolk/api-v4/Address/SetCustomerDefaultsFromCoordinates?latitude={lat}&longitude={lng}",
}

NOISE_HEADERS = {"user-agent", "accept", "accept-encoding", "accept-language", "cookie", "referer", "origin",
                 "connection", "host", "content-length", "authorization", "pragma", "cache-control", "priority",
                 "upgrade-insecure-requests", "dnt"}
PLACEHOLDER_RE = re.compile(r"select|choose|pick|^[-–\s.]*$|বাছাই|নির্বাচন|সিলেক্ট|--", re.I)
CONFIRM_RE = re.compile(
    r"\b(?:confirm|save|continue|submit|apply|proceed|start shopping|shop now|set location|done)\b|নিশ্চিত|সংরক্ষণ|চালিয়ে|ঠিক আছে|শুরু",
    re.I)

# open shadow root gulor bhitorer element-o khuje (Flutter glass-pane shadow DOM e thake). Function body r bhitor boshe.
JS_DEEP = """
  const deep = (sel, root = document) => {
    const out = [...root.querySelectorAll(sel)];
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) out.push(...deep(sel, el.shadowRoot));
    return out;
  };
"""

JS_FIND = """
const findBest = () => {
  let best = null, bh = 0;
  for (const el of document.querySelectorAll('div,main,section,ul')) {
    const s = getComputedStyle(el);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.clientHeight > 200 &&
        el.scrollHeight > el.clientHeight + 50 && el.scrollHeight > bh) { best = el; bh = el.scrollHeight; }
  }
  return best;
};
"""
# window ar sobcheye boro inner scroll-container, dutoi dhire dhire nambay (virtualized/lazy list er jonno)
SCROLL_JS = "() => {" + JS_FIND + """
  const best = findBest();
  window.scrollBy(0, window.innerHeight * 0.8);
  if (best) best.scrollTop += best.clientHeight * 0.8;
}"""
STATE_JS = "() => {" + JS_FIND + """
  const best = findBest();
  const cnt = (document.body.innerText.match(/৳/g) || []).length;
  return [document.body.scrollHeight, Math.round(window.scrollY),
          best ? Math.round(best.scrollTop) : -1, best ? best.scrollHeight : -1, cnt];
}"""


class PlaywrightFetcher:
    """JS site er jonno. Scroll kore lazy-load product load kore, ar page er JSON API call dhore.

    headed/pause: browser dekhiye khole, tumi area/branch select korte paro, Enter dile scrape cholbe.
    profile: browser data (area choice, cookies) ei folder e save thake, porer bar ar select korte hoy na.
    """

    def __init__(self, scrolls=8, headed=False, pause=False, profile="browser_profile", state=None, scroll_wait=1500, geo=None, zone=None, area=None, chrome_args=None):
        from playwright.sync_api import sync_playwright

        self.scrolls, self.pause, self._paused_once, self.state = scrolls, pause, False, state
        self.scroll_wait, self.geo = scroll_wait, geo
        self.zone, self.area = zone, area
        self._p = sync_playwright().start()
        self._browser = None
        view = {"width": 1280, "height": 900}
        launch_kw = {"args": chrome_args} if chrome_args else {}
        geo_kw = ({"permissions": ["geolocation"], "geolocation": {"latitude": geo[0], "longitude": geo[1]}}
                  if geo else {})  # site "current location" chaile ei coordinate pabe
        if state:  # location/cookie JSON file e rakhi (portable, cron/server er jonno bhalo)
            self._browser = self._p.chromium.launch(headless=not headed, **launch_kw)
            kw = {"storage_state": state} if os.path.exists(state) else {}
            if kw:
                print(f"  saved state load hoyeche: {state}")
            self._ctx = self._browser.new_context(user_agent=USER_AGENT, viewport=view, **kw, **geo_kw)
            self._page = self._ctx.new_page()
        else:  # browser_profile folder
            self._ctx = self._p.chromium.launch_persistent_context(
                profile, headless=not headed, user_agent=USER_AGENT, viewport=view, **geo_kw, **launch_kw)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.snapshots = []  # scroll er proti step er rendered HTML
        self.reqs = []  # (method, post body) -- payloads er sathe milano
        self.payloads = []  # (url, parsed_json) -- page je API call kore tar JSON
        self._page.on("response", self._on_response)
        self.shots = []  # screenshot path gulo
        self.console = []  # browser console error/warning (debug er jonno)
        self._page.on("console", lambda m: self.console.append((m.type, m.text[:160])) if m.type in ("error", "warning") else None)
        self._page.on("pageerror", lambda e: self.console.append(("pageerror", str(e)[:160])))

    def _click_load_more(self):
        """'Load more' / 'Show more' dhoroner button thakle click kore."""
        rx = re.compile(r"load more|show more|see more|view more|more products|আরও|আরো", re.I)
        for role in ("button", "link"):
            try:
                btn = self._page.get_by_role(role, name=rx).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3000)
                    print("  [load more] click kora hoyeche")
                    self._page.wait_for_timeout(self.scroll_wait)
                    return
            except Exception:
                pass

    def _root(self):
        """Popup/modal thakle shudhu tar bhitor dekhi (page er onno <select>, jemon sort-by, na chhuye)."""
        dlg = self._page.locator("[role=dialog]:visible, [class*=modal i]:visible, [class*=popup i]:visible, "
                                 "[class*=drawer i]:visible, [class*=overlay i]:visible")
        return dlg.first if dlg.count() else None

    def _pick_native(self, root, want, done):
        """<select> dropdown. True=select hoyeche, False=kichu nai/ekhono load hoyni, None=nam pawa jayni."""
        scope = root if root is not None else self._page
        sels = scope.locator("select:visible")
        for i in range(sels.count()):
            if i in done:
                continue
            sel = sels.nth(i)
            opts = sel.evaluate("el => [...el.options].map(o => [o.value, (o.text || '').trim(), o.disabled])")
            real = [(v, t) for v, t, dis in opts if v and not dis and not PLACEHOLDER_RE.search(t)]
            if not real:
                continue
            if want:
                hit = (next((v for v, t in real if want.lower() == t.lower()), None)
                       or next((v for v, t in real if want.lower() in t.lower()), None))
                if hit is None:
                    print(f"  [location] '{want}' pawa jayni. Options: {[t for _, t in real][:12]}")
                    return None
            else:
                hit = real[0][0]
            sel.select_option(value=hit)
            done.add(i)
            self._page.wait_for_timeout(1500)  # dependent dropdown (area) load howar jonno
            return True
        return False

    def _pick_custom(self, root, want, done):
        """role=combobox / role=option dhoroner custom dropdown."""
        page = self._page
        scope = root if root is not None else page
        boxes = scope.locator("[role=combobox]:visible")
        for i in range(boxes.count()):
            if i in done:
                continue
            try:
                boxes.nth(i).click(timeout=2000)
                page.wait_for_timeout(700)
                opts = page.locator("[role=option]:visible")
                texts = [clean_text(t) for t in opts.all_inner_texts()]
                real = [(k, t) for k, t in enumerate(texts) if t and not PLACEHOLDER_RE.search(t)]
                if not real:
                    page.keyboard.press("Escape")
                    continue
                if want:
                    idx = (next((k for k, t in real if want.lower() == t.lower()), None)
                           or next((k for k, t in real if want.lower() in t.lower()), None))
                    if idx is None:
                        print(f"  [location] '{want}' pawa jayni. Options: {[t for _, t in real][:12]}")
                        page.keyboard.press("Escape")
                        return None
                else:
                    idx = real[0][0]
                opts.nth(idx).click(timeout=3000)
                done.add(i)
                page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
        return False

    def _pick_dropdowns(self):
        """Zone/area popup: nam (--zone/--area) dile ta, na dile prothom option select kore confirm kore."""
        root = self._root()
        if root is None and not (self.zone or self.area):
            return False  # popup nai, ar nam-o deya hoyni: kichu chhuyo na
        wanted = [self.zone, self.area, None]
        done_n, done_c, picked = set(), set(), 0
        for step in range(3):
            want = wanted[step]
            r = self._pick_native(root, want, done_n)
            if r is False:
                r = self._pick_custom(root, want, done_c)
            if r is False:  # dependent dropdown ekhono load hoy ni hote pare
                self._page.wait_for_timeout(1500)
                r = self._pick_native(root, want, done_n)
                if r is False:
                    r = self._pick_custom(root, want, done_c)
            if r is None:
                return False
            if not r:
                break
            picked += 1
        if not picked:
            return False
        scope = self._root() or self._page
        for role in ("button", "link"):
            try:
                btn = scope.get_by_role(role, name=CONFIRM_RE).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3000)
                    break
            except Exception:
                pass
        self._page.wait_for_timeout(3000)
        if not self._has_products():
            self._load()  # location localStorage/cookie te thakle reload e product ashbe
        print(f"  [auto-location] dropdown select kora hoyeche (zone={self.zone or 'prothom'}, area={self.area or 'prothom'})")
        return True

    def _dump_controls(self):
        """--debug: page e kon dropdown/button ache, ami jate adapt korte pari."""
        try:
            info = self._page.evaluate(r"""() => {
              const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
              const t = e => (e.innerText || e.value || e.placeholder || '').trim().slice(0, 40);
              return {
                selects: [...document.querySelectorAll('select')].filter(vis).map(s => [...s.options].slice(0, 6).map(o => o.text.trim())),
                comboboxes: [...document.querySelectorAll('[role=combobox],[aria-haspopup]')].filter(vis).map(t),
                inputs: [...document.querySelectorAll('input')].filter(vis).map(i => i.placeholder || i.name || i.type).slice(0, 8),
                buttons: [...document.querySelectorAll('button,[role=button]')].filter(vis).map(t).filter(Boolean).slice(0, 25),
              };
            }""")
            print("  [location] page er controls:", json.dumps(info, ensure_ascii=False)[:900])
        except Exception:
            pass

    def _has_products(self):
        """Server product dilo kina: API te hits ache, ba page e prochur ৳ price ache."""
        if any(isinstance(d, dict) and d.get("hits") for _, d in self.payloads):
            return True
        try:
            return self._page.evaluate(
                "(document.body.innerText.match(/(?:৳|Tk\\.?|BDT|Taka)\\s*\\d/g) || []).length") >= 6
        except Exception:
            return False

    def _ensure_location(self):
        """Manual select chhara location set kore. Product na ashle chesta kore, shesh e True/False."""
        if self._has_products():
            return True
        lat, lng = self.geo
        host = urlparse(self._page.url).netloc
        api = next((p for h, p in LOCATION_APIS.items() if h in host), None)
        if api:
            js = """async (u) => {
                let r = await fetch(u, {credentials: 'include'});
                if (r.status === 405 || r.status === 404) r = await fetch(u, {method: 'POST', credentials: 'include'});
                return r.status;
            }"""
            try:
                status = self._page.evaluate(js, api.format(lat=lat, lng=lng))
                print(f"  [auto-location] location API status={status}")
            except Exception as e:
                print(f"  [auto-location] API call hoyni: {type(e).__name__}")
            self._load()
            if self._has_products():
                return True
        if self._pick_dropdowns() and self._has_products():
            return True
        # location popup er button (best-effort)
        rx = re.compile(r"current location|use my location|detect my|deliver here|set location|confirm location|proceed", re.I)
        for _ in range(3):
            clicked = False
            for role in ("button", "link"):
                try:
                    btn = self._page.get_by_role(role, name=rx).first
                    if btn.count() and btn.is_visible():
                        btn.click(timeout=3000)
                        clicked = True
                        self._page.wait_for_timeout(2500)
                        break
                except Exception:
                    pass
            if not clicked or self._has_products():
                break
        return self._has_products()

    def _on_response(self, resp):
        try:
            if "json" in resp.headers.get("content-type", "") and resp.request.resource_type in ("xhr", "fetch"):
                data = resp.json()
                self.payloads.append((resp.url, data))
                req = resp.request
                try:
                    body = (req.post_data or "")[:600]
                except Exception:
                    body = ""
                hdrs = {k.lower(): v for k, v in req.headers.items()
                        if k.lower() not in NOISE_HEADERS and not k.lower().startswith(("sec-", ":"))}
                self.reqs.append((req.method, body, hdrs))
        except Exception:
            pass

    def _load(self, url=None):
        """Page load kore. 'networkidle' er opor nirbhor kore na (Chaldal er moto site kokhono idle hoy na)."""
        try:
            if url:
                self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            else:
                self._page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  ! page load slow ({type(e).__name__}), ja load hoyeche seta niye cholchi")
        try:
            self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # idle na hole-o problem nai
        self._page.wait_for_timeout(3000)

    def get(self, url):
        try:
            self.payloads.clear()
            self.reqs.clear()
            self._load(url)
            if self.geo and not self.pause:
                if self._ensure_location():
                    print("  [auto-location] OK: product load hocche")
                    if self.state:
                        self._ctx.storage_state(path=self.state)
                else:
                    print("  [auto-location] hoyni. Ekbar --pause diye manual select kore --state save koro.")
                    if DEBUG:
                        self._dump_controls()
            if self.pause and not self._paused_once:
                self._paused_once = True
                input("\n>>> Browser e location/area select koro, tarpor shobji (vegetable) er "
                      "product gulo page e dekha jawa porjonto opekkha koro.\n"
                      "    Product dekha gele ekhane Enter chapo... ")
                self._page.wait_for_timeout(2000)  # reload kori na: location haray jete pare
                if self.state:
                    self._ctx.storage_state(path=self.state)
                    print(f"  location/cookie save hoyeche: {self.state}")
            # scroll: body height er sathe price-text er songkhya-o dekhe (inner scroll container er jonno)
            self.snapshots = [self._page.content()]
            last, stable = None, 0
            for i in range(self.scrolls):
                self._page.evaluate(SCROLL_JS)
                self._page.wait_for_timeout(self.scroll_wait)
                self._click_load_more()
                state = self._page.evaluate(STATE_JS)
                self.snapshots.append(self._page.content())  # virtualized list er jonno proti step e rakhi
                if DEBUG:
                    print(f"  [scroll {i + 1}] height/winY/boxY/boxH/৳count = {state}")
                if state == last:
                    stable += 1
                else:
                    last, stable = state, 0
                if stable >= 3:
                    break
            return self._page.content()
        except Exception as e:
            print(f"  ! playwright: {e}")
            return None

    # ---------- Flutter web (canvas) app: accessibility/semantics DOM diye click ----------
    def _flt_enable(self):
        """Flutter 'accessibility' chalu kore: tokhon button/text er jonno <flt-semantics> DOM node toiri hoy."""
        page = self._page
        for _ in range(3):
            if page.locator("flt-semantics").count():
                return True
            try:
                page.evaluate("() => {" + JS_DEEP + """
                    const p = deep('flt-semantics-placeholder')[0];
                    if (p) { p.focus(); p.click(); } }""")
            except Exception:
                pass
            page.wait_for_timeout(1500)
        return page.locator("flt-semantics").count() > 0

    def _flt_find(self, rx):
        """Label e rx mile emon shobcheye chhoto <flt-semantics> element er (x, y, label), na thakle None."""
        return self._page.evaluate("(rx) => {" + JS_DEEP + """
            const re = new RegExp(rx, 'i');
            const els = deep('flt-semantics').map(e => {
                const r = e.getBoundingClientRect();
                return [e, r, ((e.getAttribute('aria-label') || '') + ' ' + (e.textContent || '')).trim()];
            }).filter(([e, r, t]) => r.width > 2 && r.height > 2 && re.test(t));
            // jeta tap kora jay (button/combobox/option...) seta age: shudhu label (text) node e click kaj kore na
            const act = els.filter(([e]) => e.hasAttribute('flt-tappable') ||
                ['button', 'combobox', 'option', 'menuitem', 'radio', 'checkbox', 'link'].includes(e.getAttribute('role')));
            const pool = act.length ? act : els;
            pool.sort((a, b) => a[1].width * a[1].height - b[1].width * b[1].height);
            if (!pool.length) return null;
            const [e, r, t] = pool[0];
            return [r.x + r.width / 2, r.y + r.height / 2, t.slice(0, 60)];
        }""", rx)

    def _flt_click(self, rx, wait=1200):
        hit = self._flt_find(rx)
        if not hit:
            return False
        self._page.mouse.click(hit[0], hit[1])
        print(f"  [flutter] click: {hit[2]!r}")
        self._page.wait_for_timeout(wait)
        return hit

    def _shot(self, name):
        """Proti step e screenshot (canvas-o dhora porhe), jate bujha jay headless browser ki dekhchhe."""
        try:
            os.makedirs("debug_shots", exist_ok=True)
            path = f"debug_shots/{name}.png"
            self._page.screenshot(path=path)
            self.shots.append(path)
        except Exception:
            pass

    def _boot_diag(self):
        """Flutter boot na holey karon bujhar jonno: page tag, WebGL, console error."""
        try:
            info = self._page.evaluate("""() => ({
                title: document.title, url: location.href,
                body: [...document.body.children].map(e => e.tagName.toLowerCase()).slice(0, 12),
                canvases: document.querySelectorAll('canvas').length,
                webgl: !!document.createElement('canvas').getContext('webgl')
            })""")
            print(f"  [flutter] diagnose: {info}")
        except Exception as e:
            print(f"  [flutter] diagnose hoyni: {type(e).__name__}")
        for kind, text in self.console[-6:]:
            print(f"    console {kind}: {text}")

    def _flt_count(self):
        try:
            return self._page.locator("flt-semantics").count()
        except Exception:
            return 0

    CONFIRM_RX = r"Continue|Delivery Location|Confirm|Save|Done"

    def _menu_leaf_index(self, area):
        """Menu khola thakle: leaf semantics item gulor moddhe 'area' er position (DOM order = UI order)."""
        labels = self._page.evaluate("() => {" + JS_DEEP + """
            return deep('flt-semantics').filter(e => !e.querySelector('flt-semantics'))
                .map(e => (e.getAttribute('aria-label') || e.textContent || '').trim()); }""")
        want = area.lower()
        if len(labels) >= 3:
            for i, t in enumerate(labels):
                if want in t.lower():
                    return i
        return None

    def _menu_open(self):
        """Dropdown menu khola kina: modal route er por niche er 'Continue' button semantics e thake na."""
        return not self._flt_find(self.CONFIRM_RX)

    def _area_selected(self, area):
        """Menu bondho hoyeche ('Continue' ferot eseche) ar dropdown e area er naam dekha jachche."""
        return bool(self._flt_find(self.CONFIRM_RX)) and bool(self._flt_find(re.escape(area)))

    def _select_area(self, area, dd):
        page = self._page
        vp = page.viewport_size or {"width": 1280, "height": 900}
        vw, vh = vp["width"], vp["height"]
        sx, sy = dd[0], min(vh - 120, dd[1] + 150)
        rx = re.escape(area)
        # A) item ta screen er bhitore ana (menu scroll kore), tarpor click, tarpor verify
        for _ in range(16):
            hit = self._flt_find(rx)
            if hit and 40 < hit[1] < vh - 40 and 0 < hit[0] < vw:
                page.mouse.click(hit[0], hit[1])
                print(f"  [flutter] click: {hit[2]!r} (y={int(hit[1])}/{vh})")
                page.wait_for_timeout(1500)
                if self._area_selected(area):
                    return True
                print("  [flutter] click er por area select hoyni (menu ekhono khola ba onno kichu), keyboard try korchi")
                break
            page.mouse.move(sx, sy)
            page.mouse.wheel(0, -300 if (hit and hit[1] <= 40) else 300)
            page.wait_for_timeout(600)
        # B) keyboard: menu er item er position DOM order theke (na hole area-list API theke)
        for off in (0, 1, -1):
            if self._area_selected(area):
                return True
            if not self._menu_open():  # menu bondho, abar khuli
                page.mouse.click(dd[0], dd[1])
                page.wait_for_timeout(1500)
            idx = self._menu_leaf_index(area)
            if idx is None:
                idx = self._area_index(area)
            if idx is None or idx + off < 0:
                continue
            print(f"  [flutter] keyboard: '{area}' list e #{idx} (offset {off:+d})")
            for _ in range(idx + off):
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(120)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            self._shot(f"5_kbd_try_{off}")
        return self._area_selected(area)

    def _area_index(self, area):
        """App er area-list API response (payload) theke 'area' ta list er koto number e ache."""
        want = area.lower()
        for _, data in reversed(self.payloads):
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, list) and len(node) >= 3 and all(isinstance(x, dict) for x in node[:5]):
                    for i, it in enumerate(node):
                        if any(isinstance(v, str) and want in v.lower() for v in it.values()):
                            return i
                if isinstance(node, dict):
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node[:300])
        return None

    def _flt_dump(self, n=40):
        try:
            labels = self._page.evaluate("() => {" + JS_DEEP + """
                return deep('flt-semantics')
                    .map(e => (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 40)).filter(Boolean); }""")
            print(f"  [flutter] page e dekha label ({len(labels)}): {labels[:n]}")
        except Exception:
            pass

    def _product_call_seen(self):
        for purl, data in self.payloads:
            items = _items_from(data)
            if "/api/v1/" in purl and items and isinstance(items[0], dict) and "price" in items[0]:
                return True
        return False

    def _wait_product_call(self, secs):
        for _ in range(int(secs * 2)):
            if self._product_call_seen():
                return True
            self._page.wait_for_timeout(500)
        return self._product_call_seen()

    def flutter_select(self, url, zone="Dhaka", area="Dhanmondi"):
        """Zone card -> area dropdown -> area -> 'Delivery Location' click kore, product API call dhore."""
        ok = self._flutter_select(url, zone, area)
        self._shot("9_final")
        if self.shots:
            print(f"  [flutter] screenshot gulo: {', '.join(self.shots)}")
            if not ok:
                print("  -> ei screenshot gulo (specially 1_loaded, 4_menu_open, 5_after_area) amake pathao.")
        return ok

    def _flutter_select(self, url, zone, area):
        page = self._page
        self.payloads.clear()
        self.reqs.clear()
        self.shots = []
        self._load(url)
        try:
            page.wait_for_selector("flt-glass-pane, flutter-view, flt-semantics-placeholder, flt-scene-host",
                                   timeout=45000, state="attached")
        except Exception:
            print("  ! [flutter] Flutter app load hoyni (flt-glass-pane/flutter-view kichui nai)")
            self._boot_diag()
            return False
        page.wait_for_timeout(4000)  # Flutter er prothom frame draw howar jonno
        self._shot("1_loaded")
        if self._wait_product_call(6):
            print("  [flutter] location age-i select kora chhilo, product load hoyeche")
            return True
        if not self._flt_enable():
            print("  ! [flutter] accessibility chalu hoyni, ei app e auto-select kora gelo na")
            self._boot_diag()
            return False
        page.wait_for_timeout(1500)
        self._shot("2_semantics_on")
        if DEBUG:
            self._flt_dump()
        # cookie banner onno click atkay, tai age bondho kori (decline)
        if self._flt_click(r"^\s*No Thanks", wait=900):
            print("  [flutter] cookie banner bondho kora hoyeche (No Thanks)")
        if not self._flt_click(re.escape(zone), wait=2500):
            print(f"  ! [flutter] zone '{zone}' pawa jayni")
            self._flt_dump()
            return False
        self._shot("3_after_zone")
        # ---- area dropdown khola
        n_before = self._flt_count()
        dd = (self._flt_click(r"^(?!.*Choose).*Select\s*Area", wait=1500)
              or self._flt_click(r"^(?!.*Choose).*Area", wait=1500))
        if not dd:
            btn = self._flt_find(r"Delivery Location|Confirm|Save|Continue|Done")
            if btn:  # layout: dropdown button er ~110px upore (screenshot theke)
                print("  [flutter] dropdown label pawa jayni, layout theke click kora hocche")
                page.mouse.click(btn[0], btn[1] - 111)
                page.wait_for_timeout(1500)
                dd = (btn[0], btn[1] - 111, "layout")
        if not dd:
            print("  ! [flutter] 'Select Area' dropdown pawa jayni")
            self._flt_dump()
            return False
        self._shot("4_menu_open")
        print(f"  [flutter] dropdown click er por semantics node: {n_before} -> {self._flt_count()}")
        # ---- area select (scroll + click + verify, na hole keyboard)
        if not self._select_area(area, dd):
            print(f"  ! [flutter] area '{area}' select hoyni")
            self._flt_dump()
            return False
        self._shot("5_after_area")
        if not self._flt_click(r"Delivery Location|Confirm|Save|Continue|Done", wait=2500):
            print("  ! [flutter] confirm button ('Continue'/'Delivery Location') pawa jayni")
            self._flt_dump()
            return False
        self._shot("6_after_confirm")
        if self._wait_product_call(12):
            return True
        print("  [flutter] product call ashe ni, category page abar load kora hocche...")
        self._load(url)
        self._shot("7_reloaded")
        return self._wait_product_call(15)

    def close(self):
        self._ctx.close()
        if self._browser:
            self._browser.close()
        self._p.stop()


# ---------------------------------------------------------------------------
# Strategy 1 + 2: structured data
# ---------------------------------------------------------------------------
NAME_KEYS = ("nameWithoutSubText", "name", "title", "productName", "product_name", "product_title", "productTitle",
             "item_name", "itemName", "name_en", "english_name")
QTY_KEYS = ("subText", "sub_text", "unit_type", "unit", "unit_name", "uom", "pack_size", "packSize", "weight", "size", "measurement")
PRICE_KEYS = ("discount_price", "discountPrice", "discounted_price", "special_price", "offer_price",
              "sale_price", "salePrice", "selling_price", "sellingPrice", "finalPrice",
              "current_price", "price", "unit_price", "unitPrice")
OLD_KEYS = ("originalPrice", "regular_price", "regularPrice", "old_price", "oldPrice", "mrp", "original_price")
URL_KEYS = ("url", "link", "slug", "permalink", "href")


VARIATION_KEYS = ("product_variations", "variations", "variants", "skus")
ID_KEYS = ("id", "product_id", "productId", "code", "sku", "slug", "key", "uuid", "objectID", "globalCatalogID", "parentID")


def num_from(v):
    """number, "৳ 50", ba {"value": 50, ...} nested object theke price ber kore."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        return to_number(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "price", "current", "selling", "discounted", "final"):
            if k in v and (n := num_from(v[k])):
                return n
        for x in v.values():
            if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0:
                return float(x)
    return None


IMAGE_KEYS = ("image_full_url", "file_path", "photo_url", "picture_url", "image", "imageUrl", "image_url", "imageURL", "img", "picture", "pictureUrl", "picture_url",
              "picturesUrls", "pictures", "images", "imageUrls", "thumbnail", "thumb", "photo", "photos",
              "featured_image", "featuredImage", "primary_image", "mainImage", "main_image")
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|svg)(?:\?|#|$)", re.I)


def _fix_img_placeholder(u):
    """'...jpg?width=%s' / '{width}' dhoroner placeholder e ekta size boshay (Foodpanda etc.)."""
    return re.sub(r"%s|%d|\{width\}|\{w\}|\{height\}|\{h\}|\{size\}", "400", u)


IMGKEY_RE = re.compile(r"image|img|photo|picture|thumb|file_path|media", re.I)
IMGPATH_RE = re.compile(r"/(?:images?|img|media|photos?|pictures?)/", re.I)


def find_any_image(node, base="", depth=0):
    """IMAGE_KEYS e na mille: item er bhitore (3 level porjonto) je string chhobir URL-er moto seta."""
    if depth > 3:
        return ""
    if isinstance(node, dict):
        for k, v in sorted(node.items(), key=lambda kv: 0 if IMGKEY_RE.search(str(kv[0])) else 1):
            if isinstance(v, str):
                u = _fix_img_placeholder(v.strip())
                looks = (bool(IMG_EXT_RE.search(u)) or (bool(IMGPATH_RE.search(u)) and u.startswith(("http", "//")))
                         or (bool(IMGKEY_RE.search(str(k))) and u.startswith(("http", "//", "/"))))
                if looks and not u.startswith("data:") and not is_bad_image(u):
                    return _normalize_img(u, base)
            elif isinstance(v, (dict, list)):
                r = find_any_image(v, base, depth + 1)
                if r:
                    return r
    elif isinstance(node, list):
        for x in node[:5]:
            if isinstance(x, str):  # "urls": ["https://.../image/xyz", ...] (key te 'image' na thakleo)
                u = _fix_img_placeholder(x.strip())
                if u.startswith(("http", "//")) and (IMG_EXT_RE.search(u) or IMGPATH_RE.search(u)) \
                        and not is_bad_image(u):
                    return _normalize_img(u, base)
                continue
            r = find_any_image(x, base, depth + 1)
            if r:
                return r
    return ""


def pick_image(v, base=""):
    """JSON value (string / list / {url: ...}) theke prothom chhobir URL."""
    if isinstance(v, str):
        v = _fix_img_placeholder(v.strip())
        if v and not v.startswith("data:") and (IMG_EXT_RE.search(v) or v.startswith(("http", "//", "/"))):
            u = _normalize_img(v, base)
            return "" if is_bad_image(u) else u
        return ""
    if isinstance(v, list):
        for x in v:
            r = pick_image(x, base)
            if r:
                return r
        return ""
    if isinstance(v, dict):
        for k in ("url", "src", "large", "original", "medium", "small", "thumbnail", "path", "image"):
            if k in v:
                r = pick_image(v[k], base)
                if r:
                    return r
    return ""


def walk_json(node, out, base):
    if isinstance(node, dict):
        name = next((node[k] for k in NAME_KEYS if isinstance(node.get(k), str)), None)
        if name is None:  # {"name": {"en": "...", "bn": "..."}}
            nd = next((node[k] for k in NAME_KEYS if isinstance(node.get(k), dict)), None)
            if nd:
                name = next((v for v in nd.values() if isinstance(v, str) and v), None)
        price = None
        for k in PRICE_KEYS:
            if k in node and not isinstance(node[k], list):
                price = num_from(node[k])
                if price:
                    break
        if not price and isinstance(node.get("offers"), (dict, list)):
            offers = node["offers"]
            offers = offers[0] if isinstance(offers, list) and offers else offers
            if isinstance(offers, dict):
                price = to_number(offers.get("price") or offers.get("lowPrice"))
        var = None  # price variation er bhitore thakle (product -> product_variations[0].price)
        if name and not price:
            for vk in VARIATION_KEYS:
                for v in (node.get(vk) if isinstance(node.get(vk), list) else []):
                    if isinstance(v, dict):
                        p = next((num_from(v[k]) for k in PRICE_KEYS if k in v and not isinstance(v[k], list) and num_from(v[k])), None)
                        if p:
                            price, var = p, v
                            break
                if var:
                    break
        if name and price and len(name) > 2:
            ps = var if var is not None else node  # price er source
            old = next((to_number(ps[k]) for k in OLD_KEYS if ps.get(k)), None)
            if old is None:  # discount price thakle original price-ta old_price
                orig = next((to_number(ps[k]) for k in ("price", "unit_price", "unitPrice")
                             if ps.get(k) is not None and not isinstance(ps[k], (dict, list))), None)
                if orig and orig > price:
                    old = orig
            link = next((node[k] for k in URL_KEYS if isinstance(node.get(k), str)), "")
            qty = next((str(d[k]) for d in ((node, var) if var is not None else (node,)) for k in QTY_KEYS
                        if isinstance(d.get(k), (str, int, float)) and str(d[k]).strip()), "")
            img = next((r for r in (pick_image(node[k], base) for k in IMAGE_KEYS if k in node) if r), "") \
                or find_any_image(node, base)
            ids = {k: str(node[k]) for k in ID_KEYS
                   if isinstance(node.get(k), (str, int)) and not isinstance(node.get(k), bool) and str(node[k]).strip()}
            out.append({"name": clean_text(name), "quantity": qty, "price": price, "old_price": old,
                        "url": urljoin(base, link) if link else "", "image": img,
                        "_ids": ids, "_raw": node if not out else None})
        for k, v in node.items():
            if var is not None and k in VARIATION_KEYS:
                continue  # variation ke alada product dhorbo na
            walk_json(v, out, base)
    elif isinstance(node, list):
        for v in node:
            walk_json(v, out, base)


def extract_structured(soup, base):
    out = []
    for tag in soup.find_all("script"):
        txt = tag.string or tag.get_text() or ""
        t = tag.get("type", "")
        is_json = t in ("application/ld+json", "application/json") or tag.get("id") == "__NEXT_DATA__"
        if not is_json:
            continue
        try:
            walk_json(json.loads(txt), out, base)
        except (json.JSONDecodeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Strategy 3: visual heuristic (repeated cards containing a price)
# ---------------------------------------------------------------------------
def signature(el):
    return (el.name, tuple(sorted(el.get("class", []))))


CUR_ONLY_RE = re.compile(r"^(?:৳|Tk\.?|BDT|Taka|টাকা)$", re.I)
PRICE_FULL_RE = re.compile(
    rf"^\s*(?:(?:৳|Tk\.?|BDT|Taka)\s*{NUM}|{NUM}\s*(?:৳|Tk\b|BDT|Taka|টাকা))\s*$", re.I
)


def price_elements(root):
    """Price dhorano element gulo. "৳" ar "29" alada span e thakle-o parent theke ekshathe dhore."""
    found, seen = [], set()
    for s in root.find_all(string=True):
        t = norm(clean_text(s))
        if not t or not (CUR_ONLY_RE.match(t) or PRICE_RE.search(t)):
            continue
        el = s.parent
        for _ in range(3):
            if el is None or not hasattr(el, "get_text"):
                break
            if PRICE_FULL_RE.match(norm(clean_text(el.get_text(" ")))):
                if id(el) not in seen:
                    seen.add(id(el))
                    found.append(el)
                break
            el = el.parent
    return found


def is_old_price(el):
    for parent in [el] + list(el.parents)[:2]:
        if parent is None or not hasattr(parent, "name"):
            break
        if parent.name in ("del", "s", "strike"):
            return True
        if OLD_PRICE_HINT.search(" ".join(parent.get("class", []))):
            return True
    return False


UNIT = r"(?:kg|gm|gms|g|mg|ml|ltr|litre|liter|l|pcs|pc|piece|pieces|dozen|hali|pack|box|bunch|bundle)"
UNIT_ONLY_RE = re.compile(
    rf"^\(?\s*(?:min\.?\s*)?(?:per\s+)?(?:\d+(?:\.\d+)?\s*)?{UNIT}\s*\)?$", re.I
)
QTY_IN_NAME_RE = re.compile(
    rf"\(?\s*(?:min\.?\s*)?(?:\d+(?:\.\d+)?\s*(?:[-x×+]\s*\d+(?:\.\d+)?\s*)?){UNIT}\b\s*\)?", re.I
)
NOISE_RE = re.compile(
    r"^(add|add to cart|buy( now)?|cart|out of stock|sold out|new|sale|hot|offer|save|off|"
    r"\d+\s*%.*|-?\s*\d+\s*%\s*off|quick view|wishlist|compare|"
    r"\d+\s*(?:-\s*\d+\s*)?(?:hr|hrs|hour|hours|min|mins|minutes|day|days)|express|fast delivery)$", re.I
)


def is_noise(t):
    t = clean_text(t)
    return (
        len(t) < 3
        or PRICE_RE.search(norm(t))
        or UNIT_ONLY_RE.match(t)
        or NOISE_RE.match(t)
        or not re.search(r"[^\W\d_]", t)
    )


def find_quantity(card):
    for s in card.find_all(string=True):
        t = clean_text(s)
        if t and UNIT_ONLY_RE.match(t):
            return t
    return None


IMG_SKIP_RE = re.compile(
    r"(?<![a-z])(?:logo|icon|sprite|placeholder|blank|loading|spinner|pixel|1x1|badge|flag|avatar|payment|"
    r"star|arrow|chevron|caret|cart|heart|wishlist|plus|minus|close|search|menu|bullet|rating|share|ribbon|"
    r"dot|dropdown|toggle|check|tick)s?(?![a-z])", re.I)


def is_bad_image(u):
    """SVG shadharonoto icon/logo, product chhobi na."""
    u = (u or "").lower()
    return bool(u) and u.split("?")[0].split("#")[0].endswith(".svg")


def looks_like_icon(u):
    return bool(IMG_SKIP_RE.search((u or "").lower()))


def _normalize_img(v, base):
    """Next.js '/_next/image?url=<original>&w=..' hole asol URL ber kore; relative hole absolute kore."""
    v = (v or "").strip()
    p = urlparse(v)
    if p.path.endswith("/_next/image"):
        orig = dict(parse_qsl(p.query)).get("url")
        if orig:
            v = orig
    return urljoin(base, v)


def _srcset_pick(s):
    parts = [p.strip().split(" ")[0] for p in (s or "").split(",") if p.strip()]
    return parts[-1] if parts else ""  # shobcheye boro size shadharonoto shesh e thake


def _dim(v):
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def find_image(card, base="", name=""):
    """Card er bhitor theke product chhobi. Icon/svg/chhoto chhobi bad; alt e product name mille bonus."""
    words = set(re.findall(r"[a-z]{3,}", (name or "").lower()))
    cands, order = [], 0
    for img in card.find_all("img"):
        w, h = _dim(img.get("width")), _dim(img.get("height"))
        if (w and w <= 48) or (h and h <= 48):
            continue  # chhoto icon
        for attr in ("data-src", "data-lazy-src", "data-original", "data-lazy", "data-srcset", "srcset", "src"):
            raw = img.get(attr)
            if not raw:
                continue
            raw = (_srcset_pick(raw) if "srcset" in attr else raw).strip()
            if not raw or raw.startswith("data:"):
                continue
            u = _normalize_img(raw, base)
            if is_bad_image(u):
                continue
            alt = (img.get("alt") or "").lower()
            score = (3 if words and any(wd in alt for wd in words) else 0) + (
                2 if re.search(r"\.(?:jpe?g|png|webp|avif)(?:\?|#|$)", u, re.I) else 0) - (
                4 if looks_like_icon(u) else 0)
            cands.append((-score, order, u))
            order += 1
            break
    if cands:
        return sorted(cands)[0][2]
    for so in card.find_all("source"):
        v = _srcset_pick(so.get("srcset") or so.get("data-srcset"))
        if v and not v.startswith("data:") and not is_bad_image(_normalize_img(v, base)):
            return _normalize_img(v, base)
    for el in card.find_all(style=re.compile("background", re.I)):
        m = re.search(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", el.get("style", ""))
        if m and m.group(1) and not m.group(1).startswith("data:") and not is_bad_image(_normalize_img(m.group(1), base)):
            return _normalize_img(m.group(1), base)
    return ""


def find_name(card):
    for h in card.find_all(re.compile(r"^h[1-6]$")):
        t = clean_text(h.get_text(" "))
        if not is_noise(t):
            return t
    cls = re.compile(r"name|title", re.I)
    cands = card.find_all(class_=cls)
    # shobcheye bhitorer (innermost) element age: container e name + quantity ek shathe thakte pare
    cands.sort(key=lambda el: len(el.find_all(class_=cls)))
    for el in cands:
        t = clean_text(el.get_text(" "))
        if not is_noise(t):
            return t
    for a in card.find_all("a"):
        if a.get("title") and not is_noise(a["title"]):
            return clean_text(a["title"])
    for img in card.find_all("img"):
        if img.get("alt") and not is_noise(img["alt"]):
            return clean_text(img["alt"])
    # name shadharonoto [badge] name [quantity] price order e thake:
    # quantity/price er ager shesh "noise na" text ta-i name (badge jemon "Fresh" bad jay)
    last, first = "", ""
    for s in card.find_all(string=True):
        t = clean_text(s)
        if t and (UNIT_ONLY_RE.match(t) or PRICE_RE.search(norm(t))):
            break
        if not is_noise(t):
            last = t
            first = first or t
    if last:
        return last
    for s in card.find_all(string=True):
        t = clean_text(s)
        if not is_noise(t):
            return t
    # last resort: link slug
    a = card.find("a", href=True) or card.find_parent("a", href=True)
    if a:
        slug = urlparse(a["href"]).path.strip("/").split("/")[-1]
        slug = re.sub(r"[-_]+", " ", slug).strip()
        if len(slug) > 2:
            return slug.title()
    return ""


def finalize(item):
    """name theke quantity alada kore."""
    name, qty = clean_text(item.get("name")), clean_text(item.get("quantity"))
    if qty and name.lower().endswith(qty.lower()) and len(name) > len(qty):
        name = name[: -len(qty)].strip(" -,|()")
    elif qty and name.lower().startswith(qty.lower()) and len(name) > len(qty):
        name = name[len(qty):].strip(" -,|()")
    if not qty:
        m = QTY_IN_NAME_RE.search(name)
        if m and name[: m.start()].strip(" -,|()"):
            qty = m.group().strip()
            name = clean_text((name[: m.start()] + " " + name[m.end():]).strip(" -,|()"))
    if qty:
        qty = clean_text(qty.strip("() ").replace("( ", "(")).strip()
    item["name"], item["quantity"] = name, qty or ""
    item["url"], item["image"] = item.get("url") or "", item.get("image") or ""
    if item.get("old_price") and item.get("price") and item["old_price"] <= item["price"]:
        item["old_price"] = None  # mrp == price hole discount nai
    return item


def _scopes(card, n_prices):
    """card, tarpor tar parent gulo -- jotokkhon parent e onno product er price dhoke na."""
    el = card
    for _ in range(5):
        yield el
        par = el.parent
        if par is None or not hasattr(par, "find_all") or par.name in ("body", "html"):
            return
        if len(price_elements(par)) > n_prices:
            return
        el = par


def extract_heuristic(soup, base):
    for t in soup.find_all(SKIP_TAGS):
        t.decompose()
    body = soup.body or soup

    groups = defaultdict(list)  # (signature, depth) -> [elements]
    for pel in price_elements(body):
        el, depth = pel.parent, 0
        while el is not None and el is not body and depth < 9:
            if el.name not in ("html", "body"):
                groups[(signature(el), depth)].append(el)
            el, depth = el.parent, depth + 1

    best, best_score = None, 0
    for (sig, depth), els in groups.items():
        uniq = list({id(e): e for e in els}.values())
        if len(uniq) < 3:
            continue
        # card gulo ekta arekta er bhitore nested hole bad
        if any(a is not b and a in b.parents for a in uniq[:10] for b in uniq[:10]):
            continue
        if max(len(price_elements(e)) for e in uniq) > 4:  # onek price = container, card na
            continue
        if sum(1 for e in uniq if find_name(e)) < len(uniq) * 0.8:
            continue
        score = len(uniq) * 10 - depth  # beshi card, tarpor nicher level
        if score > best_score:
            best, best_score = uniq, score

    if best:  # same parent er onno sibling card (class ektu alada hole-o) gulo-o nao
        ids = {id(c) for c in best}
        extra = []
        for par in {id(c.parent): c.parent for c in best}.values():
            for child in par.find_all(True, recursive=False):
                if id(child) not in ids and 1 <= len(price_elements(child)) <= 4 and find_name(child):
                    extra.append(child)
        best = best + extra

    out = []
    for card in best or []:
        pe = price_elements(card)
        vals = [(to_number(norm(x.get_text(" "))), is_old_price(x)) for x in pe]
        cur = [v for v, old in vals if not old and v]
        old = [v for v, old in vals if old and v]
        if not old and len(cur) == 2 and cur[0] != cur[1]:  # duto price, kono strike nai: choto = current
            cur, old = [min(cur)], [max(cur)]
        nm = find_name(card)
        img, a = "", None
        for sc in _scopes(card, max(1, len(pe))):  # chhobi/link card er baire (sibling div e) thakle-o khuje
            img = img or find_image(sc, base, nm)
            a = a or sc.find("a", href=True)
            if img and a:
                break
        a = a or card.find_parent("a", href=True) or (card if card.name == "a" and card.get("href") else None)
        if DEBUG and not img and len(out) < 3:
            print("  [debug] chhobi pawa jayni. card er <img>:", str(card.find_all("img"))[:300] or "(kono img nai)")
        out.append({"name": nm, "quantity": find_quantity(card), "image": img,
                    "price": cur[0] if cur else (old[0] if old else None),
                    "old_price": old[0] if old and cur else None,
                    "url": urljoin(base, a["href"]) if a else ""})
    return out


# ---------------------------------------------------------------------------
# Optional: manual selectors (jodi kokhono auto kaj na kore)
# ---------------------------------------------------------------------------
def extract_selectors(soup, base, sel):
    out = []
    for card in soup.select(sel["item"]):
        n, p = card.select_one(sel["name"]), card.select_one(sel["price"])
        if n and p:
            a = card.select_one("a[href]")
            out.append({"name": clean_text(n.get_text(" ")), "price": to_number(p.get_text()),
                        "old_price": None, "url": urljoin(base, a["href"]) if a else "",
                        "image": find_image(card, base)})
    return out


SKIP_HOSTS = ("google", "crisp.chat", "visualstudio", "facebook", "doubleclick", "analytics", "sentry",
              "reversegeocode", "SetCustomerDefaults", "/Address/", "/assets/", "FontManifest")


def skeleton(node, key="$", depth=0, lines=None, max_depth=5):
    """JSON er structure (key, type, size, chhoto sample) -- value gulo chhoto kore dey."""
    lines = [] if lines is None else lines
    if len(lines) > 70:
        return lines
    ind = "    " + "  " * depth
    if isinstance(node, dict):
        lines.append(f"{ind}{key}  {{object, {len(node)} keys}}")
        if depth < max_depth:
            for k, v in list(node.items())[:15]:
                skeleton(v, str(k), depth + 1, lines, max_depth)
    elif isinstance(node, list):
        lines.append(f"{ind}{key}  [array, {len(node)} items]")
        if node and depth < max_depth:
            skeleton(node[0], "[0]", depth + 1, lines, max_depth)
    else:
        v = json.dumps(node, ensure_ascii=False)
        lines.append(f"{ind}{key}: {v if len(v) < 40 else v[:37] + '...'}")
    return lines


def html_diagnostics(html):
    """Rendered page e koyta price-moto text ache ar kemon dekhte."""
    text = clean_text(BeautifulSoup(html, "lxml").get_text(" "))
    hits = list(PRICE_RE.finditer(norm(text)))
    print(f"  [debug] page text e price-moto text: {len(hits)} ta")
    for m in hits[:5]:
        print("     ..." + text[max(0, m.start() - 50): m.end() + 10] + "...")


def describe_json(data, max_items=1):
    """Sobcheye boro list-of-dicts khuje tar path + prothom item er structure dekhay."""
    best = (0, "", None)
    stack = [("$", data)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node[:5]) and len(node) > best[0]:
                best = (len(node), path, node)
            stack.extend((f"{path}[]", x) for x in node[:3])
        elif isinstance(node, dict):
            stack.extend((f"{path}.{k}", v) for k, v in node.items())
    if not best[2]:
        return "  (kono list of objects nai) JSON structure:\n" + "\n".join(skeleton(data))

    def short(v, depth=0):
        if isinstance(v, dict) and depth < 2:
            return "{" + ", ".join(f"{k}: {short(x, depth + 1)}" for k, x in list(v.items())[:12]) + "}"
        if isinstance(v, list):
            sample = json.dumps(v[0], ensure_ascii=False)[:80] if v and not isinstance(v[0], (dict, list)) else ""
            return f"[{len(v)} items]" + (f" e.g. {sample}" if sample else "")
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) < 60 else s[:57] + "..."

    n, path, items = best
    lines = [f"  list at {path}  ({n} items). First item:"]
    for it in items[:max_items]:
        for k, v in list(it.items())[:25]:
            lines.append(f"      {k}: {short(v)}")
    return "\n".join(lines)


def parse_page(html, url, mode="auto", selectors=None):
    soup = BeautifulSoup(html, "lxml")
    if mode == "selectors" and selectors:
        return extract_selectors(soup, url, selectors), "selectors"
    if mode in ("auto", "structured"):
        items = extract_structured(soup, url)
        if len(items) >= 3 or (mode == "structured" and items):
            return items, "structured-data"
    if mode in ("auto", "heuristic"):
        items = extract_heuristic(BeautifulSoup(html, "lxml"), url)
        if items:
            return items, "heuristic"
    return [], "none"


# ---------------------------------------------------------------------------
# Scrape + export
# ---------------------------------------------------------------------------
def drop_repeated_images(items, label=""):
    """Ek-i chhobi onek product e thakle (jemon ekta icon), seta chhobi na -- muche dey."""
    from collections import Counter
    cnt = Counter(i["image"] for i in items if i.get("image"))
    bad = {u for u, c in cnt.items() if c >= max(4, 0.5 * len(items))}
    for i in items:
        if i.get("image") in bad:
            i["image"] = ""
    if bad:
        print(f"  ! {len(bad)} ta chhobi onek product e ek-i chhilo (icon dhore), bad deya hoyeche{label}")


def _trim_json(node, depth=0):
    """Sample print er jonno JSON chhoto kore (boro list/string kete)."""
    if isinstance(node, dict):
        return {k: _trim_json(v, depth + 1) for k, v in list(node.items())[:25]} if depth < 4 else "{...}"
    if isinstance(node, list):
        return ([_trim_json(v, depth + 1) for v in node[:2]] + (["..."] if len(node) > 2 else [])) if depth < 4 else "[...]"
    if isinstance(node, str):
        return node if len(node) <= 90 else node[:87] + "..."
    return node


URL_PATTERN_FILE = "url_patterns.json"


def _load_url_pattern(host):
    try:
        with open(URL_PATTERN_FILE, encoding="utf-8") as f:
            return json.load(f).get(host)
    except (OSError, ValueError):
        return None


def fill_urls(items, tpl):
    """Template (jemon https://x.com/p/{id}) diye URL khali product e URL bosay; {id}/{code}/{slug} item er id gulo theke."""
    n = 0
    for it in items:
        if it.get("url"):
            continue
        ids, missing = it.get("_ids") or {}, [False]

        def sub(m):
            v = ids.get(m.group(1))
            if v is None:
                missing[0] = True
                return ""
            return str(v)
        u = re.sub(r"\{(\w+)\}", sub, tpl)
        if not missing[0]:
            it["url"] = u
            n += 1
    return n


def _derive_tpl(url, ids):
    """URL e item er kono id (shobcheye lomba age) thakle sheta {key} diye replace kore template banay."""
    for key, val in sorted(ids.items(), key=lambda kv: -len(kv[1])):
        pat = rf"(?<![A-Za-z0-9]){re.escape(str(val))}(?![A-Za-z0-9])"
        if re.search(pat, url):
            return re.sub(pat, "{" + key + "}", url, count=1)
    return None


def _save_url_pattern(host, tpl):
    try:
        with open(URL_PATTERN_FILE, encoding="utf-8") as f:
            allv = json.load(f)
    except (OSError, ValueError):
        allv = {}
    allv[host] = tpl
    with open(URL_PATTERN_FILE, "w", encoding="utf-8") as f:
        json.dump(allv, f, ensure_ascii=False, indent=2)
    print(f"  [learn-url] URL pattern shikha hoyeche: {tpl}  -> {URL_PATTERN_FILE}")


def learn_url_pattern(fetcher, rows, host):
    """Product URL pattern shikhe: (1) page er <a href> theke (click lage na), (2) na hole browser e product click kore.
    Boro list e upor-er product DOM theke soriye jay, tai je product gulo ekhon page e ache segulo try kore."""
    cands = [r for r in rows if r.get("_ids") and r.get("name")]
    if not cands:
        print("  [learn-url] API item e id/code/slug nai, pattern shikha jabe na")
        return False
    page = fetcher._page
    page_url = page.url
    # ---- 1) <a href> link e product id thakle: click chhara-i pattern
    try:
        hrefs = [h for h in set(page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")) if h != page_url]
    except Exception:
        hrefs = []
    for c in cands:
        for h in hrefs:
            tpl = _derive_tpl(h, c["_ids"])
            if not tpl:
                continue
            filled = [fill for fill in (re.sub(r"\{(\w+)\}", lambda m: o["_ids"].get(m.group(1), "\0"), tpl)
                                        for o in cands if o is not c) if fill in hrefs]
            if filled or sum(1 for x in hrefs if _derive_tpl(x, c["_ids"])) == 1:
                _save_url_pattern(host, tpl)
                return True
    print(f"  [learn-url] page e <a> link: {len(hrefs)} ta, kintu product id wala link nai. Click kore dekhchi...")
    # ---- 2) click: normal -> JS click -> mouse click
    before, last_err, cand, how = page.url, "", None, ""
    for c in cands[-8:][::-1] + cands[:4]:
        loc = page.get_by_text(c["name"][:25], exact=False)
        try:
            n = loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 4)):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                el.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                continue
            for how_try in ("click", "js", "mouse"):
                try:
                    if how_try == "click":
                        el.click(timeout=3000)
                    elif how_try == "js":
                        el.evaluate("e => e.click()")  # overlay/popup upore thakleo element e shorashori click
                    else:
                        bb = el.bounding_box()
                        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                except Exception as e:
                    last_err = f"{type(e).__name__}: {str(e).splitlines()[0][:140]}"
                    continue
                page.wait_for_timeout(1500)
                cand, how = c, how_try
                if page.url != before:
                    break
            if cand is not None:
                break
        if cand is not None:
            break
    if cand is None:
        print(f"  [learn-url] page e kono product click kora jayni: {last_err or 'DOM e product text nai'}")
        return False
    page.wait_for_timeout(1000)
    after = page.url
    if after == before:
        print(f"  [learn-url] click hoyeche ({how}) kintu URL bodlayni (popup/modal hote pare), product e alada URL nai. "
              "Dorkar hole --page-url-fallback ba --item-url dao")
        return False
    tpl = _derive_tpl(after, cand["_ids"])
    if tpl:
        _save_url_pattern(host, tpl)
        return True
    print(f"  [learn-url] click er por URL e kono id mile ni: {after[:140]}")
    print(f"    (product ids: {cand['_ids']})")
    return False


def dedupe(items):
    out = {}
    for i in items:
        out.setdefault((i["name"].lower(), (i.get("quantity") or "").lower(), i["price"]), i)
    return list(out.values())


def enrich(items, other, fields=("url", "image")):
    """items e jei field khali (URL, chhobi), seta onno source (other) theke name+quantity / name / price+quantity diye mile dey."""
    by_nq, by_n, by_pq = {}, {}, {}
    for n in other:
        if not any(n.get(f) for f in fields):
            continue
        nm, q = n["name"].lower(), (n.get("quantity") or "").lower()
        by_nq.setdefault((nm, q), n)
        by_n.setdefault(nm, n)
        by_pq.setdefault((n["price"], q), []).append(n)
    filled = 0
    for it in items:
        need = [f for f in fields if not it.get(f)]
        if not need:
            continue
        nm, q = it["name"].lower(), (it.get("quantity") or "").lower()
        m = by_nq.get((nm, q)) or by_n.get(nm)
        if not m:
            cand = by_pq.get((it["price"], q), [])
            m = cand[0] if len(cand) == 1 else None  # shudhu jokhon ekta-i mile jay
        if m:
            got = False
            for f in need:
                if m.get(f):
                    it[f], got = m[f], True
            filled += got
    return filled


def no_product_diag(fetcher):
    """Product na pele page e asole ki ache dekhay (title, text, screenshot, bot-check korche kina)."""
    page = getattr(fetcher, "_page", None)
    if page is None:
        return
    try:
        title, url = page.title(), page.url
        text = clean_text(page.evaluate("document.body ? document.body.innerText : ''"))[:220]
        print(f"  [diag] URL: {url[:110]}")
        print(f"  [diag] title: {title[:80]!r}")
        print(f"  [diag] page text: {text!r}")
        print(f"  [diag] JSON API call: {len(getattr(fetcher, 'payloads', []))} ta | console: {getattr(fetcher, 'console', [])[-2:]}")
        fetcher._shot("no_products")
        print("  [diag] screenshot: debug_shots/no_products.png (eta amake pathao)")
        if re.search(r"captcha|access denied|are you a robot|verify you are|unusual traffic|blocked|just a moment|press (&|and) hold|too many requests",
                     f"{title} {text}", re.I):
            print("  [diag] site bot-check/block dichhe mone hocche. --headed diye dekho, ba onno folder/profile (age jekhane kaj korto) theke chalao. "
                  "Ami bot-check bypass korbo na.")
    except Exception as e:
        print(f"  [diag] hoyni: {type(e).__name__}")


def scrape(url_template, fetcher, pages, mode):
    rows, seen = [], set()
    site = urlparse(url_template).netloc.replace("www.", "")
    for page in range(1, pages + 1):
        url = page_url(url_template, page)
        if not allowed_by_robots(url, fetcher):
            print(f"[{site}] robots.txt disallow kore: {url}")
            break
        print(f"[{site}] page {page}: {url}")
        html = fetcher.get(url)
        if not html:
            break
        items, how = [], "none"
        seen_html, merged = set(), {}
        for h in (getattr(fetcher, "snapshots", None) or [html]):
            if hash(h) in seen_html:
                continue
            seen_html.add(hash(h))
            part, how_i = parse_page(h, url, mode)
            if DEBUG:
                npr = len(price_elements(BeautifulSoup(h, "lxml")))
                print(f"  [debug] snapshot {len(seen_html)}: page e price={npr}, parse hoyeche={len(part)}")
            if part:
                how = how_i
            for it in part:
                merged[(it["name"], it.get("quantity"), it["price"])] = it
        items = list(merged.values())
        if DEBUG:
            print(f"  [debug] shob snapshot milaye unique product: {len(items)}")
        net = []
        for purl, data in getattr(fetcher, "payloads", []):
            walk_json(data, net, url)
        net = dedupe([finalize(i) for i in net])
        items = dedupe([finalize(i) for i in items])
        drop_repeated_images(items)
        drop_repeated_images(net, " (API)")
        filled = 0
        if mode == "auto" and net and len(net) >= len(items):
            filled = enrich(net, items)  # API te chhobi/URL na thakle page theke
            items, how = net, "network-json"  # API data: URL (slug), old price shoho shothik
        elif net:
            filled = enrich(items, net)  # page e link/chhobi na thakle API theke
        if filled:
            print(f"  onno source theke {filled} ta product er URL/chhobi mila-no hoyeche")
        tpl = ITEM_URL or _load_url_pattern(site)
        if tpl:
            n_url = fill_urls(items, tpl)
            if n_url:
                print(f"  URL pattern diye {n_url} ta URL bosano hoyeche: {tpl}")
        if PAGE_URL_FALLBACK:
            for it in items:
                it["url"] = it.get("url") or url  # product er alada URL na thakle ei page er URL
        if DEBUG:
            tag = re.sub(r"\W+", "_", urlparse(url).path)[:40] or "home"
            with open(f"debug_{site}_{tag}.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("  [debug] saved HTML; JSON calls seen:")
            html_diagnostics(html)
            _pl = getattr(fetcher, "payloads", [])
            _api_only = any("/api/v1/" in u for u, _ in _pl)  # 6amMart: shudhu API call dekhai
            _shown = set()
            for n, (purl, data) in enumerate(_pl):
                if any(h in purl for h in SKIP_HOSTS) or purl in _shown or (_api_only and "/api/v1/" not in purl):
                    continue
                _shown.add(purl)
                mask = lambda t: re.sub(r"-?\d+\.\d{5,}", "<coord>", t)  # location coordinate lukai
                print("   -", mask(purl)[:120])
                if n < len(getattr(fetcher, "reqs", [])):
                    meth, body = fetcher.reqs[n][:2]
                    hd = fetcher.reqs[n][2] if len(fetcher.reqs[n]) > 2 else {}
                    print(f"     request: {meth} {mask(body)[:300]}  headers: {mask(json.dumps(hd))[:200]}")
                if isinstance(data, dict) and "hits" in data:
                    print(f"     meta: page={data.get('page')} nbPages={data.get('nbPages')} "
                          f"hitsPerPage={data.get('hitsPerPage')} nbHits={data.get('nbHits')}")
                if isinstance(data, dict) and data.get("nbHits") == 0:
                    print("  ! server 0 product dilo (nbHits=0): location/area select hoyni ba ei category te kichu nai")
                print(describe_json(data))
                with open(f"debug_json_{site}_{n}.json", "w", encoding="utf-8") as jf:
                    json.dump(data, jf, ensure_ascii=False)
        uniq = {}
        for i in items:
            uniq.setdefault((i["name"], i["quantity"], i["price"]), i)
        new = [i for k, i in uniq.items() if k not in seen and i["price"]]
        print(f"  method={how}  found={len(items)}  new={len(new)}  url={sum(1 for i in items if i.get('url'))}/{len(items)}  image={sum(1 for i in items if i.get('image'))}/{len(items)}")
        if how == "network-json" and items and (not any(i.get("image") for i in items) or not any(i.get("url") for i in items)):
            raw = next((i.get("_raw") for i in items if i.get("_raw")), None)
            if raw:
                print("  ! url/image khali. API item er sample (eta paste koro, ami thik key mila dibo):")
                print("    " + re.sub(r"-?\d+\.\d{5,}", "<coord>", json.dumps(_trim_json(raw), ensure_ascii=False))[:1800])
        if not new:
            if not items:
                print("  ! kono product pawa jayni. --render playwright diye try koro.")
                no_product_diag(fetcher)
            break
        for i in new:
            seen.add((i["name"], i["quantity"], i["price"]))
            i.update(site=site, currency="BDT",
                     scraped_at=datetime.now().isoformat(timespec="seconds"))
            rows.append(i)
        time.sleep(random.uniform(*DELAY_RANGE))
    if LEARN_URL and rows and hasattr(fetcher, "_page") and not _load_url_pattern(site):
        if learn_url_pattern(fetcher, rows, site):  # shikhar sathe sathe ei run er row e-o boshao
            n_url = fill_urls(rows, _load_url_pattern(site))
            print(f"  URL pattern diye {n_url}/{len(rows)} ta URL bosano hoyeche")
    return rows


FIELDS = ["site", "name", "quantity", "price", "old_price", "currency", "url", "image", "image_file", "scraped_at"]


def _layout(n):
    """Header nam bujha na gele, column songkhya diye purono file er layout."""
    v2b = [f for f in FIELDS if f != "image_file"]
    v2 = [f for f in v2b if f != "image"]
    v1 = [f for f in v2 if f != "quantity"]
    return {len(FIELDS): FIELDS, len(v2b): v2b, len(v2): v2, len(v1): v1}.get(n)


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return v


def load_existing(base, formats):
    """Age-r file thakle row gulo poro (csv prefer, na hole json)."""
    rows = []
    try:
        if "csv" in formats and os.path.exists(f"{base}.csv"):
            with open(f"{base}.csv", newline="", encoding="utf-8-sig") as f:
                data = list(csv.reader(f))
            if data:
                header = [h.strip().lower() for h in data[0]]
                # header nam thik thakle name diye, na hole (Excel e edit hole) position diye
                cols = header if {"name", "price"} <= set(header) else _layout(len(header))
                if cols:
                    rows = [dict(zip(cols, r)) for r in data[1:] if any(r)]
        elif "json" in formats and os.path.exists(f"{base}.json"):
            with open(f"{base}.json", encoding="utf-8") as f:
                rows = json.load(f)
    except (OSError, ValueError, csv.Error) as e:
        print(f"  ! age-r file pora jayni ({e}); notun file banano hocche")
        return []
    for r in rows:
        r["price"], r["old_price"] = _num(r.get("price")), _num(r.get("old_price"))
        for f in ("quantity", "url", "image", "image_file"):
            r[f] = r.get(f) or ""
        if is_bad_image(r["image"]):
            r["image"] = ""  # age-r run e dhora icon/svg
    return rows


def merge(old, new):
    """Same (site, name, quantity) hole notun price diye replace, baki gulo rakhe."""
    key = lambda r: (r.get("site"), (r.get("name") or "").lower(), (r.get("quantity") or "").lower())
    merged = {key(r): r for r in old}
    updated = sum(1 for r in new if key(r) in merged)
    for r in new:
        old_r = merged.get(key(r))
        if old_r:  # notun run e URL/chhobi na pele purono ta rekhe dao
            for f in ("url", "image"):
                if not r.get(f) and old_r.get(f):
                    r[f] = old_r[f]
            if not r.get("image_file") and old_r.get("image_file") and r.get("image") == old_r.get("image"):
                r["image_file"] = old_r["image_file"]
        merged[key(r)] = r
    print(f"  merge: age-r {len(old)}, notun {len(new)} ({updated} ta update), mot {len(merged)}")
    return list(merged.values())


def sniff_ext(content, ctype):
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}
    if ctype in ext:
        return ext[ctype]
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:4] == b"GIF8":
        return ".gif"
    return None


def download_images(rows, folder="images"):
    """Proti product er chhobi folder e save kore, row["image_file"] e local path boshay."""
    import hashlib
    global ROBOTS_FILE
    todo = [r for r in rows if r.get("image")]
    if not todo:
        print("Chhobi download: kono image URL nai")
        return
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
    done, ok, cached, failed = {}, 0, 0, 0
    print(f"Chhobi download: {len(todo)} ta -> {folder}/")
    for r in todo:
        u = r["image"]
        if u in done:
            r["image_file"] = done[u]
            continue
        d = os.path.join(folder, re.sub(r"[^\w.-]", "_", r.get("site") or "misc"))
        os.makedirs(d, exist_ok=True)
        stem = hashlib.sha1(u.encode("utf-8")).hexdigest()[:16]
        have = next((f for f in os.listdir(d) if f.startswith(stem + ".")), None)
        if have:  # age-ei download kora
            r["image_file"] = done[u] = os.path.join(d, have).replace("\\", "/")
            cached += 1
            continue
        saved, ROBOTS_FILE = ROBOTS_FILE, None  # --robots-file shudhu product site er; chhobir host er nijer robots dekhi
        try:
            allowed = allowed_by_robots(u)
        finally:
            ROBOTS_FILE = saved
        if not allowed:
            failed += 1
            continue
        try:
            resp = s.get(u, timeout=TIMEOUT, headers={"Referer": r.get("url") or f"https://{r.get('site', '')}/"})
            ext = sniff_ext(resp.content, resp.headers.get("Content-Type", "").split(";")[0].strip().lower())
            if resp.status_code != 200 or not ext or not (200 <= len(resp.content) <= 10_000_000):
                failed += 1
                continue
            path = os.path.join(d, stem + ext).replace("\\", "/")
            with open(path, "wb") as f:
                f.write(resp.content)
            r["image_file"] = done[u] = path
            ok += 1
        except requests.RequestException:
            failed += 1
        time.sleep(random.uniform(0.3, 0.8))
    print(f"  chhobi: notun {ok}, age-r {cached}, hoyni {failed}")


def allowed_ignoring_file(url):
    """--robots-file shudhu product site er; onno host (API/CDN) er nijer robots.txt dekhi."""
    global ROBOTS_FILE
    saved, ROBOTS_FILE = ROBOTS_FILE, None
    try:
        return allowed_by_robots(url)
    finally:
        ROBOTS_FILE = saved


# ---------------------------------------------------------------------------
# 6amMart-based site (Flutter app + Laravel API), jemon Unimart.
# Page canvas e draw hoy (DOM e kichu nai), zone ekta API header (zoneId) diye set hoy,
# tai browser/popup lage na. Website host -> API base:
SIXAM_SITES = {"unimart.online": "https://myadmin.unimart.online"}


def is_sixam(url):
    return urlparse(url).netloc.replace("www.", "") in SIXAM_SITES


def _sixam_get(s, url, headers=None, params=None):
    try:
        r = s.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  ! {type(e).__name__}: {url[:100]}")
        return None
    if DEBUG:
        print(f"  [6am] GET {r.url[:150]} -> {r.status_code}")
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code}: {r.url[:130]}  {clean_text(r.text)[:160]}")
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _zone_header(z):
    """zone id -> '[1]' format (zoneId header JSON array string)."""
    if isinstance(z, str) and z.strip().startswith("["):
        return z.strip()
    if isinstance(z, (list, tuple)):
        return json.dumps(list(z))
    try:
        return json.dumps([int(z)])
    except (TypeError, ValueError):
        return None


def sixam_zone(s, api, zone=None, area=None, lat=None, lng=None):
    """Default zone: --zone/--area nam diye zone list theke, na dile coordinate diye, na dile prothom active zone."""
    base, hdr = f"{api}/api/v1", {"Accept": "application/json", "X-localization": "en"}
    zones = _sixam_get(s, f"{base}/zone/list", hdr)
    if isinstance(zones, dict):
        zones = zones.get("zones") or zones.get("data") or []
    zones = [z for z in zones if isinstance(z, dict) and "id" in z] if isinstance(zones, list) else []
    label = lambda z: z.get("display_name") or z.get("name") or str(z["id"])
    find = lambda w: next((z for z in zones if w.lower() in f"{z.get('name', '')} {z.get('display_name', '')}".lower()), None)
    if zone:  # --zone strict: nam na mille thame (bhul zone e scrape korbo na)
        hit = find(zone)
        if hit:
            print(f"  [6am] zone: {label(hit)} (id {hit['id']})")
            return _zone_header(hit["id"])
        print(f"  ! zone '{zone}' pawa jayni. Available zones: {[label(z) for z in zones][:25]}")
        print("    (Dhanmondi/Gulshan er moto naam zone na, area -- tahole --zone na diye --area dao ba kichu dio na)")
        return None
    if area:  # --area lenient: zone list e na thakle (area alada) default zone e cholbe
        hit = find(area)
        if hit:
            print(f"  [6am] zone: {label(hit)} (id {hit['id']})")
            return _zone_header(hit["id"])
        print(f"  [6am] area '{area}' zone list e nai (area alada hote pare), default zone use kora hocche. "
              f"Zones: {[label(z) for z in zones][:25]}")
    z = _sixam_get(s, f"{base}/config/get-zone-id", hdr, {"lat": lat, "lng": lng})
    zid = _zone_header(z.get("zone_id")) if isinstance(z, dict) else None
    if zid:
        print(f"  [6am] zone coordinate ({lat}, {lng}) theke: {zid}")
        return zid
    active = [z for z in zones if str(z.get("status", 1)) in ("1", "True", "true")]
    if active:
        print(f"  [6am] zone: {label(active[0])} (id {active[0]['id']}) [prothom active zone]")
        return _zone_header(active[0]["id"])
    print("  ! kono zone pawa jayni (zone list/get-zone-id kaj koreni)")
    return None


def _items_from(data):
    items = []
    if isinstance(data, dict):
        items = data.get("products") or data.get("items") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    return items if isinstance(items, list) else []


def sixam_item(it, api, host, url_tpl=""):
    price = to_number(it.get("price"))
    if price is None or not it.get("name"):
        return None
    disc, dtype = to_number(it.get("discount")) or 0, str(it.get("discount_type") or "")
    final = price
    if disc:
        final = price * (1 - disc / 100) if dtype == "percent" else price - disc
        final = round(max(final, 0), 2)
    image = (it.get("image_full_url") if isinstance(it.get("image_full_url"), str) else "") or pick_image(
        it.get("image"), f"{api}/storage/app/public/product/")
    iid = it.get("id")
    url = (url_tpl.replace("{id}", str(iid)).replace("{slug}", str(it.get("slug") or ""))
           if url_tpl and iid is not None else "")
    row = finalize({"name": clean_text(it["name"]), "quantity": "", "price": final,
                    "old_price": price if final < price else None, "url": url, "image": image})
    if not row["quantity"]:
        row["quantity"] = clean_text(str(it.get("unit_type") or ""))
    row.update(site=host, currency="BDT", scraped_at=datetime.now().isoformat(timespec="seconds"))
    return row


def _list_of_dicts(d, *keys):
    if isinstance(d, dict):
        for k in keys:
            if isinstance(d.get(k), list):
                d = d[k]
                break
    return [c for c in d if isinstance(c, dict)] if isinstance(d, list) else []


LEARN_FILE = "sixam_headers.json"
ITEM_URL = None  # --item-url
LEARN_URL = False  # --learn-url
PAGE_URL_FALLBACK = False  # --page-url-fallback


def _load_learned(host, path=None):
    try:
        with open(path or LEARN_FILE, encoding="utf-8") as f:
            return json.load(f).get(host)
    except (OSError, ValueError):
        return None


def learn_sixam(fetcher, page_url, out_file="sixam_headers.json"):
    """Browser e (zone+area select kore) app je product API call korlo, tar header/param save kore rakhe."""
    p = urlparse(page_url)
    host = p.netloc.replace("www.", "")
    cat = parse_qs(p.query).get("id", [None])[0]
    pls, reqs = getattr(fetcher, "payloads", []), getattr(fetcher, "reqs", [])
    for n in range(len(pls) - 1, -1, -1):
        purl, data = pls[n]
        items = _items_from(data)
        if "/api/v1/" not in purl or not items or not isinstance(items[0], dict) or "price" not in items[0]:
            continue
        if n >= len(reqs) or reqs[n][0] != "GET":
            continue
        u = urlparse(purl)
        qd = dict(parse_qsl(u.query))
        last_seg = u.path.rstrip("/").rsplit("/", 1)[-1]
        # browser e jei category te giye call holo (URL er id na-o hote pare) seta khuje {cid} bosai
        used = cat if (cat and (re.search(rf"/{re.escape(cat)}(?=/|$)", u.path) or cat in qd.values())) \
            else (last_seg if last_seg.isdigit() else None)
        path = re.sub(rf"/{re.escape(used)}(?=/|$)", "/{cid}", u.path) if used else u.path
        params = {k: ("{cid}" if used and v == used else v) for k, v in qd.items()
                  if k not in ("limit", "offset", "type")}
        learned = {"api_path": path, "headers": reqs[n][2], "params": params}
        try:
            with open(out_file, encoding="utf-8") as f:
                allv = json.load(f)
        except (OSError, ValueError):
            allv = {}
        allv[host] = learned
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(allv, f, ensure_ascii=False, indent=2)
        mask = lambda t: re.sub(r"-?\d+\.\d{5,}", "<coord>", t)
        print(f"  [learn] product API shikha hoyeche -> {out_file}")
        print(f"    path:    {path}")
        print(f"    headers: {mask(json.dumps(reqs[n][2], ensure_ascii=False))[:300]}")
        print(f"    params:  {mask(json.dumps(params, ensure_ascii=False))[:200]}")
        return True
    seen_paths = sorted({urlparse(u).path for u, _ in pls if "/api/v1/" in u})
    print("  ! [learn] browser e kono product API call (price shoho item list) dhora porenni.")
    print(f"    Ja API call dhora poreche: {seen_paths[:25]}")
    print("    Delivery Location chapar por category te product dekha gele tarpor Enter chapo.")
    return False


def learn_item_url(fetcher, page_url, out_file="sixam_headers.json"):
    """Prothom product click kore URL ki hoy dekhe pattern ({id}) shikhe."""
    first = None
    for purl, data in reversed(getattr(fetcher, "payloads", [])):
        items = _items_from(data)
        if "/api/v1/" in purl and items and isinstance(items[0], dict) and "price" in items[0]:
            first = items[0]
            break
    if not first or first.get("id") is None or not first.get("name"):
        return False
    page = fetcher._page
    fetcher._flt_enable()
    page.wait_for_timeout(1000)
    name = clean_text(first["name"])
    hit = fetcher._flt_find(re.escape(name[:18]))
    if not hit:
        print(f"  [learn] product '{name[:30]}' page e khuje pawa jayni, URL pattern shikha gelo na")
        return False
    before = page.url
    page.mouse.click(hit[0], hit[1])
    page.wait_for_timeout(2500)
    fetcher._shot("8_product_click")
    after = page.url
    pid = str(first["id"])
    tpl = re.sub(rf"(?<=[=/]){re.escape(pid)}(?=[&/#]|$)", "{id}", after)
    if after == before or "{id}" not in tpl:
        print(f"  [learn] product click e URL pattern pawa jayni (URL: {after[:100]})")
        return False
    host = urlparse(page_url).netloc.replace("www.", "")
    try:
        with open(out_file, encoding="utf-8") as f:
            allv = json.load(f)
    except (OSError, ValueError):
        allv = {}
    allv.setdefault(host, {})["item_url"] = tpl
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(allv, f, ensure_ascii=False, indent=2)
    print(f"  [learn] product URL pattern shikha hoyeche: {tpl}")
    return True


def auto_learn_sixam(url, zone="Dhaka", area="Dhanmondi", out_file="sixam_headers.json", headed=False,
                     profile="browser_profile_6am"):
    """Background e (browser na dekhiye) zone+area select kore product API header shikhe file e rakhe."""
    if not allowed_by_robots(url):
        print(f"  ! robots.txt ei page ke disallow kore: {url}")
        return False
    print(f"[auto-learn] zone={zone!r} area={area!r} select kora hocche (background browser)...")
    f = PlaywrightFetcher(scrolls=1, headed=headed, profile=profile, scroll_wait=1000,
                          chrome_args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                                       "--ignore-gpu-blocklist", "--enable-webgl"])  # headless e WebGL (CanvasKit) er jonno
    try:
        ok = f.flutter_select(url, zone, area) and learn_sixam(f, url, out_file)
        if ok:
            learn_item_url(f, url, out_file)
        return ok
    finally:
        f.close()


def sixam_children(s, api, hdr, cat):
    return [c for c in _list_of_dicts(_sixam_get(s, f"{api}/api/v1/categories/childes/{cat}", hdr),
                                      "categories", "data") if "id" in c]


def sixam_diagnose(s, api, hdr, cat):
    """Item na pele: category ache kina, ar kon zone e kichu item ache seta probe kore dekhay."""
    base = f"{api}/api/v1"
    print("  --- diagnose ---")
    cats = _list_of_dicts(_sixam_get(s, f"{base}/categories", hdr), "categories", "data")
    match = next((c for c in cats if str(c.get("id")) == str(cat)), None)
    if match:
        print(f"  category {cat}: name={match.get('name')!r} parent_id={match.get('parent_id')} position={match.get('position')}")
    else:
        print(f"  category {cat} categories list e nai ({len(cats)} ta category). Kichu: "
              f"{[(c.get('id'), c.get('name')) for c in cats[:15]]}")
    zones = [z for z in _list_of_dicts(_sixam_get(s, f"{base}/zone/list", {"Accept": "application/json"}), "zones", "data")
             if "id" in z]
    for z in zones:
        h2 = dict(hdr, zoneId=json.dumps([z["id"]]))
        tot = lambda d: d.get("total_size") if isinstance(d, dict) else None
        top = next((c["id"] for c in cats if str(c.get("id")) != str(cat)), cat)
        any_items = tot(_sixam_get(s, f"{base}/categories/items/{top}", h2, {"limit": 5, "offset": 1, "type": "all"}))
        in_cat = tot(_sixam_get(s, f"{base}/categories/items/{cat}", h2, {"limit": 5, "offset": 1, "type": "all"}))
        print(f"  zone {z['id']} ({z.get('display_name') or z.get('name')}): onno category ({top}) er item={any_items}, category {cat} er item={in_cat}")
    print("  (zone e 'shob item'=0 hole oi zone e store/item nai ba header alada. Category e 0 hole category id/sub-category dekho.)")


def scrape_sixam(page_url, max_pages=50, zone=None, area=None, lat=None, lng=None):
    p = urlparse(page_url)
    host = p.netloc.replace("www.", "")
    api = SIXAM_SITES[host]
    cat = parse_qs(p.query).get("id", [None])[0]
    if not cat:
        print(f"  ! URL e category id nai (?id=...): {page_url}")
        return []
    if not allowed_ignoring_file(f"{api}/api/v1/config"):
        print(f"  ! {api} er robots.txt ei API ke disallow kore -- skip")
        return []
    print(f"[{host}] 6amMart API: {api}  category id={cat}")
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    cfg = _sixam_get(s, f"{api}/api/v1/config", {"Accept": "application/json"})
    module_id = ((cfg or {}).get("module") or {}).get("id", 1)
    learned = None if (zone or area) else _load_learned(host)
    origin = f"{p.scheme}://{p.netloc}"
    if ITEM_URL:
        item_tpl, how = ITEM_URL, "--item-url"
    elif learned and learned.get("item_url"):
        item_tpl, how = learned["item_url"], "browser theke shikha"
    else:
        item_tpl, how = f"{origin}/item-details?id={{id}}&page=item", "6amMart standard (guess, ekta URL khule dekhe nio)"
    print(f"  [6am] product URL pattern ({how}): {item_tpl}")
    if learned:
        print("  [6am] browser theke shikha header/param use kora hocche (zone+area shoho)")
        hdr = {"Accept": "application/json", "X-localization": "en", **learned["headers"]}
        endpoints = [(api + learned["api_path"], False)]
    else:
        zid = sixam_zone(s, api, zone, area, lat, lng)
        if not zid:
            return []
        hdr = {"Accept": "application/json", "Content-Type": "application/json", "X-localization": "en",
               "zoneId": zid, "moduleId": str(module_id), "latitude": str(lat), "longitude": str(lng)}
        endpoints = [(f"{api}/api/v1/categories/items/{{cid}}", False),
                     (f"{api}/api/v1/items/latest", True)]  # fallback (6amMart er onno endpoint)
    seen, limit = set(), 24

    def fetch(cid):
        out, ep = [], 0
        for page in range(1, max_pages + 1):
            while True:
                tpl, by_param = endpoints[ep]
                params = {"limit": limit, "offset": page, "type": "all"}
                if learned:
                    params.update({k: str(v).replace("{cid}", str(cid)) for k, v in learned["params"].items()})
                if by_param:
                    params["category_id"] = cid
                data = _sixam_get(s, tpl.format(cid=cid), hdr, params)
                items = _items_from(data)
                if items or page > 1 or ep == len(endpoints) - 1:
                    break
                if isinstance(data, dict):
                    print(f"  ! 200 OK kintu item nai. keys={list(data)[:10]} total_size={data.get('total_size')} "
                          f"{clean_text(str(data.get('errors') or data.get('message') or ''))[:150]}")
                ep += 1
                print(f"  [6am] alternative endpoint try kora hocche: {endpoints[ep][0].format(cid=cid)}")
            new = 0
            for it in items:
                row = sixam_item(it, api, host, item_tpl) if isinstance(it, dict) else None
                if row and (row["name"], row["quantity"], row["price"]) not in seen:
                    seen.add((row["name"], row["quantity"], row["price"]))
                    out.append(row)
                    new += 1
            print(f"  category {cid} page {page}: {len(items)} item, notun {new}")
            total = to_number(data.get("total_size")) if isinstance(data, dict) else None
            if not items or not new or (total and len(out) >= total):
                break
            time.sleep(random.uniform(0.8, 1.6))
        return out

    rows = fetch(cat)
    if not rows:  # parent category hole item sub-category te thakte pare
        kids = sixam_children(s, api, hdr, cat)
        if kids:
            print(f"  [6am] category {cat} e sub-category: {[(k['id'], k.get('name')) for k in kids][:20]}")
            for k in kids:
                rows.extend(fetch(k["id"]))
    if not rows:
        sixam_diagnose(s, api, hdr, cat)
    return rows


def _write(path, writer):
    try:
        writer(path)
        print(f"Saved {path}")
    except PermissionError:
        alt = path.replace(".", f"_{datetime.now():%H%M%S}.", 1)
        print(f"  ! {path} khola ache (Excel bondho koro). Tai save hoyeche: {alt}")
        writer(alt)


def export(rows, base, formats, overwrite=False):
    if not overwrite:
        rows = merge(load_existing(base, formats), rows)

    def w_csv(p):
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    def w_json(p):
        with open(p, "w", encoding="utf-8") as f:
            json.dump([{k: r.get(k) for k in FIELDS} for r in rows], f, ensure_ascii=False, indent=2)

    if "csv" in formats:
        _write(f"{base}.csv", w_csv)
    if "json" in formats:
        _write(f"{base}.json", w_json)


def main():
    ap = argparse.ArgumentParser(description="BD grocery price scraper (auto-detect)")
    ap.add_argument("--url", nargs="+", required=True, help="category page URL(s)")
    ap.add_argument("--pages", type=int, default=1, help="koyta page (default 1)")
    ap.add_argument("--render", choices=["requests", "playwright"], default="requests")
    ap.add_argument("--scrolls", type=int, default=60, help="Playwright e koybar scroll korbe (infinite scroll site)")
    ap.add_argument("--mode", choices=["auto", "structured", "heuristic"], default="auto")
    ap.add_argument("--format", nargs="+", default=["csv", "json"], choices=["csv", "json"])
    ap.add_argument("--out", default="grocery_prices")
    ap.add_argument("--headed", action="store_true", help="browser dekhiye khole (default: lukano)")
    ap.add_argument("--pause", action="store_true", help="page khule Enter er jonno opekkha kore; area select korar jonno (--headed shoho)")
    ap.add_argument("--scroll-wait", type=int, default=1500, help="proti scroll er por koto ms opekkha (default 1500). Slow site e baro")
    ap.add_argument("--auto-location", action="store_true", help="location nijei set kore (--lat/--lng), manual select lage na")
    ap.add_argument("--learn-url", action="store_true", help="browser e prothom product click kore product URL pattern shikhe (url_patterns.json), tarpor shob product e boshay")
    ap.add_argument("--page-url-fallback", action="store_true", help="product er alada URL na thakle category/page er URL boshay")
    ap.add_argument("--item-url", help="product page URL pattern, jemon https://www.unimart.online/item-details?id={id}&page=item ({id}/{slug} bosbe)")
    ap.add_argument("--auto-learn", action="store_true", help="6amMart/Flutter site (Unimart): background e zone+area select kore header shikhe, tarpor scrape kore. Default zone=Dhaka area=Dhanmondi")
    ap.add_argument("--relearn", action="store_true", help="--auto-learn: shikha file thakleo abar select kore shikho")
    ap.add_argument("--learn", nargs="?", const="sixam_headers.json", metavar="FILE", help="--browser shoho: app er product API header/param save kore (por theke browser chhara cholbe)")
    ap.add_argument("--browser", action="store_true", help="6amMart site (Unimart) ke-o browser diye chalao: API call/header dekhte (--pause --debug shoho)")
    ap.add_argument("--zone", help="location popup er zone/city (jemon Dhaka). Na dile prothom option")
    ap.add_argument("--area", help="location popup er area (jemon Dhanmondi). Na dile prothom option")
    ap.add_argument("--lat", type=float, default=23.7461, help="auto-location latitude (default: Dhaka, Dhanmondi er kachhe)")
    ap.add_argument("--lng", type=float, default=90.3742, help="auto-location longitude")
    ap.add_argument("--state", help="location/cookie JSON file (cron/server er jonno). Prothom bar --pause shoho chalao, tarpor shudhu --state dao")
    ap.add_argument("--profile", default="browser_profile", help="browser data folder (area choice mone rakhe)")
    ap.add_argument("--download-images", nargs="?", const="images", metavar="FOLDER", help="chhobi folder e save kore, CSV te image_file e local path dey (default folder: images)")
    ap.add_argument("--overwrite", action="store_true", help="age-r file replace kore (default: notun data jog hoy)")
    ap.add_argument("--robots-file", help="robots.txt fetch na hole: browser theke save kora file")
    ap.add_argument("--debug", action="store_true", help="rendered HTML save kore + JSON API call dekhay")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args()
    print(f"grocery_scraper {VERSION}")
    global DEBUG, ROBOTS_FILE, LEARN_FILE, ITEM_URL, LEARN_URL, PAGE_URL_FALLBACK
    DEBUG, ROBOTS_FILE = args.debug, args.robots_file
    ITEM_URL, LEARN_URL, PAGE_URL_FALLBACK = args.item_url, args.learn_url, args.page_url_fallback
    LEARN_FILE = args.learn or "sixam_headers.json"
    if args.browser:
        args.render = "playwright"

    fetcher = None
    use_api = lambda u: is_sixam(u) and not args.browser
    if any(not use_api(u) for u in args.url):  # 6amMart site e browser lage na
        fetcher = PlaywrightFetcher(args.scrolls, args.headed or args.pause, args.pause, args.profile, args.state, args.scroll_wait,
                                     (args.lat, args.lng) if (args.auto_location or args.zone or args.area) else None,
                                     args.zone, args.area) if args.render == "playwright" else RequestsFetcher()
    rows = []
    try:
        for u in args.url:
            if use_api(u):
                z, a = args.zone, args.area
                if args.auto_learn:
                    host = urlparse(u).netloc.replace("www.", "")
                    learn_file = args.learn or "sixam_headers.json"
                    if args.relearn or not _load_learned(host, learn_file):
                        auto_learn_sixam(u, z or "Dhaka", a or "Dhanmondi", learn_file, args.headed)
                    if not _load_learned(host, learn_file):
                        print("! [auto-learn] shikha jayni, tai scrape korchi na (area chhara 0 item ashto).")
                        print("  Try koro: --auto-learn --relearn --headed --debug  (browser dekhe karon bujhte)")
                        print("  ba ekbar manually: --browser --pause --learn")
                        continue
                    z = a = None  # shikha header use hobe (zone+area shoho)
                rows.extend(scrape_sixam(u, args.pages if args.pages > 1 else 50, z, a, args.lat, args.lng))
            else:
                rows.extend(scrape(u, fetcher, args.pages, args.mode))
                if args.learn and is_sixam(u):
                    learn_sixam(fetcher, u, args.learn)
    finally:
        if fetcher:
            fetcher.close()

    print(f"\nTotal products: {len(rows)}")
    if rows:
        if args.download_images:
            download_images(rows, args.download_images)
        export(rows, args.out, args.format, args.overwrite)
    else:
        print("! kono product pawa jayni. Upore error/zone message dekho. Browser site e location expire hole --pause diye abar select koro.")
        sys.exit(2)  # cron/Task Scheduler e failure dhora jabe


if __name__ == "__main__":
    main()
