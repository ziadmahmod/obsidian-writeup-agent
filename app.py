"""
Writeup Agent — Local Flask server that uses your Claude Max subscription
via the `claude` CLI to summarize bug bounty writeups into Obsidian notes.

Run: python app.py
Then open: http://localhost:5050
"""

import base64
import json
import mimetypes
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse


def _cli_bin(name: str) -> str:
    """Find a CLI binary, including the .cmd shim Windows npm installs use."""
    return shutil.which(name) or shutil.which(name + ".cmd") or name


# Local CLIs that authenticate via their own login flow (no API key needed
# in our config) and follow the same `<bin> -p` / `--model` invocation shape.
LOCAL_CLI_BINARIES = {
    "claude_cli":   ("claude",   "Claude CLI"),
    "gemini_cli":   ("gemini",   "Gemini CLI"),
    "qwen_cli":     ("qwen",     "Qwen Code CLI"),
    "opencode_cli": ("opencode", "OpenCode CLI"),
}

import requests
import trafilatura
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).parent.resolve()
CONFIG_DIR = Path.home() / ".writeup-agent"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "vault_path": str(Path.home() / "Documents" / "WriteupVault"),
    "notes_subdir": "Writeups",
    "attachments_subdir": "attachments",
    # AI provider. One of:
    #   "claude_cli"     — uses the local `claude` CLI (Claude Max sub, no API key)
    #   "openai"         — OpenAI API (needs api_key)
    #   "anthropic_api"  — Anthropic API direct (needs api_key)
    #   "gemini"         — Google Gemini via its OpenAI-compatible endpoint
    #   "openrouter"     — OpenRouter unified gateway (any model)
    "provider": "claude_cli",
    # Model name. Per-provider defaults applied if empty.
    "model": "sonnet",
    # API key for HTTP providers. Ignored for claude_cli. Stored plain-text
    # in ~/.writeup-agent/config.json — never commit that file.
    "api_key": "",
}

# Per-provider sensible defaults if the user leaves `model` empty.
PROVIDER_DEFAULT_MODEL = {
    "claude_cli": "sonnet",
    "gemini_cli": "",   # Gemini CLI picks its own default (gemini-2.5-pro)
    "qwen_cli":   "",   # Qwen CLI picks its own default (qwen3-coder-plus)
    "opencode_cli": "anthropic/claude-sonnet-4-5-20250929",  # provider/model format
    "openai": "gpt-4o-mini",
    "anthropic_api": "claude-sonnet-4-5-20250929",
    "gemini": "gemini-2.0-flash",
    "openrouter": "google/gemini-2.5-flash",
}

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    merged = {**DEFAULT_CONFIG, **cfg}
    CONFIG_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Content extraction
# ─────────────────────────────────────────────────────────────────────────────

IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:max_len].strip("-") or "writeup"


