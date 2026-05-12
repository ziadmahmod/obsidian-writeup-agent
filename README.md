# Bugbounty Writeups Agent 🎯

A local Flask app that turns any bug-bounty writeup URL into a structured **Obsidian note** with a deep analysis written in Arabic Egyptian — images included, organized, ready to study.

Use your existing AI subscription (Claude Max, Gemini free tier, Qwen, etc.) — **no API keys required** for the free-tier CLIs.

---

## ✨ Features

- 📥 Fetches and extracts any public writeup (Medium, freedium, PortSwigger, personal blogs)
- 🖼️ Handles lazy-loaded images (Medium `<picture>/srcset`, WordPress `data-src`)
- 🤖 Pluggable AI backend — switch between Claude, Gemini, Qwen, ChatGPT in one click
- 📝 Output is Obsidian-flavored markdown with YAML frontmatter, wiki-links, and embedded screenshots
- 📦 One-click portable export: a single `.md` with images base64-embedded that opens anywhere (GitHub, VS Code, browser)
- 🔍 Supports both vulnerability writeups and methodology articles
- 🔒 Everything runs locally — your data never leaves your machine

---

## 📋 Requirements

1. **Python 3.10+**
2. **One AI provider** (pick from the settings panel after install):
   - **Local CLIs** — no API key needed (free-tier login through the CLI):
     - [Claude Code](https://claude.com/product/claude-code) (needs Claude Max subscription)
     - [Gemini CLI](https://github.com/google-gemini/gemini-cli) (Google login, ~1500 free requests/day on Flash)
     - [Qwen Code CLI](https://github.com/QwenLM/qwen-code)
     - [OpenCode](https://opencode.ai) (open-source agent, works with any provider)
   - **HTTP APIs** — need an API key:
     - [OpenAI](https://platform.openai.com/api-keys) (ChatGPT)
     - [Anthropic](https://console.anthropic.com/)
     - [Google Gemini API](https://aistudio.google.com/app/apikey)
     - [OpenRouter](https://openrouter.ai/keys) (any model, one key)
3. **An Obsidian vault** on your machine (or any folder — Obsidian is recommended but not required)

---

## ⚡ Install

### 🪟 Windows

```powershell
git clone https://github.com/ziadmahmod/bugbounty-writeups-agent.git
cd bugbounty-writeups-agent

python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

start.bat
```

Open **http://localhost:5050** in your browser.

#### Auto-start with Windows (optional)

```powershell
$wsh = New-Object -ComObject WScript.Shell
$lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\writeup-agent.lnk"
$sc = $wsh.CreateShortcut($lnk)
$sc.TargetPath = "$PWD\launch_silent.vbs"
$sc.WorkingDirectory = "$PWD"
$sc.Save()
```

The server will now start silently in the background every time you log in.

### 🍎 macOS / 🐧 Linux

```bash
git clone https://github.com/ziadmahmod/bugbounty-writeups-agent.git
cd bugbounty-writeups-agent

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

chmod +x start.sh
./start.sh
```

Open **http://localhost:5050**.

---

## 🚀 Usage

1. **Open settings** (⚙ icon) and configure:
   - `Vault Path`: full path to your Obsidian vault (e.g. `D:\Obsidian\BugBounty` or `~/Documents/Vault`)
   - `Notes Subdirectory`: folder for notes inside the vault (e.g. `Writeups`)
   - `Attachments Subdirectory`: folder for images (e.g. `attachments`)
   - `AI Provider`: pick a CLI or HTTP API
   - `Model`: the model dropdown updates based on the provider you selected
2. Click **Save Settings**.
3. Paste a writeup URL in the input box.
4. Click **Analyze and save to Obsidian**. Takes ~30s–2min depending on the model.
5. The result includes:
   - Title, severity, vulnerability type, target, bounty
   - Saved note path on disk
   - Number of images downloaded
   - Buttons to preview / copy / download the markdown

---

## 🤖 Choosing a Provider

| Provider | API key? | Cost | Speed | Notes |
|---|---|---|---|---|
| **Gemini CLI** (Flash) | ❌ Google login | Free (~1500 req/day) | ~30s | Best default — fast, generous quota |
| **Gemini CLI** (Pro) | ❌ Google login | Free (~50 req/day) | ~30s | Higher quality, tight quota |
| **Claude CLI** | ❌ Max subscription | $20/month | ~90s | Highest quality output |
| **Qwen Code CLI** | ❌ Login required | Free tier limited | ~200s | Backup option |
| **OpenCode CLI** | ❌ (per-provider auth) | Depends on provider | varies | Use any model with one CLI — needs `opencode auth login` |
| **OpenAI** | ✅ | Pay-per-use | ~10–15s | Cheap with gpt-4o-mini |
| **Anthropic API** | ✅ | Pay-per-use | ~20s | Use Haiku for speed |
| **Gemini API** | ✅ | Free tier exists | ~10s | Separate quota from CLI |
| **OpenRouter** | ✅ | Pay-per-use | varies | One key for many models |

**Recommended setup**: install Gemini CLI, log in once, leave the model on **gemini-2.5-flash**. Free, fast, and the analysis is detailed enough for most writeups.

---

## 📁 Output Layout

```
<vault>/
├── Writeups/
│   └── account-takeover-via-reusable-tokens.md
└── attachments/
    └── account-takeover-via-reusable-tokens/
        ├── account-takeover-via-reusable-tokens-01.png
        ├── account-takeover-via-reusable-tokens-02.png
        └── ...
```

Each note contains:
- **YAML frontmatter** (vulnerability, severity, target, bounty, researcher, tags, source)
- **TL;DR** (Arabic + English)
- **💡 الفكرة الأساسية** — core insight, 3–5 paragraphs
- **🎯 سيناريو الهجوم** — full attack scenario, narrative style
- **🧪 الـ Payloads** — every payload explained
- **🔍 السبب الجذري** — root cause in the code/architecture
- **🛡️ الإصلاح** — concrete remediation steps
- **📚 دروس مستفادة** — lessons learned
- **🔗 تقنيات مرتبطة** — related techniques as Obsidian wiki-links
- **📸 كل اللقطات** — all screenshots embedded with `![[file.png]]`

---

## 🌍 Supported Sites

| Site type | Support |
|---|---|
| Static blogs (Hugo / Hexo / Jekyll / Ghost / WordPress) | ✅ Excellent |
| Medium | ✅ With lazy-loading fix |
| freedium-mirror.cfd | ✅ |
| PortSwigger Research | ✅ |
| Personal security blogs | ✅ |
| HackerOne public reports | ⚠️ Hit-or-miss |
| Sites behind Cloudflare WAF | ❌ Usually blocked |
| SPA blogs (Next.js client-rendered) | ❌ Content not in initial HTML |
| HackerOne private / paywalled | ❌ Needs auth |

---

## ⚙ Configuration

Settings live at `~/.writeup-agent/config.json` (on Windows: `C:\Users\<you>\.writeup-agent\config.json`).

You can edit it by hand if you prefer.

> ⚠️ **Security note**: If you use an HTTP API provider, your API key is stored as plain text in this file. The file is `.gitignore`d and never committed. Do not share it.

---

## 🔧 Troubleshooting

### "Provider unavailable" / "CLI not in PATH"
- For a CLI provider, make sure the binary is on `PATH`. Try `claude --version` / `gemini --version` / `qwen --version` in a terminal.
- **On Windows:** npm-installed CLIs land in `%APPDATA%\npm`. If `where claude` fails, add that folder to `PATH`.
- **On macOS / Linux (zsh):** if the CLI is installed via `npm i -g` or a user-local installer, it usually goes into `~/.local/bin`, which zsh doesn't see by default. Add it to your shell profile:
  ```bash
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
  ```
- **On Linux (bash):** same idea, different file:
  ```bash
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
  ```
- After fixing `PATH`, restart the writeup-agent server so the new env is picked up.

### Page fetch fails
- Some sites (private HackerOne, paywalled Medium) need auth — those won't work.
- Try `https://freedium-mirror.cfd/<medium-url>` as a workaround for paywalled Medium posts.
- Cloudflare WAF can block `requests`-based fetches.

### Images don't appear in Obsidian
- The notes use bare-filename embeds (`![[image.png]]`) so any Obsidian link-format setting works.
- Make sure your vault root in settings matches the folder you opened in Obsidian.

### Page returned empty / "not a writeup"
- The site likely requires JavaScript to render content — not supported.
- Save the page from the browser's Reader View as HTML, then host it locally and re-analyze.

### `ImportError: lxml.html.clean module is now a separate project`
This happens with `lxml >= 5.2` (which dropped `lxml.html.clean`) plus `trafilatura`'s `justext` dependency that still imports it. The pinned `requirements.txt` already accounts for this — make sure your install is up to date:
```bash
pip install -r requirements.txt --upgrade
```
If you still see it, install the split module directly:
```bash
pip install lxml_html_clean
```

### Server crashes on banner print (Windows)
- `start.bat` sets `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` automatically. Use it instead of running `python app.py` directly.

### Gemini CLI keeps retrying for minutes
- Your `gemini-2.5-pro` daily quota is exhausted (free tier is ~50 req/day). Switch to `gemini-2.5-flash` in the model dropdown.

### LLM returns invalid JSON
- The agent automatically retries once with the parse error attached. If it still fails, try a different model or click Analyze again.

---

## 🛠️ Customization

- **Change the analysis language/tone** → edit `ANALYSIS_PROMPT` in `app.py`
- **Change the sections in the note** → edit `build_obsidian_md()` in `app.py`
- **Change the tags style** → same function

---

## 🔐 Privacy

- The server binds to `127.0.0.1` only — nobody on your network can reach it.
- The vault, attachments, and config all stay on your machine.
- If you use a local CLI (Claude / Gemini / Qwen), traffic goes through that CLI's own auth.
- If you use an HTTP API (OpenAI / Anthropic / Gemini API / OpenRouter), the writeup content is sent to that provider — check their privacy policy.
- Public page contents are fetched with plain HTTP requests (no JS execution).

---

## 🤝 Contributing

Issues and PRs welcome. If you find a site or lazy-loading pattern that doesn't work, open an issue with the URL.

---

## 📄 License

[MIT](LICENSE)
