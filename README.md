# DropAlert

**Free game drops · email alerts · optional auto-claim**

Tracks free and free-to-play titles on Epic, Steam, GOG, and more. Emails you when new drops appear and can claim games into your libraries with browser automation.

> Built by [Veera](https://github.com/veera-1175).

## Stack

- **Core:** Python 3.11+, SQLite, APScheduler
- **Fetch:** Epic / Steam / GamerPower APIs
- **Claim:** Playwright (Chrome profiles)
- **UI:** Flask dashboard

## Quick start

```powershell
git clone https://github.com/veera-1175/DropAlert.git
cd DropAlert

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

copy .env.example .env
# set SMTP_USER / SMTP_PASSWORD (Gmail app password)
```

**One-shot check + email**

```powershell
python hunter.py --once
```

**Dashboard**

```powershell
python web.py
```

Open http://127.0.0.1:5050

**First-time store login**

```powershell
python hunter.py --login-only
```

Browser profiles: `%LOCALAPPDATA%\DropAlert\`

## Layout

```
DropAlert/
├── hunter.py      fetch · DB · email · CLI
├── claim.py       Epic / Steam / GOG claim
├── web.py         Flask UI
├── .env.example
├── requirements.txt
└── .github/workflows/daily.yml
```

## Features

| Capability | Detail |
|------------|--------|
| Free / F2P / upcoming | Epic + Steam + GamerPower |
| Email digest | SMTP |
| Auto-claim | Optional Playwright |
| Dashboard | Filter, select, claim history |
| Cloud mode | GitHub Actions daily fetch |

## GitHub Actions (daily email)

Workflow: `.github/workflows/daily.yml` (06:00 UTC).

Add these **repository secrets** so the schedule can email you:

| Secret | Example |
|--------|---------|
| `SMTP_HOST` | `smtp.gmail.com` (optional; defaults) |
| `SMTP_PORT` | `587` (optional; defaults) |
| `SMTP_USER` | your Gmail |
| `SMTP_PASSWORD` | Gmail app password |
| `EMAIL_TO` | recipient address |

Without SMTP user/password the job still **fetches games** and stays green; it only skips email.

## License

MIT