def fetch_html(url: str) -> str:
    """Fetch the page HTML. Falls back to r.jina.ai (a public fetch proxy
    that handles bot detection on its end) when the direct request is
    blocked — common for Medium, Cloudflare-fronted blogs, and when the
    client IP is on a residential / VPN / Kali range."""
    try:
        resp = requests.get(url, headers=UA, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status in (401, 403, 429, 451, 503):
            # Fetch through r.jina.ai. It returns the page rendered as
            # markdown; we wrap it in a minimal HTML envelope so the
            # downstream trafilatura/extract pipeline can still work
            # (it'll mostly pass-through, which is fine).
            print(f"      Direct fetch returned {status}; falling back to r.jina.ai...")
            r = requests.get(
                f"https://r.jina.ai/{url}",
                headers={"Accept": "text/html", "User-Agent": UA["User-Agent"]},
                timeout=60,
            )
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            body = r.text
            if "<html" not in body.lower():
                # jina returned plain markdown — wrap so the extractor accepts it
                body = f"<html><body><article>{body}</article></body></html>"
            return body
        raise


def _pick_largest_from_srcset(srcset: str) -> str:
    """Parse a srcset like 'url1 640w, url2 1100w, url3 2x' and return the URL
    with the largest descriptor. Falls back to the last URL if no descriptors."""
    if not srcset:
        return ""
    best_url, best_size = "", -1
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        url = tokens[0]
        size = 0
        if len(tokens) > 1:
            desc = tokens[1].lower()
            if desc.endswith("w"):
                try:
                    size = int(desc[:-1])
                except ValueError:
                    pass
            elif desc.endswith("x"):
                try:
                    size = int(float(desc[:-1]) * 1000)
                except ValueError:
                    pass
        if size > best_size or not best_url:
            best_size = size
            best_url = url
    return best_url


_LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-original-src")


def preprocess_lazy_images(html: str) -> str:
    """Rewrite lazy-loaded image markup so trafilatura's <img> extraction works.

    Covers three common patterns:
      1. <picture><source srcset="..."/><img/></picture> (Medium, Substack)
      2. <img data-src="..." src="placeholder.gif"/> (jQuery lazy plugins, WordPress)
      3. <img srcset="url 640w, url 1200w"/> with empty/missing src (responsive)
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # 1. <picture>: lift the best <source srcset> URL onto the inner <img>
    for pic in soup.find_all("picture"):
        img = pic.find("img")
        if not img:
            continue
        if img.get("src") and not img.get("src", "").startswith("data:"):
            continue  # already has a real src
        for source in pic.find_all("source"):
            srcset = source.get("srcset") or source.get("srcSet") or ""
            best = _pick_largest_from_srcset(srcset)
            if best:
                img["src"] = best
                break

    # 2 + 3. handle every <img> regardless of <picture>
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if src and not src.startswith("data:"):
            continue
        # try lazy data-* attrs first
        for attr in _LAZY_SRC_ATTRS:
            v = img.get(attr)
            if v:
                img["src"] = v
                src = v
                break
        if src and not src.startswith("data:"):
            continue
        # fall back to srcset on the <img> itself
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        best = _pick_largest_from_srcset(srcset)
        if best:
            img["src"] = best

    return str(soup)


def extract_markdown(html: str, base_url: str) -> str:
    """Extract main content as markdown; falls back to alternatives."""
    html = preprocess_lazy_images(html)
    md = trafilatura.extract(
        html,
        output_format="markdown",
        include_images=True,
        include_links=True,
        include_tables=True,
        favor_recall=True,
    )

    if not md or len(md) < 400:
        try:
            r = requests.get(
                f"https://r.jina.ai/{base_url}",
                headers={"Accept": "text/plain"},
                timeout=30,
            )
            if r.ok and len(r.text) > 400:
                md = r.text
        except Exception:
            pass

    if not md:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        container = soup.find("article") or soup.find("main") or soup.body
        md = container.get_text("\n", strip=True) if container else ""

    def absolutize(match: re.Match) -> str:
        alt = match.group(1)
        src = match.group(2).strip().split(" ")[0]
        if src.startswith(("http://", "https://")):
            return f"![{alt}]({src})"
        return f"![{alt}]({urljoin(base_url, src)})"

    md = IMG_RE.sub(absolutize, md)
    return md


def replace_images_with_placeholders(md: str):
    """Replace each ![](url) with [Screenshot N]. Returns (new_md, ordered_urls)."""
    urls = []

    def repl(match):
        src = match.group(2).strip().split(" ")[0]
        if not src.startswith(("http://", "https://")):
            return match.group(0)
        if src not in urls:
            urls.append(src)
        idx = urls.index(src) + 1
        return f"[Screenshot {idx}]"

    new_md = IMG_RE.sub(repl, md)
    return new_md, urls


# ─────────────────────────────────────────────────────────────────────────────
# Image downloading
# ─────────────────────────────────────────────────────────────────────────────

EXT_BY_CT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}


def download_images(urls, dest_dir: Path, slug: str):
    """Download each image; returns {url: relative_filename}."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    mapping = {}

    for i, url in enumerate(urls, start=1):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()

            ct = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            ext = EXT_BY_CT.get(ct)
            if not ext:
                guess = Path(urlparse(url).path).suffix.lower()
                ext = guess if guess in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"} else ".png"

            fname = f"{slug}-{i:02d}{ext}"
            fpath = dest_dir / fname
            fpath.write_bytes(r.content)
            mapping[url] = fname
            print(f"  ✓ Image {i}: {fname} ({len(r.content) // 1024} KB)")
        except Exception as e:
            print(f"  ✗ Image {i} failed: {e}")

    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# Claude CLI invocation
# ─────────────────────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """انت محلل أمن سيبراني هجومي خبير. هتحلل محتوى متعلق بـ bug bounty / offensive security وتطلعلي تحليل عميق ومفصل عشان يتحفظ في Obsidian كمرجع تعليمي.

المحتوى ممكن يكون:
- writeup لثغرة محددة (الأكتر شيوعاً)
- مقال methodology / technique / recon (مثلاً "ازاي تلاقي hidden APIs"، "tips للـ subdomain enumeration"، "checklist لـ IDOR hunting")
- tutorial أو guide عن tool أو tactic معين في bug bounty
- tips & tricks article من باحث

كل ما سبق valid ومقبول. تعامل معاه كله بنفس البنية، وعدّل الـ fields حسب طبيعة المحتوى:
- لو writeup لثغرة محددة: املأ كل الـ fields زي ما هي
- لو methodology/technique article: حط `vulnerability_type` = "Methodology" أو نوع الـ technique (مثلاً "Recon", "API-Hunting", "Subdomain-Enum"), `severity` = "N/A", `target` = "N/A", `bounty` = "N/A", واعتبر "attack_scenario" = "السيناريو/الـ workflow اللي بيشرحه المقال", "payloads" = الأوامر/الـ requests/الـ tools اللي بيستخدمها, "root_cause" = "ليه التكنيك ده بيشتغل / إيه الـ insight ورا الفكرة"

محتوى الـ writeup مكتوب تحت. الصور مشار ليها بصيغة [Screenshot 1], [Screenshot 2]... لما تكون صورة معينة مهمة لفهم نقطة، اذكرها في الشرح بنفس الصيغة دي بالظبط [Screenshot N] عشان نقدر نركبها في المكان الصح.

ارجعلي JSON object صالح فقط (من غير ``` ومن غير أي كلام قبل أو بعد) بالشكل ده بالظبط:

{
  "title": "عنوان وصفي مختصر بالإنجليزي (max 80 chars)",
  "vulnerability_type": "نوع الثغرة الأساسي: XSS, IDOR, SSRF, RCE, SQLi, CSRF, SSTI, Auth-Bypass, Account-Takeover, Race-Condition, Path-Traversal, XXE, Deserialization, Business-Logic, Open-Redirect, OAuth-Misconfiguration, GraphQL, Cache-Poisoning, etc.",
  "severity": "Low | Medium | High | Critical",
  "target": "اسم الشركة / التطبيق / البرنامج",
  "bounty": "المبلغ لو متذكر مثلاً '$5,000' وإلا 'Not disclosed'",
  "researcher": "اسم الباحث / handle لو موجود وإلا 'Unknown'",
  "tldr_ar": "ملخص في جملتين بالعربي المصري — يديك صورة سريعة عن الباج",
  "tldr_en": "1-2 sentence English summary",

  "core_idea_explained": "شرح تفصيلي بالعربي المصري للفكرة الذكية اللي خلت الباج ده يشتغل. ده أهم قسم في التحليل. اشرح: ايه الـ insight اللي اكتشفه الباحث؟ ليه ده مش obvious؟ ايه اللي بيخليه creative؟ خليه 3-5 فقرات تشرح المنطق بعمق بحيث اللي يقرا الشرح ده بس يبقى فاهم الفكرة كاملة من غير ما يقرا الـ writeup الأصلي. استخدم [Screenshot N] لو محتاج تشير لصورة.",

  "attack_scenario": "شرح بالعربي المصري للسيناريو الكامل للهجوم بشكل سردي مش bullet points. اشرح ايه اللي بيحصل في كل مرحلة وليه. اذكر الـ requests والـ responses لما تكون مهمة. اشرح من منظور المهاجم. 3-6 فقرات. استخدم [Screenshot N] في السياق المناسب.",

  "attack_steps_concise": [
    "خطوة قصيرة عملية 1",
    "خطوة قصيرة عملية 2"
  ],

  "payloads": [
    {
      "description": "شرح بالعربي للـ payload ده وامتى يستخدم وايه اللي بيعمله",
      "code": "الـ payload أو الـ HTTP request زي ما هو بالظبط",
      "language": "http | js | sql | bash | python | json | xml | yaml | text"
    }
  ],

  "root_cause_explained": "شرح تفصيلي بالعربي المصري للسبب الجذري للثغرة. ايه الخطأ في الكود / المعمارية / المنطق اللي سمح بالباج ده يحصل؟ مش الـ symptom، الـ root cause الحقيقي. 2-4 فقرات.",

  "remediation": "شرح عملي بالعربي ازاي يتم اصلاح الثغرة دي صح. مش بس defensive coding generic، اقترح حل محدد للحالة دي.",

  "lessons": [
    "درس مستفاد عملي 1 (بالعربي)",
    "درس 2"
  ],

  "related_techniques": ["technique-1-english-slug", "technique-2"],
  "tags": ["english-lowercase-hyphenated", "tags-for-obsidian"]
}

قواعد مهمة جداً:
- كل حقول الشرح (اللي بتنتهي بـ _ar أو _explained أو attack_scenario أو descriptions) لازم تكون بالعربي المصري الدارج، مش الفصحى الجامدة
- الكود والـ payloads يفضلوا زي ما هم بالإنجليزي
- الـ tags كلها إنجليزي lowercase-hyphenated
- كن مفصّل وعميق في الشرح — ده مرجع تعليمي مش مجرد summary
- اذكر [Screenshot N] بالظبط في السياق المناسب
- ارجع {"title": "NOT_A_WRITEUP"} بس لو المحتوى ملوش علاقة نهائياً بـ bug bounty / infosec / offensive security / web security / API security / recon / penetration testing (مثلاً صفحة طبخ، خبر سياسة، صفحة 404). أي مقال فيه قيمة لباحث bug bounty مقبول حتى لو مش writeup لثغرة محددة.

محتوى الـ writeup:
---
__CONTENT__
---

ارجع الـ JSON دلوقتي مباشرة بدون أي كلام قبله أو بعده.
"""


def call_local_cli(prompt: str, binary: str, friendly_name: str, model: str = "") -> str:
    """Run a local AI CLI (`claude`, `gemini`, `qwen`) with the prompt on stdin.
    Each of these CLIs accepts `-p` for non-interactive mode and `--model` for
    selecting the model. Quirks:
      - claude: `-p` is a no-arg flag; stdin is read as the prompt.
      - gemini: `-p` REQUIRES a value (commander parser); use `-p ""` so that
        the prompt comes from stdin per its docs ("Appended to input on stdin").
      - qwen: same shape as gemini; harmless to also pass `-p ""`.
    """
    if binary == "opencode":
        # opencode uses a subcommand instead of -p, and takes the message as a
        # positional arg (or stdin if none). We pass on stdin so prompts of
        # any size work on Windows (no command-line length limit).
        cmd = [_cli_bin(binary), "run"]
        if model:
            cmd += ["--model", model]
        try:
            result = subprocess.run(
                cmd, input=prompt, capture_output=True,
                text=True, timeout=900, encoding="utf-8",
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"أمر `{binary}` مش موجود في الـ PATH. اتأكد إن {friendly_name} متثبت."
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{friendly_name} خد وقت أكتر من 15 دقيقة.")
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:800]
            raise RuntimeError(f"{friendly_name} رجع error (code {result.returncode}):\n{err}")

        # opencode prints ANSI escape codes and a "> build · <model>" header,
        # AND exits 0 even when the provider denies the request — with the
        # actual error on stderr. So we must check both streams and only
        # report success if stdout has the model's response.
        def _strip_ansi(s):
            return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "")

        out = _strip_ansi(result.stdout)
        err = _strip_ansi(result.stderr)
        # Any "Error: ..." line anywhere is a real failure.
        for stream_name, stream in (("stderr", err), ("stdout", out)):
            for line in stream.splitlines():
                line = line.strip()
                if line.startswith("Error:"):
                    raise RuntimeError(
                        f"{friendly_name} رجع error من الـ provider:\n{line}\n\n"
                        f"جرّب `opencode auth login` واختار provider عندك credentials ليه "
                        f"(Anthropic / OpenAI / إلخ)."
                    )
        # Drop the leading banner line "> build · <model>" if present.
        cleaned = re.sub(r"^\s*>\s+build\s+·.*\n", "", out, count=1).strip()
        if not cleaned:
            # stdout was empty but no explicit Error line — surface stderr.
            raise RuntimeError(
                f"{friendly_name} ما رجعش output.\nstderr:\n{err[:600]}"
            )
        return cleaned

    cmd = [_cli_bin(binary), "-p"]
    if binary in ("gemini", "qwen"):
        cmd.append("")
    if binary == "qwen":
        # Qwen Code is agentic by default (workspace discovery, tool calls,
        # approval prompts) which makes it slow and produces shortened
        # summaries. Force pure LLM behavior with these flags:
        #   --approval-mode yolo: never pause for confirmation
        #   --system-prompt:      replace the "coding agent" persona so the
        #                         model just answers the analysis prompt
        # Note: we intentionally do NOT pass --bare (it also drops the auth
        # config) or --auth-type (qwen-oauth free tier was discontinued
        # 2026-04-15; rely on whichever auth the user has set via `qwen auth`).
        cmd += [
            "--approval-mode", "yolo",
            "--system-prompt",
            "You are a security analyst. Respond ONLY with the JSON object "
            "exactly as the user message specifies. Do not call any tools, "
            "do not explore files, do not ask questions, do not add commentary. "
            "Output the JSON directly with no surrounding text or fences.",
        ]
    if model:
        cmd += ["--model", model]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=900,
            encoding="utf-8",
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"أمر `{binary}` مش موجود في الـ PATH. اتأكد إن {friendly_name} متثبت ومعرّف صح."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{friendly_name} خد وقت أكتر من 15 دقيقة. جرب writeup أقصر.")

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:800]
        raise RuntimeError(f"{friendly_name} رجع error (code {result.returncode}):\n{err}")

    return result.stdout.strip()


def _call_openai_compat(prompt: str, api_key: str, model: str, base_url: str, provider_name: str) -> str:
    """Shared HTTP path for any OpenAI-compatible /chat/completions endpoint
    (OpenAI, Gemini's OpenAI-compat endpoint, OpenRouter)."""
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
            },
            timeout=900,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"{provider_name} request فشل: {e}")

    if r.status_code != 200:
        snippet = (r.text or "").strip()[:600]
        raise RuntimeError(f"{provider_name} رجع error ({r.status_code}):\n{snippet}")

    try:
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError) as e:
        raise RuntimeError(f"{provider_name} رد بشكل مش متوقع: {e}\n{r.text[:400]}")


def call_anthropic_api(prompt: str, api_key: str, model: str) -> str:
    """Anthropic Messages API (api.anthropic.com)."""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 16000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=900,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Anthropic API request فشل: {e}")

    if r.status_code != 200:
        snippet = (r.text or "").strip()[:600]
        raise RuntimeError(f"Anthropic API رجع error ({r.status_code}):\n{snippet}")

    try:
        data = r.json()
        return data["content"][0]["text"].strip()
    except (ValueError, KeyError, IndexError) as e:
        raise RuntimeError(f"Anthropic API رد بشكل مش متوقع: {e}\n{r.text[:400]}")


def call_llm(prompt: str, cfg: dict) -> str:
    """Dispatch to the configured AI provider."""
    provider = (cfg.get("provider") or "claude_cli").strip()
    model = (cfg.get("model") or PROVIDER_DEFAULT_MODEL.get(provider, "")).strip()
    api_key = (cfg.get("api_key") or "").strip()

    if provider in LOCAL_CLI_BINARIES:
        binary, friendly = LOCAL_CLI_BINARIES[provider]
        return call_local_cli(prompt, binary, friendly, model)

    if not api_key:
        raise RuntimeError(
            f"الـ provider '{provider}' محتاج API key. حطه في الإعدادات."
        )

    if provider == "openai":
        return _call_openai_compat(prompt, api_key, model, "https://api.openai.com/v1", "OpenAI")
    if provider == "anthropic_api":
        return call_anthropic_api(prompt, api_key, model)
    if provider == "gemini":
        return _call_openai_compat(
            prompt, api_key, model,
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "Gemini",
        )
    if provider == "openrouter":
        return _call_openai_compat(prompt, api_key, model, "https://openrouter.ai/api/v1", "OpenRouter")

    raise RuntimeError(f"Provider غير معروف: {provider}")


def parse_claude_json(raw: str) -> dict:
    """Parse Claude's response as JSON, tolerating fences / extra text."""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = s.strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError as e:
                raise RuntimeError(f"رد الـ AI مش JSON صالح: {e}")
        raise RuntimeError("رد الـ AI مش JSON صالح.")


# ─────────────────────────────────────────────────────────────────────────────
# Obsidian markdown builder
# ─────────────────────────────────────────────────────────────────────────────

SEVERITY_BADGE = {
    "Critical": "🔴 Critical",
    "High": "🟠 High",
    "Medium": "🟡 Medium",
    "Low": "🟢 Low",
}


def embed_screenshots(text: str, image_map_by_index, attachments_prefix: str) -> str:
    """Replace [Screenshot N] tokens with Obsidian embeds.

    Uses bare-filename wiki-links (`![[file.webp]]`) instead of a full path
    so Obsidian resolves them regardless of the user's "New link format"
    setting (Shortest / Relative / Absolute). Filenames are unique per
    writeup because they already include the slug prefix.
    """

    def repl(m):
        idx = int(m.group(1))
        fname = image_map_by_index.get(idx)
        if fname:
            return f"\n\n![[{fname}]]\n\n"
        return m.group(0)

    return re.sub(r"\[Screenshot\s+(\d+)\]", repl, text)


def build_obsidian_md(data: dict, source_url: str, image_map_by_index, attachments_prefix: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    title = data.get("title", "Untitled Writeup")
    severity = data.get("severity", "Unknown")
    sev_badge = SEVERITY_BADGE.get(severity, severity)

    def esc_yaml(s):
        return str(s).replace('"', "'")

    lines = ["---"]
    lines.append(f'title: "{esc_yaml(title)}"')
    lines.append(f'vulnerability: {data.get("vulnerability_type", "Unknown")}')
    lines.append(f'severity: {severity}')
    lines.append(f'target: "{esc_yaml(data.get("target", "Unknown"))}"')
    lines.append(f'bounty: "{esc_yaml(data.get("bounty", "Not disclosed"))}"')
    lines.append(f'researcher: "{esc_yaml(data.get("researcher", "Unknown"))}"')
    lines.append(f'source: {source_url}')
    lines.append(f'date_added: {today}')

    tags = data.get("tags", []) or []
    if tags:
        lines.append("tags:")
        for t in tags:
            slug = re.sub(r"[^a-z0-9-]", "-", str(t).lower()).strip("-")
            if slug:
                lines.append(f"  - {slug}")

    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")

    lines.append(
        f"**{sev_badge}**  ·  `{data.get('vulnerability_type', '?')}`  ·  "
        f"🎯 {data.get('target', '?')}  ·  💰 {data.get('bounty', '—')}  ·  "
        f"👤 {data.get('researcher', '—')}"
    )
    lines.append("")

    tldr_ar = (data.get("tldr_ar") or "").strip()
    tldr_en = (data.get("tldr_en") or "").strip()
    if tldr_ar or tldr_en:
        lines.append("> [!summary] الخلاصة السريعة")
        if tldr_ar:
            for ln in tldr_ar.splitlines():
                lines.append(f"> {ln}")
        if tldr_en:
            lines.append(">")
            for ln in tldr_en.splitlines():
                lines.append(f"> *{ln}*")
        lines.append("")

    core = (data.get("core_idea_explained") or "").strip()
    if core:
        lines.append("## 💡 الفكرة الأساسية")
        lines.append("")
        lines.append(embed_screenshots(core, image_map_by_index, attachments_prefix))
        lines.append("")

    scenario = (data.get("attack_scenario") or "").strip()
    if scenario:
        lines.append("## 🎯 سيناريو الهجوم")
        lines.append("")
        lines.append(embed_screenshots(scenario, image_map_by_index, attachments_prefix))
        lines.append("")

    steps = data.get("attack_steps_concise") or []
    if steps:
        lines.append("### الخطوات باختصار")
        lines.append("")
        for i, step in enumerate(steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    payloads = data.get("payloads") or []
    if payloads:
        lines.append("## 🧪 الـ Payloads")
        lines.append("")
        for p in payloads:
            desc = (p.get("description") or "").strip()
            code = (p.get("code") or "").strip()
            lang = (p.get("language") or "").strip() or "text"
            if desc:
                lines.append(f"**{desc}**")
                lines.append("")
            if code:
                lines.append(f"```{lang}")
                lines.append(code)
                lines.append("```")
                lines.append("")

    rc = (data.get("root_cause_explained") or "").strip()
    if rc:
        lines.append("## 🔍 السبب الجذري")
        lines.append("")
        lines.append(embed_screenshots(rc, image_map_by_index, attachments_prefix))
        lines.append("")

    rem = (data.get("remediation") or "").strip()
    if rem:
        lines.append("## 🛡️ الإصلاح")
        lines.append("")
        lines.append(rem)
        lines.append("")

    lessons = data.get("lessons") or []
    if lessons:
        lines.append("## 📚 دروس مستفادة")
        lines.append("")
        for l in lessons:
            lines.append(f"- {l}")
        lines.append("")

    related = data.get("related_techniques") or []
    if related:
        lines.append("## 🔗 تقنيات مرتبطة")
        lines.append("")
        for t in related:
            slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(t)).strip("-")
            lines.append(f"- [[{slug}]]")
        lines.append("")

    if image_map_by_index:
        lines.append("## 📸 كل اللقطات")
        lines.append("")
        for idx in sorted(image_map_by_index.keys()):
            lines.append(f"**Screenshot {idx}**")
            lines.append(f"![[{image_map_by_index[idx]}]]")
            lines.append("")

    lines.append("## 🌐 المصدر")
    lines.append("")
    lines.append(f"[الـ Writeup الأصلي]({source_url})")
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config", methods=["GET", "POST"])
def config_route():
    if request.method == "POST":
        new_cfg = request.json or {}
        save_config(new_cfg)
        return jsonify(load_config())
    return jsonify(load_config())


@app.route("/api/analyze", methods=["POST"])
def analyze():
    payload = request.json or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL مطلوب"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    cfg = load_config()
    vault = Path(cfg["vault_path"]).expanduser()
    notes_dir = vault / cfg["notes_subdir"]
    attachments_root = vault / cfg["attachments_subdir"]

    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
        attachments_root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"مش قادر أعمل المجلدات: {e}"}), 500

    try:
        print(f"\n[1/5] 📥 Fetching: {url}")
        html = fetch_html(url)

        print(f"[2/5] 📄 Extracting content...")
        full_md = extract_markdown(html, url)
        if not full_md or len(full_md) < 200:
            return jsonify({"error": "مش قادر أستخرج محتوى من الصفحة دي."}), 500

        clean_md, image_urls = replace_images_with_placeholders(full_md)
        print(f"      Content: {len(clean_md)} chars  |  Images found: {len(image_urls)}")

        url_to_index = {u: i + 1 for i, u in enumerate(image_urls)}

        provider = cfg.get("provider", "claude_cli")
        print(f"[3/5] 🤖 Calling {provider} (this may take ~30-90s)...")
        prompt = ANALYSIS_PROMPT.replace("__CONTENT__", clean_md[:80000])
        raw = call_llm(prompt, cfg)
        try:
            data = parse_claude_json(raw)
        except RuntimeError as parse_err:
            # LLMs occasionally emit JSON with a missing comma or trailing text.
            # Give them one chance to fix it with the error context attached.
            print(f"      ⚠ JSON parse failed ({parse_err}); retrying once with fix instruction...")
            fix_prompt = (
                "ردك السابق كان JSON غير صالح. الخطأ:\n"
                f"{parse_err}\n\n"
                "ردك السابق كان:\n"
                f"{raw[:4000]}\n\n"
                "ارجع نفس المحتوى بنفس الـ schema بالظبط، بس JSON صالح 100% "
                "(فواصل صحيحة، quotes صحيحة، مفيش trailing commas). "
                "JSON object فقط، بدون أي كلام قبله أو بعده."
            )
            raw = call_llm(fix_prompt, cfg)
            data = parse_claude_json(raw)

        if data.get("title") == "NOT_A_WRITEUP":
            return jsonify({"error": "الصفحة دي مش writeup أمني. جرب لينك تاني."}), 400

        title = data.get("title", "writeup")
        slug = slugify(title)

        slug_attachments_dir = attachments_root / slug
        print(f"[4/5] 🖼️  Downloading {len(image_urls)} images → {slug_attachments_dir.name}/")
        image_map = download_images(image_urls, slug_attachments_dir, slug) if image_urls else {}

        index_to_fname = {url_to_index[u]: fname for u, fname in image_map.items()}
        attachments_prefix = f"{cfg['attachments_subdir']}/{slug}"

        print(f"[5/5] 📝 Building Obsidian markdown...")
        md_content = build_obsidian_md(data, url, index_to_fname, attachments_prefix)

        # Portable version: rewrite Obsidian ![[attachments/.../file]] embeds
        # to base64 data: URIs so a single .md file renders everywhere
        # (GitHub, VS Code, browser) without depending on the source CDN
        # (which may set CORP/hotlink headers that block cross-origin rendering).
        fname_to_datauri = {}
        for fname in image_map.values():
            fpath = slug_attachments_dir / fname
            if not fpath.is_file():
                continue
            ct, _ = mimetypes.guess_type(fname)
            if not ct:
                ct = "image/png"
            b64 = base64.b64encode(fpath.read_bytes()).decode("ascii")
            fname_to_datauri[fname] = f"data:{ct};base64,{b64}"

        def _to_portable(m):
            inner = m.group(1)
            fname = inner.split("/")[-1]
            uri = fname_to_datauri.get(fname)
            return f"![]({uri})" if uri else m.group(0)
        md_portable = re.sub(r"!\[\[([^\]]+)\]\]", _to_portable, md_content)

        note_path = notes_dir / f"{slug}.md"
        if note_path.exists():
            note_path = notes_dir / f"{slug}-{datetime.now().strftime('%H%M%S')}.md"
        note_path.write_text(md_content, encoding="utf-8")
        print(f"      ✓ Saved: {note_path}\n")

        return jsonify({
            "ok": True,
            "title": title,
            "slug": slug,
            "severity": data.get("severity"),
            "vulnerability_type": data.get("vulnerability_type"),
            "target": data.get("target"),
            "note_path": str(note_path),
            "attachments_dir": str(slug_attachments_dir) if image_map else None,            "image_count": len(image_map),
            "markdown": md_content,
            "markdown_portable": md_portable,
            "tags": data.get("tags", []),
        })

    except requests.RequestException as e:
        return jsonify({"error": f"فشل تحميل الصفحة: {e}"}), 500
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"خطأ غير متوقع: {e}"}), 500


