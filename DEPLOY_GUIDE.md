# Deployment guide — putting the HMH Labz WhatsApp agent platform on a VPS

A step-by-step guide for someone who has **never set up a server before**. Follow it top to
bottom, in order, without skipping. Every command is meant to be copied and pasted exactly.

> **Domain.** This guide uses the platform's domain, `heyozo.com` (the value in `.env.example`).
> If you ever deploy under a different domain, replace `heyozo.com` with yours **everywhere**
> you see it.
>
> **Time needed:** about half a day for the server, plus waiting time for Meta (template
> approval, app review) which can take 1–3 days. Do the Meta paperwork (Part 1, item 4) first.

---

## Contents

- [How to read this guide](#how-to-read-this-guide)
- [The big picture (what we are building)](#the-big-picture-what-we-are-building)
- [Part 1 — Accounts and information to collect first](#part-1--accounts-and-information-to-collect-first)
- [Part 2 — Prepare your Mac](#part-2--prepare-your-mac)
- [Part 3 — Buy and start the VPS](#part-3--buy-and-start-the-vps)
- [Part 4 — Point the domain at the server (Cloudflare DNS)](#part-4--point-the-domain-at-the-server-cloudflare-dns)
- [Part 5 — Lock the server down (security)](#part-5--lock-the-server-down-security)
- [Part 6 — Install Docker](#part-6--install-docker)
- [Part 7 — Download the platform code](#part-7--download-the-platform-code)
- [Part 8 — Fill in the settings file (`.env`)](#part-8--fill-in-the-settings-file-env)
- [Part 9 — Start everything for the first time](#part-9--start-everything-for-the-first-time)
- [Part 10 — Check that it works](#part-10--check-that-it-works)
- [Part 11 — Create your HMH Labz console login](#part-11--create-your-hmh-labz-console-login)
- [Part 12 — Connect WhatsApp (Meta)](#part-12--connect-whatsapp-meta)
- [Part 13 — Add the first client (Aquamena)](#part-13--add-the-first-client-aquamena)
- [Part 14 — Backups: switch on and test](#part-14--backups-switch-on-and-test)
- [Part 15 — Alerts to your WhatsApp](#part-15--alerts-to-your-whatsapp)
- [Part 16 — Status page and outside uptime check](#part-16--status-page-and-outside-uptime-check)
- [Part 17 — Final go-live checklist](#part-17--final-go-live-checklist)
- [Part 18 — Everyday operations](#part-18--everyday-operations)
- [Part 19 — When something goes wrong](#part-19--when-something-goes-wrong)
- [Appendix A — Running on the AWS free tier instead of Hostinger](#appendix-a--running-on-the-aws-free-tier-instead-of-hostinger)
- [Glossary](#glossary)

---

## How to read this guide

- **Grey boxes are commands.** Copy the whole box and paste it into the Terminal window, then
  press **Enter**.
- **Words in CAPITALS inside a command** (like `YOUR_SERVER_IP`) are placeholders. Replace them
  with your real value before pressing Enter. Everything else stays exactly as written.
- **Where to run it.** Each command box says where it runs:
  - 💻 **Mac** — the Terminal app on your own computer.
  - 🖥️ **Server** — a Terminal window that is logged in to the VPS (you will see a prompt like
    `deploy@hmh-agents:~$`).
- **Passwords never go on the command line.** When the guide says "type it at the prompt",
  the screen will show nothing while you type. That is normal. Type, then press Enter.
- **Keep a password manager open** (1Password, Bitwarden…). You will create many secrets.
  Every time this guide says **"Save in password manager"**, do it right away.
- If a command prints the word **error** in red and you don't know why, **stop**. Look in
  [Part 19](#part-19--when-something-goes-wrong) before you continue.

---

## The big picture (what we are building)

One rented server (a "VPS") runs everything, inside **Docker containers** (sealed boxes that
each run one program):

| Box (container) | What it does in plain words |
|---|---|
| **caddy** | The front door. Gets the free HTTPS padlock certificates and sends each visitor to the right box. The only box the internet can reach. |
| **api** | Receives WhatsApp messages from Meta and serves the dashboards' data. |
| **worker** | Thinks of the AI reply and sends it to WhatsApp. |
| **scheduler** | The alarm clock: runs nightly backups, campaign sending, alert checks, and replays any message that got stuck. |
| **postgres** | The database: every client, customer, message and order. |
| **redis** | A fast to-do list between the api and the worker. |
| **uptime-kuma** | A small status page showing whether everything is up. |

The web addresses you will end up with:

| Address | Who uses it |
|---|---|
| `https://api.heyozo.com` | Meta sends WhatsApp messages here. Nobody browses it. |
| `https://admin.heyozo.com` | **HMH Labz console** — our staff only, with 2-step login. |
| `https://aquamena.heyozo.com` | **Aquamena's dashboard** (each client gets `theirname.heyozo.com`). |
| `https://go.heyozo.com/q/…` | The page customers land on after scanning a printed QR code. |
| `https://status.heyozo.com` | Status page, HMH Labz staff only. |

---

## Part 1 — Accounts and information to collect first

Get **all** of these before you touch the server. Tick each one.

| # | What | Where | What you need from it |
|---|---|---|---|
| 1 | **VPS** — Hostinger **KVM 4** (4 CPU, 16 GB RAM) or bigger. *Starting on the AWS free tier instead? Use [Appendix A](#appendix-a--running-on-the-aws-free-tier-instead-of-hostinger) alongside this guide.* | hostinger.com | You buy it in Part 3. |
| 2 | **The domain** (e.g. `heyozo.com`) with its DNS managed by **Cloudflare** (free plan is fine) | cloudflare.com | Login to Cloudflare. If the domain was bought elsewhere, add it to Cloudflare and change the nameservers at the registrar to the two Cloudflare gives you; wait until Cloudflare says "Active". |
| 3 | **GitHub** access to `xtuae/AI-Agent-Platform` as an **admin** of the repo | github.com | Needed to add a "deploy key" in Part 7. |
| 4 | **Meta Business** account for HMH Labz, verified, with a **Meta App** (type *Business*) that has the **WhatsApp** product added | business.facebook.com, developers.facebook.com | App ID, **App secret**, and for each client: **WhatsApp Business Account ID (WABA ID)**, **Phone number ID**, and a **System-user access token**. Details in Part 12. |
| 5 | **Google AI Studio** API key (paid, billing enabled) | aistudio.google.com | `GOOGLE_AI_API_KEY`. Set a monthly spend limit in Google Cloud billing. |
| 6 | **OpenRouter** API key (the backup AI provider) | openrouter.ai | `OPENROUTER_API_KEY`. Add some credit and set a limit. |
| 7 | **Cloudflare R2** bucket for backups (or Backblaze B2) | Cloudflare → R2 | Bucket name, endpoint URL, access key ID, secret access key. Steps in Part 14. |
| 8 | An **authenticator app** on your phone (Google Authenticator, Authy, 1Password…) | App Store | For the console's 2-step login. |
| 9 | **Password manager** | — | To store every secret this guide creates. |
| 10 | Aquamena's **catalog file** (`aquamena_catalog.json`, products and coupon prices **confirmed by the client**) and their **customer spreadsheet** (`.xlsx`) | From the client | Used in Part 13. Prices are never typed into code. |

---

## Part 2 — Prepare your Mac

### 2.1 Open Terminal

Press **⌘ + Space**, type **Terminal**, press Enter. A window with a text prompt opens. This is
where all 💻 Mac commands go.

### 2.2 Make your SSH key (your "digital house key" for the server)

Check whether you already have one:

💻 Mac
```bash
ls ~/.ssh/id_ed25519.pub
```

- If it prints a file name → you already have a key. Go to 2.3.
- If it says "No such file" → make one:

💻 Mac
```bash
ssh-keygen -t ed25519 -C "haja@hmhlabz.com"
```

Press **Enter** to accept the default location. When asked for a passphrase, type one (you will
not see it), press Enter, type it again. **Save the passphrase in your password manager.**

### 2.3 Copy your public key (you will paste it into Hostinger)

💻 Mac
```bash
pbcopy < ~/.ssh/id_ed25519.pub
```

It is now on your clipboard. The **public** key (`.pub`) is safe to share. The other file
(`id_ed25519`, no `.pub`) is private: never send it to anyone.

### 2.4 Make the backup encryption key (on your Mac, NOT on the server)

Backups are locked with a key whose **private half never touches the server**. So even if
someone broke into the server or the backup bucket, they could not read the backups.

💻 Mac
```bash
cd /Volumes/Mac-HMH/Desktop/AGENT
uv run python -m api.scripts.backup keygen --out ~/backup-age-key.txt
```

It prints a line starting with **`age1…`** — that is the **public key**. Copy it into your
password manager as **"BACKUP_AGE_RECIPIENT"**. You will paste it into the server settings in
Part 8.

Now protect the private key file `~/backup-age-key.txt`:

1. Open it (`open -e ~/backup-age-key.txt`), copy its whole content into your password manager
   as **"Backup private key — HMH agents"**.
2. Put **one more copy offline** (a USB stick kept in the office safe, or printed on paper).
3. Then delete it from the Mac:

💻 Mac
```bash
rm -P ~/backup-age-key.txt
```

> ⚠️ **If this private key is lost, no backup can ever be opened.** That is why there are two copies.

---

## Part 3 — Buy and start the VPS

> **On AWS?** Do [Appendix A.2](#appendix-a--running-on-the-aws-free-tier-instead-of-hostinger) instead of this part.

1. In **Hostinger** → **VPS** → buy **KVM 4** (or bigger). Choose a data-centre close to the
   UAE (e.g. a nearby region from the list).
2. When asked for the operating system choose **Plain OS → Ubuntu 24.04 LTS** (not a "panel"
   or "application" template).
3. Set a strong **root password** → **Save in password manager** ("VPS root password").
4. If it offers **"Add SSH key"**, paste the key from step 2.3 (⌘ + V).
5. Finish. Wait until hPanel shows the VPS as **Running**.
6. On the VPS overview page, copy the **IPv4 address** (four numbers like `203.0.113.25`).
   **Save it** as "VPS IP". Below it is written as `YOUR_SERVER_IP`.
7. Turn on **Hostinger's automatic weekly backups/snapshots** if offered. (These are extra
   protection only. Our real backups are in Part 14.)

> **Emergency door:** hPanel has a **Browser terminal** button on the VPS page. If you ever lock
> yourself out over SSH, that button still lets you in.

---

## Part 4 — Point the domain at the server (Cloudflare DNS)

### 4.1 Add the DNS records

Cloudflare → choose your domain → **DNS** → **Records** → **Add record**. Add these **five**
records. For every one: **Type `A`**, **IPv4 address = `YOUR_SERVER_IP`**, and switch the
**orange cloud OFF so it is grey ("DNS only")**.

| Type | Name | Content | Proxy status |
|---|---|---|---|
| A | `api` | YOUR_SERVER_IP | **DNS only (grey)** |
| A | `admin` | YOUR_SERVER_IP | **DNS only (grey)** |
| A | `go` | YOUR_SERVER_IP | **DNS only (grey)** |
| A | `status` | YOUR_SERVER_IP | **DNS only (grey)** |
| A | `*` | YOUR_SERVER_IP | **DNS only (grey)** |

The `*` record ("wildcard") covers every client's dashboard (`aquamena.`, future clients…), so
you never need to touch DNS again when adding a client.

> **Why grey and not orange?** The server gets its own certificates, and its login protection
> needs to see each visitor's real address. Orange would hide that.

### 4.2 Make a Cloudflare API token (lets the server prove it owns the domain)

1. Cloudflare → click your profile icon (top right) → **My Profile** → **API Tokens** →
   **Create Token**.
2. Choose the template **"Edit zone DNS"** → **Use template**.
3. **Permissions:** keep *Zone · DNS · Edit*, then **+ Add more** → *Zone · Zone · Read*
   (Caddy's Cloudflare plugin needs both).
4. **Zone Resources:** *Include → Specific zone → heyozo.com*.
5. **Continue to summary** → **Create Token**.
6. Copy the token (it is shown **once**, about 40 characters) → **Save in password manager** as
   "CLOUDFLARE_API_TOKEN".

### 4.3 Check the DNS works

Wait 5 minutes, then:

💻 Mac
```bash
dig +short api.heyozo.com
dig +short test123.heyozo.com
```

Both must print `YOUR_SERVER_IP`. If they print nothing, wait a little longer and try again.

---

## Part 5 — Lock the server down (security)

### 5.1 First login, as root

💻 Mac
```bash
ssh root@YOUR_SERVER_IP
```

- The first time it asks *"Are you sure you want to continue connecting?"* → type `yes`, Enter.
- If it asks for a password, use the root password from Part 3.

You are now on the server. The prompt changes to something like `root@srv123:~#`.

### 5.2 Update the system

🖥️ Server (as root)
```bash
apt update && apt -y upgrade
```

This can take a few minutes. If a pink/purple screen asks about a configuration file, press
**Enter** to keep the default.

### 5.3 Create the `deploy` user (we never work as root day to day)

🖥️ Server (as root)
```bash
adduser --gecos "" deploy
```

Type a password for `deploy` twice → **Save in password manager** ("VPS deploy user password").
You will need it for `sudo` commands.

Give it admin rights and your SSH key:

🖥️ Server (as root)
```bash
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys 2>/dev/null || true
chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys 2>/dev/null; ls -l /home/deploy/.ssh
```

If the last line shows **no** `authorized_keys` file (you didn't add the key in Hostinger), run
this on your **Mac** in a **new** Terminal window (⌘ + N) and enter the deploy password:

💻 Mac
```bash
ssh-copy-id deploy@YOUR_SERVER_IP
```

### 5.4 Firewall (UFW) — only web traffic and our SSH port get in

🖥️ Server (as root)
```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 2222/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable
ufw status
```

(Port 22 stays open for a few more minutes only, so you can't lock yourself out. We close it in 5.6.)

### 5.5 Harden SSH: key-only, no root login, port 2222

🖥️ Server (as root)
```bash
cat > /etc/ssh/sshd_config.d/00-hmh.conf <<'EOF'
Port 2222
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sshd -t && echo "SSH CONFIG OK"
```

It must print **SSH CONFIG OK**. (The file is named `00-…` on purpose: Ubuntu reads these files
in order and the **first** setting wins, so ours beats Hostinger's own `50-cloud-init.conf`.)

Apply it:

🖥️ Server (as root)
```bash
systemctl daemon-reload
systemctl restart ssh.socket 2>/dev/null; systemctl restart ssh
ss -tlnp | grep 2222
```

The last line must show something listening on `:2222`.

### 5.6 TEST the new login before closing anything

**Leave the current window open.** Open a **new** Terminal window on your Mac (⌘ + N):

💻 Mac (new window)
```bash
ssh -p 2222 deploy@YOUR_SERVER_IP
```

- ✅ You get a `deploy@…$` prompt (maybe after your SSH key passphrase) → it works.
- ❌ It fails → go back to the old window (still logged in as root) and check 5.3–5.5. If both
  windows are closed, use hPanel's **Browser terminal**.

When it works, close port 22 (in the **deploy** window):

🖥️ Server
```bash
sudo ufw delete allow 22/tcp
sudo ufw status
```

Now `exit` the old root window. From now on you **always** log in as `deploy` on port 2222.

### 5.7 Make logging in easy

On your Mac, save a shortcut so you can just type `ssh hmh`:

💻 Mac
```bash
cat >> ~/.ssh/config <<'EOF'

Host hmh
  HostName YOUR_SERVER_IP
  User deploy
  Port 2222
  IdentityFile ~/.ssh/id_ed25519
EOF
```

(Open `~/.ssh/config` with `open -e ~/.ssh/config` and replace `YOUR_SERVER_IP` with the real
IP.) From now on: 💻 `ssh hmh`

### 5.8 Block password-guessers (fail2ban) and turn on automatic security updates

🖥️ Server
```bash
sudo apt install -y fail2ban python3-systemd unattended-upgrades
sudo tee /etc/fail2ban/jail.d/sshd.local >/dev/null <<'EOF'
[sshd]
enabled = true
port = 2222
backend = systemd
maxretry = 5
bantime = 1h
EOF
sudo systemctl enable --now fail2ban
sudo systemctl restart fail2ban
sudo fail2ban-client status sshd
sudo dpkg-reconfigure -f noninteractive -plow unattended-upgrades
```

`fail2ban-client status sshd` should show the jail as active.

### 5.9 Swap memory and network tuning

Swap is "emergency memory" on disk. It stops the server crashing during a big spreadsheet
import.

🖥️ Server
```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo tee /etc/sysctl.d/99-agents.conf >/dev/null <<'EOF'
vm.swappiness=10
net.core.somaxconn=4096
net.ipv4.tcp_max_syn_backlog=4096
fs.file-max=200000
EOF
sudo sysctl --system >/dev/null && free -h
```

`free -h` should show a **Swap** line of about 4.0Gi.

Keep the server's clock on **UTC** (the default). The nightly backup runs at 01:30 UTC =
05:30 UAE time.

---

## Part 6 — Install Docker

🖥️ Server
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy
```

**Log out and back in** so the new permission takes effect:

🖥️ Server
```bash
exit
```

💻 Mac
```bash
ssh hmh
```

Check:

🖥️ Server
```bash
docker version --format 'Docker {{.Server.Version}}' && docker compose version
```

Both lines must print a version (Docker 27+ and Compose v2).

> Docker is allowed to open ports even when the firewall says no. That's fine here: in our
> setup **only the front door (Caddy)** opens ports, 80 and 443. The database and Redis are never
> reachable from the internet.

---

## Part 7 — Download the platform code

The repository is private, so the server gets its own **read-only key** for it.

### 7.1 Make the server's GitHub key

🖥️ Server
```bash
ssh-keygen -t ed25519 -C "hmh-vps deploy key" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

Copy the line it prints (starts with `ssh-ed25519`).

### 7.2 Add it to GitHub

1. Open **github.com/xtuae/AI-Agent-Platform** → **Settings** → **Deploy keys** → **Add deploy key**.
2. Title: `hmh-vps`. Key: paste. **Leave "Allow write access" UNTICKED** (read-only).
3. **Add key**.

### 7.3 Tell the server to use that key for GitHub, then download

🖥️ Server
```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com
```

Answer `yes` to the fingerprint question. It should say *"Hi xtuae/AI-Agent-Platform! You've
successfully authenticated…"*. (It also says it doesn't provide shell access. That's normal.)

🖥️ Server
```bash
sudo mkdir -p /opt/agents && sudo chown deploy:deploy /opt/agents
git clone git@github.com:xtuae/AI-Agent-Platform.git /opt/agents
cd /opt/agents && git log -1 --oneline
```

### 7.4 One very important shortcut: `dc`

The repo contains a file for **developers' laptops** (`docker-compose.override.yml`). Plain
`docker compose` would load it **and run the server in development mode**. On the server we
must always say `-f docker-compose.yml`. To make this impossible to forget:

🖥️ Server
```bash
echo "alias dc='docker compose -f /opt/agents/docker-compose.yml --project-directory /opt/agents'" >> ~/.bashrc
echo "cd /opt/agents" >> ~/.bashrc
source ~/.bashrc
```

> ⚠️ **On the server, always type `dc …`, never `docker compose …`.** All commands below use `dc`.

---

## Part 8 — Fill in the settings file (`.env`)

The `.env` file holds every password and key. It lives **only** on the server (and in your
password manager). It is never put in GitHub.

### 8.1 Make the file

🖥️ Server
```bash
cd /opt/agents
cp .env.example .env
chmod 600 .env
```

### 8.2 Generate the secrets the server makes for itself

Run this. It prints ready-made lines. **Copy the whole output into your password manager**
(one secure note called "HMH agents .env secrets"):

🖥️ Server
```bash
echo "POSTGRES_SUPERUSER_PASSWORD=$(openssl rand -hex 24)"
echo "DB_OWNER_PASSWORD=$(openssl rand -hex 24)"
echo "DB_APP_PASSWORD=$(openssl rand -hex 24)"
echo "DB_BACKUP_PASSWORD=$(openssl rand -hex 24)"
echo "APP_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')"
echo "JWT_SECRET=$(openssl rand -hex 48)"
echo "META_VERIFY_TOKEN=$(openssl rand -hex 20)"
```

> ⚠️ **`APP_ENCRYPTION_KEY` is critical.** It unlocks every client's stored WhatsApp token. If
> it is lost, every client must be re-connected. Keep it in the password manager **separately
> from** the backups. Never change it once clients are connected.

### 8.3 Make the status-page password

🖥️ Server
```bash
docker run --rm -it caddy:2.10-alpine caddy hash-password
```

Type a new password for the status page twice → **save the password** in your password
manager ("Status page — user hmh"). It prints a long line starting with `$2a$…`. That is the
**hash**. Copy it.

### 8.4 Open the file in the editor

🖥️ Server
```bash
nano .env
```

How to use **nano**: move with the arrow keys; delete with Backspace; paste with **⌘ + V**
(Mac Terminal). Save: **Ctrl + O**, then Enter. Exit: **Ctrl + X**.

Change these lines. Leave every line not listed here as it is.

| Line in `.env` | Put this | Notes |
|---|---|---|
| `APP_ENV=` | `production` | **Must** be production. |
| `BASE_DOMAIN=` | `heyozo.com` | Your domain, no `https://`, no `www`. |
| `ACME_EMAIL=` | `ops@hmhlabz.com` (or any mailbox you read) | Let's Encrypt emails here if a certificate has a problem. |
| `CLOUDFLARE_API_TOKEN=` | token from Part 4.2 | |
| `POSTGRES_SUPERUSER_PASSWORD=` | from 8.2 | |
| `DB_OWNER_PASSWORD=` | from 8.2 | |
| `DB_APP_PASSWORD=` | from 8.2 | |
| `APP_ENCRYPTION_KEY=` | from 8.2 | |
| `JWT_SECRET=` | from 8.2 | |
| `OPTIN_BASE_URL=` | `https://go.heyozo.com` | Printed QR codes point here — decide it **before** printing any. |
| `META_APP_SECRET=` | Meta App secret (Part 12.1) | You can leave it empty now and fill in at Part 12. |
| `META_VERIFY_TOKEN=` | from 8.2 | You will paste the same value into Meta in Part 12. |
| `GOOGLE_AI_API_KEY=` | your Google AI key | |
| `OPENROUTER_API_KEY=` | your OpenRouter key | |
| `LLM_PRICES_USD_PER_MTOK=` | keep, and **add** the price of the model you use from Google's price list, e.g. `{"gemini-3.5-flash-lite": ["0.30", "2.50"]}` (gemini-3.5-flash has no published price yet) | Without it, AI cost shows as 0 in the dashboards. |
| `DB_BACKUP_PASSWORD=` | from 8.2 | |
| `BACKUP_AGE_RECIPIENT=` | the `age1…` public key from Part 2.4 | |
| `BACKUP_BUCKET=` … `BACKUP_SECRET_ACCESS_KEY=` | from Part 14.1 | Can stay empty until Part 14. |
| `ALERT_TENANT_SLUG=` / `ALERT_TO=` | leave empty for now | Part 15. |
| `STATUS_BASIC_AUTH_HASH=` | the `$2a$…` hash from 8.3 **inside single quotes**: `STATUS_BASIC_AUTH_HASH='$2a$14$abc…'` | ⚠️ The single quotes are required. Without them the `$` signs get eaten and the status page login breaks. |

Save (**Ctrl + O**, Enter) and exit (**Ctrl + X**).

### 8.5 Check you didn't miss one

🖥️ Server
```bash
grep -nE '^(APP_ENV|BASE_DOMAIN|ACME_EMAIL|CLOUDFLARE_API_TOKEN|POSTGRES_SUPERUSER_PASSWORD|DB_OWNER_PASSWORD|DB_APP_PASSWORD|DB_BACKUP_PASSWORD|APP_ENCRYPTION_KEY|JWT_SECRET|META_VERIFY_TOKEN|GOOGLE_AI_API_KEY|OPENROUTER_API_KEY|STATUS_BASIC_AUTH_HASH|OPTIN_BASE_URL)=$' .env || echo "ALL REQUIRED VALUES FILLED"
grep -n '^APP_ENV=' .env
```

- It must print **ALL REQUIRED VALUES FILLED**. Any line it prints instead is still empty.
- The second line must show `APP_ENV=production`.

> **Never** paste the `.env` file into chat, email, Slack or a ticket. If you need help, share
> the **names** of the settings, not the values.

---

## Part 9 — Start everything for the first time

### 9.1 Start the database and Redis first

(The deploy script expects the database to already be running. On a brand-new server it isn't
yet, so we start it by hand once.)

🖥️ Server
```bash
dc up -d postgres redis
sleep 20
dc ps
```

Both `postgres` and `redis` should show **(healthy)**. If postgres says "starting", wait 20
seconds and run `dc ps` again.

Check the database created its users (this happens only on the very first start):

🖥️ Server
```bash
dc logs postgres | grep "roles ready"
```

It should print `roles ready: owner=agents_owner app=agents_app backup=agents_backup db=agents`.

### 9.2 Run the deploy script

This builds everything (first time: **10–20 minutes**; it downloads the AI search model and
builds the dashboard), sets up the database tables, and starts all boxes.

🖥️ Server
```bash
cd /opt/agents
chmod +x deploy/deploy.sh
deploy/deploy.sh
```

It prints steps beginning with `==`. It's fine if it says
`!! BACKUP_BUCKET not set … deploying WITHOUT a pre-deploy backup`; we set up backups in Part 14.

At the end you want to see:

```
   healthy: e571112
SERVICE       IMAGE                      STATUS
api           hmh-agents-api:e571112     Up … (healthy)
caddy         …                          Up …
…
```

If it ends with `!! /health did not come up within 60 s`, see [Part 19](#part-19--when-something-goes-wrong).

### 9.3 Wait for the certificates

The front door (Caddy) now asks Let's Encrypt for HTTPS certificates. This takes **1–3 minutes**.
Watch it:

🖥️ Server
```bash
dc logs -f caddy | grep -iE "certificate obtained|error"
```

You want to see `certificate obtained successfully` for `api.`, `go.`, `status.` and
`*.heyozo.com`. Press **Ctrl + C** to stop watching.

---

## Part 10 — Check that it works

💻 Mac
```bash
curl -s https://api.heyozo.com/health; echo
```

Should print something like `{"status":"ok", … "version": … }`.

Then in your browser:

| Open | You should see |
|---|---|
| `https://admin.heyozo.com` | The HMH Labz logo and a **console sign-in** page, with a padlock 🔒 in the address bar. |
| `https://aquamena.heyozo.com` | A dashboard **sign-in** page (login works after Part 13). |
| `https://status.heyozo.com` | A username/password box → `hmh` + the status password from 8.3 → Uptime Kuma setup page. |
| `https://go.heyozo.com/q/test` | A plain "not found" page (correct — no QR code exists yet). |

All four good? The platform is running. 🎉 Now we connect it to the real world.

---

## Part 11 — Create your HMH Labz console login

🖥️ Server
```bash
dc run --rm api python -m api.scripts.platform_users add --email haja@hmhlabz.com --role owner --name "Haja"
```

1. It asks for a **password** (twice). Choose a strong one → **save in password manager**.
2. It shows a **QR code** in the terminal. Open your **authenticator app** → **Add** → **Scan QR
   code** → scan it. (If the QR doesn't scan, it also prints an `otpauth://` link. Copy that into
   the authenticator instead.)
3. The QR code is shown **only once**. If you lose your phone, someone with the owner role runs
   `… platform_users reset-totp --email …`.

Sign in at `https://admin.heyozo.com` with email + password + the 6-digit code.

Add your colleagues the same way. Roles:

| Role | Can do |
|---|---|
| `owner` | Everything, including recording reimbursements. |
| `ops` | Everything except reimbursements; can pull Meta statements. |
| `support` | Look only. |

Other commands: `… platform_users list`, `… password --email …`, `… disable --email …`
(disabling takes effect immediately).

---

## Part 12 — Connect WhatsApp (Meta)

### 12.1 Get the App secret

developers.facebook.com → **My Apps** → the HMH Labz app → **App settings** → **Basic** →
**App secret** → **Show** → copy.

Put it in the server's `.env`:

🖥️ Server
```bash
nano /opt/agents/.env
```

Fill in `META_APP_SECRET=`, save, exit. Then restart so it's picked up:

🖥️ Server
```bash
dc up -d api worker scheduler
```

### 12.2 Tell Meta where to send messages (the webhook)

In the Meta App dashboard → **WhatsApp** → **Configuration** → **Webhook** → **Edit**:

| Field | Value |
|---|---|
| **Callback URL** | `https://api.heyozo.com/webhook/meta` |
| **Verify token** | the `META_VERIFY_TOKEN` value from Part 8.2 (exactly the same) |

Click **Verify and save**. A green tick means the server answered correctly.

Then under **Webhook fields**, click **Subscribe** on these four:

- `messages`
- `message_template_status_update`
- `phone_number_quality_update`
- `account_update`

### 12.3 Make the app "Live"

At the top of the Meta App dashboard, switch **App Mode** from *Development* to **Live**. Meta
asks for a **Privacy Policy URL** first (App settings → Basic). Use HMH Labz's privacy policy
page. In Development mode, real customer messages are **not** delivered to the server.

### 12.4 Make a permanent access token (System user)

business.facebook.com → **Settings** (Business settings) → **Users** → **System users**:

1. **Add** → name `hmh-agents-platform`, role **Admin** → Create.
2. **Assign assets** → **Apps** → the HMH Labz app → **Full control**.
   Then **WhatsApp accounts** → the client's WhatsApp account → **Full control**.
3. **Generate new token** → choose the app → expiry **Never** → tick these permissions:
   `whatsapp_business_messaging`, `whatsapp_business_management` (and `business_management`
   if offered) → **Generate**.
4. Copy the token **now** (shown once) → **save in password manager** as
   "Meta system-user token — Aquamena".

> Each client's WhatsApp account must be assigned to this system user. With Embedded Signup the
> client shares their WhatsApp account with HMH Labz and you assign it here.

### 12.5 Note the client's two IDs

Meta App dashboard → **WhatsApp** → **API Setup** (or WhatsApp Manager → Phone numbers), select
the client's number, and note:

- **Phone number ID** (a long number, *not* the phone number itself)
- **WhatsApp Business Account ID** (WABA ID)

### 12.6 Link the client's WhatsApp account to our app (one-time, per client)

Without this, Meta won't send that number's messages to us. Run on your Mac. The token is
typed at a hidden prompt so it doesn't end up in your history.

💻 Mac
```bash
read -rs META_TOKEN; echo
curl -s -X POST "https://graph.facebook.com/v21.0/WABA_ID/subscribed_apps" -H "Authorization: Bearer $META_TOKEN"; echo
```

(Paste the system-user token at the first, silent line and press Enter. Replace `WABA_ID`.)
It must print `{"success":true}`.

**Only if the number is new to the Cloud API** (WhatsApp Manager shows the number as *Pending*
or *Not registered*), register it with a 6-digit PIN you choose. **Save the PIN** (it's the
number's two-step verification PIN):

💻 Mac
```bash
curl -s -X POST "https://graph.facebook.com/v21.0/PHONE_NUMBER_ID/register" -H "Authorization: Bearer $META_TOKEN" -H "Content-Type: application/json" -d '{"messaging_product":"whatsapp","pin":"SIX_DIGIT_PIN"}'; echo
unset META_TOKEN
```

---

## Part 13 — Add the first client (Aquamena)

### 13.1 Copy the catalog file to the server

The catalog (`aquamena_catalog.json`) holds the **client-confirmed** products and coupon prices.

💻 Mac
```bash
scp aquamena_catalog.json hmh:~/
```

(Run it in the folder where the file is, or give its full path.)

### 13.2 Create the client

🖥️ Server — first type the two secrets at hidden prompts (nothing shows while typing):
```bash
read -rs SEED_META_ACCESS_TOKEN; echo; export SEED_META_ACCESS_TOKEN
read -rs SEED_ADMIN_PASSWORD;   echo; export SEED_ADMIN_PASSWORD
```

1. First line → paste the **Meta system-user token** (Part 12.4), Enter.
2. Second line → type a **first password for Aquamena's dashboard admin** (at least 12
   characters), Enter. **Save it in the password manager**. You'll give it to the client.

Then run (replace the CAPITALS):

🖥️ Server
```bash
dc run --rm -e SEED_META_ACCESS_TOKEN -e SEED_ADMIN_PASSWORD \
  -v ~/aquamena_catalog.json:/tmp/catalog.json:ro \
  api python -m api.scripts.seed_tenant --slug aquamena \
  --phone-number-id PHONE_NUMBER_ID --waba-id WABA_ID \
  --display-phone "+971 5X XXX XXXX" \
  --admin-email OWNER_EMAIL_AT_AQUAMENA \
  --escalation-phone 9715XXXXXXXX \
  --catalog /tmp/catalog.json
unset SEED_META_ACCESS_TOKEN SEED_ADMIN_PASSWORD
```

- `--escalation-phone` is the Aquamena staff number that is told when a customer needs a human.
  Digits only, starting with `971`, no `+`.
  The alert only goes out once an approved Utility template is set as well (without it the
  worker logs `escalation_notify_unconfigured`): see RUNBOOK → "Escalation alerts to staff".
- Optional: add `--service-start 2026-11-01 --free-months-until 2027-01-31` when the contract
  dates are confirmed.
- Success ends with a `seed_done` line showing the number of products and coupon packages.
- It is **safe to run again** (for example with a corrected catalog). It updates instead of
  duplicating.

Delete the copy of the catalog from the server:

🖥️ Server
```bash
shred -u ~/aquamena_catalog.json
```

### 13.3 Check the client's modules and channel

🖥️ Server
```bash
dc run --rm api python -m api.scripts.modules show --slug aquamena
dc run --rm api python -m api.scripts.channel_token list
```

- Modules should show the **water_delivery** set (catalog, orders, coupons, campaigns). To
  change: `… modules enable --slug aquamena water_delivery`.
- The channel line should say **token set**.

### 13.4 First real test 📱

1. From **your own phone**, send a WhatsApp message ("Hi") to Aquamena's number.
2. Within a few seconds you should get a reply that **says it's an AI assistant**.
3. Open `https://aquamena.heyozo.com`, sign in with the admin email + password from 13.2 →
   **Chats**: your conversation is there.
4. Reply **STOP** from your phone → you get an opt-out confirmation, and the dashboard shows
   you as opted out.

No reply? See [Part 19](#part-19--when-something-goes-wrong) → "No WhatsApp reply".

### 13.5 Import Aquamena's customer list

🖥️ Server — copy the file up (from 💻 Mac: `scp customers.xlsx hmh:~/`), then a **dry run**
(changes nothing, just checks):
```bash
mkdir -p ~/imports && mv ~/customers.xlsx ~/imports/
dc run --rm -v ~/imports:/imports api python -m api.scripts.import_excel --slug aquamena /imports/customers.xlsx
```

It prints a summary and writes a report CSV in `~/imports/`. Read it (rows skipped, phone
numbers that couldn't be understood). Fix the spreadsheet if needed and repeat. When it looks
right, run it for real by adding `--commit`:

🖥️ Server
```bash
dc run --rm -v ~/imports:/imports api python -m api.scripts.import_excel --slug aquamena /imports/customers.xlsx --commit
```

Imported customers are **not** opted in to marketing. They opt in through the QR codes (next).
Afterwards: `shred -u ~/imports/*` to remove the customer data from the server's home folder.

### 13.6 QR codes for opt-in

In Aquamena's dashboard → **Settings** → **Opt-in QR codes** → create one per place
(delivery van, flyer, shop counter…). Download and print. Scanning opens
`https://go.heyozo.com/q/…` and then WhatsApp with a ready-made message. Sending it opts the
customer in, with proof recorded.

### 13.7 Billing settings for the console

The console's margin and reimbursement figures need these four values on the client. Open the
database as the owner:

🖥️ Server
```bash
dc exec postgres psql -U agents_owner -d agents
```

Then type (replace the values; dates are `YYYY-MM-DD`):

```sql
UPDATE tenants SET monthly_fee_aed = 0000.00,
                   service_start = '2026-11-01',
                   free_months_until = '2027-01-31',
                   meta_charges_borne_by_us_until = '2026-12-14'
 WHERE slug = 'aquamena';
\q
```

It must print `UPDATE 1`. (`\q` exits.)

### 13.8 The data-processing description for the contract

🖥️ Server
```bash
dc run --rm api python -m api.scripts.dpa --slug aquamena > ~/dpa-aquamena.md
```

💻 Mac
```bash
scp hmh:~/dpa-aquamena.md ~/Desktop/
```

Review it, send it with the contract, then delete it from the server (`rm ~/dpa-aquamena.md`).

---

## Part 14 — Backups: switch on and test

### 14.1 Make the backup bucket (Cloudflare R2)

1. Cloudflare → **R2 Object Storage** → **Create bucket** → name `hmh-agents-backups`,
   location **Automatic** (or Europe/Asia) → Create.
2. R2 → **Manage R2 API Tokens** → **Create API token** → permission **Object Read & Write**,
   **Apply to specific bucket only → hmh-agents-backups** → Create.
3. Copy: **Access Key ID**, **Secret Access Key**, and the **endpoint** for S3 clients
   (looks like `https://<account-id>.r2.cloudflarestorage.com`) → **save in password manager**.

### 14.2 Put them in `.env`

🖥️ Server
```bash
nano /opt/agents/.env
```

```
BACKUP_BUCKET=hmh-agents-backups
BACKUP_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
BACKUP_ACCESS_KEY_ID=...
BACKUP_SECRET_ACCESS_KEY=...
```

(`BACKUP_AGE_RECIPIENT` and `DB_BACKUP_PASSWORD` you already filled in Part 8.) Save, exit, then:

🖥️ Server
```bash
dc up -d scheduler
```

### 14.3 Take a backup now

🖥️ Server
```bash
dc run --rm scheduler python -m api.scripts.backup run
dc run --rm scheduler python -m api.scripts.backup list
```

The list must show one backup with today's date. From now on one is taken **every night at
01:30 UTC**, and also before every deploy. Backups older than 30 days are deleted automatically.

### 14.4 Prove a restore works (do this before go-live, then on the first Monday of every month)

This creates a **separate, new** database from the backup and checks every table's row count.
It never touches the live data.

1. Copy the **backup private key** from your password manager into a file on the server:

   🖥️ Server
   ```bash
   nano /opt/agents/backup-age-key.txt
   ```
   Paste the key, save, exit.

2. Restore:

   🖥️ Server
   ```bash
   cd /opt/agents && set -a && . ./.env && set +a
   dc run --rm -v "$PWD/backup-age-key.txt:/key.txt:ro" scheduler \
     python -m api.scripts.backup restore --identity-file /key.txt \
     --target-db agents_restore_$(date +%Y%m%d) \
     --admin-url "postgresql://postgres:$POSTGRES_SUPERUSER_PASSWORD@postgres:5432/postgres"
   ```

   ✅ Good result: it ends with **"Row counts match"**.

3. **Immediately** remove the key from the server, and clear the loaded secrets:

   🖥️ Server
   ```bash
   shred -u /opt/agents/backup-age-key.txt
   exec bash
   ```

4. Drop the test copy so it doesn't use disk space:

   🖥️ Server
   ```bash
   dc exec postgres psql -U postgres -c "DROP DATABASE agents_restore_$(date +%Y%m%d);"
   ```

---

## Part 15 — Alerts to your WhatsApp

The platform can message HMH Labz staff on WhatsApp when something is wrong (database down,
backups failing, a client's quality rating dropping…).

Alerts are sent *from HMH Labz's own WhatsApp number*, which exists on the platform as an
internal tenant with the slug **`hmhlabz`** (no modules, no catalog, no message cap). **Do not**
set `ALERT_TENANT_SLUG=aquamena`: alerts would then come from the client's number.

1. **Template.** In HMH Labz's WhatsApp Manager → **Message templates** → **Create**:
   - Category **Utility**, name **`platform_alert`**, language **English**
   - Body: `HMH Labz platform alert: {{1}} Check the console for details.`
   - Submit and wait for **Approved** (minutes to a day).
2. **Create the HMH Labz tenant.** You need HMH Labz's own number's **Phone number ID** and
   **WhatsApp Business Account ID** (as in Part 12.5, for HMH Labz's own WABA) and a Meta
   system-user token that can send from that number (Part 12.4; link the WABA as in 12.6).

   🖥️ Server — first paste the token at a hidden prompt (nothing shows while typing):
   ```bash
   read -rs SEED_META_ACCESS_TOKEN; echo; export SEED_META_ACCESS_TOKEN
   ```
   Then run (replace the CAPITALS):

   🖥️ Server
   ```bash
   dc run --rm -e SEED_META_ACCESS_TOKEN \
     api python -m api.scripts.seed_tenant --slug hmhlabz \
     --phone-number-id HMH_PHONE_NUMBER_ID --waba-id HMH_WABA_ID \
     --display-phone "+971 5X XXX XXXX"
   unset SEED_META_ACCESS_TOKEN
   ```
   - No `--catalog`, `--admin-email` or `--escalation-phone`: this tenant only sends alerts.
     The `seed_catalog_empty` warning at the end is expected.
   - Success ends with a `seed_done` line with `access_token_set=true`. Safe to run again.
   - Check: `dc run --rm api python -m api.scripts.channel_token list` shows the HMH Labz
     number with **token set**.
3. **Settings** in `.env`:
   ```
   ALERT_TENANT_SLUG=hmhlabz
   ALERT_TO=9715XXXXXXXX,9715YYYYYYYY
   ```
   (Who receives alerts. Digits only, comma between numbers.) Save, then `dc up -d api worker scheduler`.
4. **Test**: each recipient must first send any WhatsApp message to the HMH Labz number once
   (that opens the conversation). Until the template is approved, alerts only appear in the logs.

What each alert means and what to do: see `RUNBOOK.md` → *Alerts: what each one means*.

---

## Part 16 — Status page and outside uptime check

### 16.1 Uptime Kuma (inside view)

1. Open `https://status.heyozo.com` → log in with `hmh` + the status password (Part 8.3).
2. First time: create the Uptime Kuma **admin account** → save in password manager.
   (It is Uptime Kuma v2. If it ever asks which database to use, something is off: the server
   is set to SQLite. Upgrading it later: `RUNBOOK.md` → *Upgrade Uptime Kuma*.)
3. **Add New Monitor** three times:

| Monitor type | Friendly name | URL | Interval |
|---|---|---|---|
| HTTP(s) | API health | `http://api:8000/health` | 60 s |
| HTTP(s) | Aquamena dashboard | `https://aquamena.heyozo.com/` | 60 s |
| HTTP(s) – Keyword *or* HTTP(s) with **Accepted status codes = 404** | Consent page host | `https://go.heyozo.com/q/x` | 60 s |

(The third one expects a 404 "not found". Getting a 404 proves the page server is up.)

### 16.2 UptimeRobot (outside view — the number for the contract's 99.5%)

1. Sign up free at **uptimerobot.com**.
2. **Add New Monitor** → type **HTTP(s)** → URL `https://api.heyozo.com/health` →
   interval **1 minute** (or the smallest allowed) → alert contact = your email.

---

## Part 17 — Final go-live checklist

Tick every line before telling the client it's live.

- [ ] `ssh hmh` works; `ssh root@…` and port 22 do **not**.
- [ ] `sudo ufw status` shows only 2222, 80, 443.
- [ ] `grep APP_ENV /opt/agents/.env` shows `production`.
- [ ] `https://api.heyozo.com/health` answers OK.
- [ ] All five addresses from Part 10 open with a padlock 🔒.
- [ ] Console login with 2-step code works.
- [ ] Meta webhook shows ✅ verified, four fields subscribed, app is **Live**.
- [ ] Test message from your phone gets an AI reply with the AI disclosure.
- [ ] **STOP** opts out, and the dashboard shows it.
- [ ] Aquamena admin can log in to their dashboard. Ask them to **change the password** at first sign-in.
- [ ] Customer list imported with `--commit`; report checked.
- [ ] A backup exists (`backup list`) **and** a restore test said "Row counts match".
- [ ] Backup private key is in the password manager **and** in one offline place.
- [ ] `APP_ENCRYPTION_KEY` and all `.env` values are in the password manager.
- [ ] Uptime Kuma and UptimeRobot are green.
- [ ] Spending limits set at Google AI and OpenRouter.
- [ ] Alerts: set up (Part 15) **or** consciously postponed with a date.

---

## Part 18 — Everyday operations

Always log in first: 💻 `ssh hmh` (you land in `/opt/agents`).

### Put a new version live (deploy)

After engineering pushes changes to GitHub `main`:

🖥️ Server
```bash
deploy/deploy.sh
```

It backs up first, updates the database, restarts and checks health. If anything fails
**before** the restart, the old version simply keeps running. Then send a test WhatsApp and open
a dashboard.

### Go back to the previous version (rollback)

🖥️ Server
```bash
tail -3 deploy/history.log
deploy/deploy.sh PREVIOUS_CODE
```

`PREVIOUS_CODE` = the short code (like `269fcd4`) on the line **before** the last one. Read
`RUNBOOK.md` → *Rollback* if the bad release changed the database structure.

### See what's running

🖥️ Server
```bash
dc ps
```

Every line should say **Up** (most also **(healthy)**).

### Look at the logs (what a box is saying)

🖥️ Server
```bash
dc logs --tail 100 api
dc logs --tail 100 worker
dc logs -f worker
```

(`-f` = keep watching live; **Ctrl + C** stops.) Logs never contain message text or tokens, by
design.

### Restart one box / everything

🖥️ Server
```bash
dc restart worker
dc up -d
```

### Disk space

🖥️ Server
```bash
df -h /
docker system df
docker image prune -a --filter until=168h -f
```

The last command removes old program versions older than a week (never data).

### Replace a client's Meta token (when it expires or leaks)

🖥️ Server
```bash
dc run --rm api python -m api.scripts.channel_token list
dc run --rm api python -m api.scripts.channel_token set --phone-number-id PHONE_NUMBER_ID
```

Paste the new token at the hidden prompt. It's tested with Meta **before** replacing the old
one. Then revoke the old token in Meta Business settings.

### Add another client later

DNS needs nothing (the `*` record covers it). Then: Meta steps 12.4–12.6 for their number →
engineering adds their profile to `seed_tenant.py` → Part 13 with their slug → choose modules
(`… modules enable --slug THEIRSLUG PRESET`, presets: `water_delivery`, `real_estate`,
`law_firm`). Full list: `RUNBOOK.md` → *Onboard a tenant*.

### Server updates and reboots

Security updates install themselves. Once a month:

🖥️ Server
```bash
sudo apt update && sudo apt -y upgrade
[ -f /var/run/reboot-required ] && echo "REBOOT NEEDED" || echo "no reboot needed"
```

If it says **REBOOT NEEDED**, do it at a quiet time (e.g. 03:00 UAE): `sudo reboot`. Everything
starts again by itself in about a minute. Messages that arrive meanwhile are retried by Meta.

### Monthly routine (first Monday)

1. Restore test (Part 14.4).
2. `df -h /` below 70%.
3. `dc run --rm api python -m api.scripts.channel_token list` — any token expiring soon?
4. Check Google AI / OpenRouter spend against the limits.

---

## Part 19 — When something goes wrong

| What you see | Most likely cause | What to do |
|---|---|---|
| `ssh: connect … Connection refused` / timed out | Wrong port or IP; or fail2ban blocked your IP after wrong attempts | Use `ssh hmh` (port 2222). Wait 1 hour, or use hPanel **Browser terminal** and run `sudo fail2ban-client unban YOUR_MAC_IP`. |
| `permission denied … docker.sock` | You didn't log out/in after Part 6 | `exit`, then `ssh hmh` again. |
| `deploy.sh` stops at **migrate** with a connection error | Database isn't running | `dc up -d postgres redis`, wait 20 s, run `deploy/deploy.sh` again. |
| `deploy.sh` says **/health did not come up** | A setting is wrong or missing | `dc logs --tail 100 api`. Read the last error. It usually names the setting (e.g. `APP_ENCRYPTION_KEY`, `JWT_SECRET`, `DATABASE_URL`). Fix `.env`, run `deploy/deploy.sh` again. |
| `password authentication failed for user "agents_app"` | `.env` DB passwords were changed **after** the database was first created | The database keeps the passwords from its first start. Put the original passwords back in `.env` (from the password manager). |
| Browser says **"Not secure"** / certificate error | Certificate not issued yet, DNS wrong, or orange cloud | `dc logs caddy \| grep -i error`. Check Part 4 (grey cloud, records point to the IP) and the Cloudflare token. A wrong token stops Caddy from starting at all. Then `dc restart caddy`. |
| Status page keeps asking for the password | Hash not in single quotes | Part 8.4 last row; then `dc up -d caddy`. |
| Meta **"Verify and save"** fails | Verify token mismatch, or api not reachable | Compare `META_VERIFY_TOKEN` in `.env` with what you typed in Meta. `curl https://api.heyozo.com/health` from your Mac. |
| **No WhatsApp reply** to a test message | App not Live; WABA not subscribed (12.6); wrong phone number ID; `META_APP_SECRET` wrong | `dc logs --tail 50 api \| grep -i webhook`: nothing → Meta isn't sending (check 12.2, 12.3, 12.6). `403` → `META_APP_SECRET` wrong. Received but no reply → `dc logs --tail 100 worker` (AI keys, token). |
| Replies say "we'll get back to you shortly" and the chat is handed to a person | Both AI providers failing | Check Google AI and OpenRouter keys, billing and spend caps. |
| Everything slow / alerts about queue | Worker overloaded | `dc up -d --scale worker=2`. |
| Disk alert | Old images or logs | Part 18 → Disk space. |

Still stuck? Collect the output of `dc ps` and the **last 100 lines** of the relevant log
(`dc logs --tail 100 api`). Never include `.env`. Send that to engineering. Deeper procedures
(database outage, quality rating drops, "the agent said something wrong") are in `RUNBOOK.md`.

---

## Appendix A — Running on the AWS free tier instead of Hostinger

Use this for the **first couple of months** (testing and the first clients). Follow the guide
as normal; this appendix only lists the steps that are **different** on AWS. Everything else
(Docker, the code, `.env`, Meta, backups, alerts) is exactly the same.

**What you get.** A new AWS account (opened on or after 15 July 2025) gets **USD 100 of credit**
(up to 100 more for finishing AWS's sign-up tasks) for **6 months**. The server we use, an
**m7i-flex.large** (2 CPU, 8 GB), is about **USD 75 a month** with its disk and IP address, so the
credit lasts roughly **6–10 weeks**. When the credit or the 6 months runs out, AWS asks you to
upgrade to a paid plan, or it closes the account. Plan the move (A.8) **before** that happens.

**The limit.** 8 GB is half of what the guide asks for. A smaller settings file
(`docker-compose.aws.yml`) shrinks every box to fit. It is fine for testing and a few clients,
not for heavy traffic or large campaigns.

### A.1 (replaces Part 1, item 1) — the AWS account

1. Sign up at **aws.amazon.com** → choose the **Free plan**. A card is needed for identity;
   nothing is charged on the free plan.
2. Turn on 2-step login for the root user: account name (top right) → **Security credentials**
   → **Assign MFA device**.
3. **Billing and Cost Management** → **Credits** shows what is left. Check it every week.
4. **Billing and Cost Management** → **Budgets** → **Create budget** → template
   **"Monthly cost budget"** → amount **10 USD** → your email. You get an email if something
   starts costing real money.

### A.2 (replaces Part 3) — start the server

AWS console → top-right region menu: choose **Middle East (UAE) me-central-1**. If, in step 3
below, `m7i-flex.large` is not marked **Free tier eligible** there, switch the region to
**Europe (Frankfurt) eu-central-1** or **Asia Pacific (Mumbai) ap-south-1** and use that one
for everything below.

1. **EC2** → **Key pairs** (left menu) → **Actions → Import key pair**. Name `hmh-mac`. Paste
   the public key from step 2.3 (⌘ + V) → **Import key pair**.
2. **EC2** → **Instances** → **Launch instances**. Name: `hmh-agents`.
3. **Application and OS Images:** **Ubuntu** → **Ubuntu Server 24.04 LTS** (64-bit x86).
   **Instance type:** `m7i-flex.large` (must say **Free tier eligible**).
4. **Key pair:** `hmh-mac`.
5. **Network settings → Edit → Create security group** named `hmh-agents`. This is AWS's
   firewall, in front of the server's own one (UFW, Part 5.4). Add these rules:

   | Type | Port | Source |
   |---|---|---|
   | SSH | 22 | **My IP** (removed again in A.4) |
   | Custom TCP | 2222 | Anywhere (0.0.0.0/0) |
   | HTTP | 80 | Anywhere |
   | HTTPS | 443 | Anywhere |
   | Custom UDP | 443 | Anywhere |

6. **Configure storage:** **30** GiB, type **gp3**.
7. **Launch instance.** Wait until **Instance state** shows **Running**.
8. Give the server a fixed address (otherwise it changes on every stop/start and breaks DNS):
   **EC2** → **Elastic IPs** → **Allocate Elastic IP address** → **Allocate**. Select it →
   **Actions → Associate Elastic IP address** → instance `hmh-agents` → **Associate**.
   This address is `YOUR_SERVER_IP` everywhere in the guide. **Save it.**
9. **Emergency door** (the AWS version of hPanel's browser terminal): **EC2** → **Settings**
   (left menu, bottom) → **EC2 Serial Console** → **Manage** → **Allow** → Save. If you ever
   lock yourself out: select the instance → **Connect** → **EC2 serial console**, then log in as
   `deploy` with the deploy password from 5.3.

Then do Part 4 (DNS) as written, with the Elastic IP.

> **Shortcut:** steps 1–8 are also written as one CloudFormation stack in
> `deploy/aws/ec2-free-tier.yaml` (the command is at the top of that file). The stack's
> `ServerIp` output is `YOUR_SERVER_IP`.

### A.3 (replaces Part 5.1 and 5.3) — first login and the `deploy` user

On AWS you first log in as **`ubuntu`**, not `root`:

💻 Mac
```bash
ssh ubuntu@YOUR_SERVER_IP
```

Then become root and carry on with 5.2 as written:

🖥️ Server
```bash
sudo -i
```

In **5.3**, the key must be copied from the `ubuntu` user (AWS puts a "please log in as ubuntu"
block in root's key file, which would lock `deploy` out). Run `adduser` as written, then use this
instead of the second box:

🖥️ Server (as root)
```bash
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp /home/ubuntu/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys; ls -l /home/deploy/.ssh
```

5.4 and 5.5 are the same (keep UFW on as well; the two firewalls don't interfere).

### A.4 (addition to Part 5.6) — close port 22 in AWS as well

After `sudo ufw delete allow 22/tcp`, also remove it from AWS: **EC2** → **Security Groups** →
`hmh-agents` → **Inbound rules** → **Edit inbound rules** → **Delete** the SSH / 22 row →
**Save rules**.

If the server was made with the CloudFormation stack, close it there instead (a console edit
would be undone by the next stack update):

💻 Mac
```bash
aws cloudformation deploy --profile HMHLabz --region us-east-1 --stack-name hmh-agents --template-file deploy/aws/ec2-free-tier.yaml --parameter-overrides AllowSsh22=false
```

Do 5.7 and 5.8 as written. **Do 5.9 (swap) — on 8 GB it is not optional.**

### A.5 (replaces Part 7.4) — the `dc` shortcut includes the AWS sizing file

🖥️ Server
```bash
echo "alias dc='docker compose -f /opt/agents/docker-compose.yml -f /opt/agents/docker-compose.aws.yml --project-directory /opt/agents'" >> ~/.bashrc
echo "cd /opt/agents" >> ~/.bashrc
source ~/.bashrc
```

### A.6 (addition to Part 8.4) — one extra line in `.env`

Near the top of `.env`, under `LOG_LEVEL`, add (no `#` in front):

```
COMPOSE_OVERRIDE=docker-compose.aws.yml
```

`deploy/deploy.sh` reads it and uses the smaller sizing. Check it after Part 9:

🖥️ Server
```bash
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}'
```

The postgres line should end in **/ 2GiB** (it would say 3.5GiB without the AWS file).

### A.7 What else is different

- **Part 3, step 7 (Hostinger snapshots):** skip. AWS snapshots cost extra; our real backups
  (Part 14) are enough.
- **Server updates and reboots (Part 18):** reboot with `sudo reboot` as written. Never
  **Stop** and **Start** the instance from the AWS console unless needed; it keeps its Elastic
  IP, but every stopped hour still pays for the disk and the IP.
- **Watch memory** for the first week: `free -h`. If **Swap used** stays above about 1 GB, the
  server is too small for the traffic. Time to move (A.8).

### A.8 When the credit runs out — moving up

Decide about **two weeks before** the credit ends (A.1, step 3). Two choices:

**Stay on AWS (paid).** Upgrade the account to a paid plan when AWS asks. Then make the server
bigger:

1. AWS console: select the instance → **Instance state → Stop**. Wait for **Stopped**.
2. **Actions → Instance settings → Change instance type** → `m7i.xlarge` (4 CPU, 16 GB) or
   bigger → **Apply**. **Instance state → Start**. The Elastic IP stays the same.
3. 💻 `ssh hmh`, then remove the AWS sizing:

   🖥️ Server
   ```bash
   sed -i '/^COMPOSE_OVERRIDE=/d' /opt/agents/.env
   sed -i '/alias dc=/d' ~/.bashrc
   echo "alias dc='docker compose -f /opt/agents/docker-compose.yml --project-directory /opt/agents'" >> ~/.bashrc
   source ~/.bashrc
   deploy/deploy.sh
   ```

**Move to Hostinger.** Set up the new server with Parts 3–9 as written (without this appendix).
Then restore the latest backup on it with `RUNBOOK.md` "Backups and restore", switch the five Cloudflare DNS
records (Part 4.1) to the new IP, and test before you delete anything on AWS. Afterwards, on
AWS: **terminate** the instance and **release** the Elastic IP (an unattached Elastic IP is
still billed).

---

## Glossary

| Word | Meaning |
|---|---|
| **VPS** | A rented computer in a data-centre that we control fully. |
| **SSH** | The secure way to type commands on the server from your Mac. |
| **SSH key** | A pair of files that replaces a password. The public half goes on the server; the private half never leaves your Mac. |
| **root / sudo** | The all-powerful admin account / "run this one command as admin". |
| **Docker / container** | A sealed box that runs one program with everything it needs. |
| **`dc`** | Our shortcut for "docker compose with the production file". |
| **DNS** | The internet's phone book: turns `api.heyozo.com` into the server's IP. |
| **HTTPS certificate** | What gives the browser padlock 🔒; Caddy gets them free from Let's Encrypt. |
| **Webhook** | Meta "calling" our server each time a customer sends a WhatsApp message. |
| **Tenant** | One client business on the platform (e.g. Aquamena). Each tenant's data is walled off from the others. |
| **Slug** | The short name of a tenant used in addresses and commands, e.g. `aquamena`. |
| **WABA** | WhatsApp Business Account — the client's account at Meta that owns their number. |
| **Phone number ID** | Meta's internal ID for the WhatsApp number (not the phone number itself). |
| **System-user token** | A permanent key that lets the platform send messages from a client's number. |
| **Migration** | An automatic update of the database's structure, run by the deploy script. |
| **`.env`** | The file on the server holding all settings and secrets. |
| **age key** | The lock (public key) and the only key (private key) for the backups. |
| **R2** | Cloudflare's file storage, where encrypted backups are kept. |
| **TOTP / 2-step code** | The 6-digit code from your authenticator app. |