@app.route("/api/check-provider", methods=["GET"])
def check_provider():
    """Verify the currently configured AI provider is reachable."""
    cfg = load_config()
    provider = cfg.get("provider", "claude_cli")
    api_key = (cfg.get("api_key") or "").strip()

    if provider in LOCAL_CLI_BINARIES:
        binary, friendly = LOCAL_CLI_BINARIES[provider]
        try:
            r = subprocess.run(
                [_cli_bin(binary), "--version"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                return jsonify({"ok": True, "provider": provider, "version": r.stdout.strip()})
            return jsonify({"ok": False, "provider": provider, "error": r.stderr.strip() or "unknown"}), 500
        except FileNotFoundError:
            return jsonify({"ok": False, "provider": provider, "error": f"{binary} CLI مش موجود في PATH"}), 500
        except Exception as e:
            return jsonify({"ok": False, "provider": provider, "error": str(e)}), 500

    # For HTTP providers, we just verify the API key is present.
    # A real validation call would cost a token; we skip that.
    if not api_key:
        return jsonify({
            "ok": False,
            "provider": provider,
            "error": f"محتاج API key للـ {provider}. حطه في الإعدادات.",
        }), 400

    return jsonify({"ok": True, "provider": provider, "version": f"{provider} (API key configured)"})


# Kept for backwards compatibility with any existing UI cache.
@app.route("/api/check-claude", methods=["GET"])
def check_claude_compat():
    return check_provider()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("┌──────────────────────────────────────────────────────────────┐")
    print("│  Writeup Agent — Local Obsidian writeup summarizer           │")
    print("│  Powered by Claude Code (Max subscription)                   │")
    print("├──────────────────────────────────────────────────────────────┤")
    print(f"│  Config:  {CONFIG_PATH}")
    print(f"│  Server:  http://localhost:5050                              │")
    print("└──────────────────────────────────────────────────────────────┘\n")
    app.run(host="127.0.0.1", port=5050, debug=False)
