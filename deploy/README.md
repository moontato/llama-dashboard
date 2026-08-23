# Deployment

Target: Jetson AGX Orin running JetPack 5.1.2 / Ubuntu 20.04.  
URL: `https://orinserver.tailbf896b.ts.net` (Tailscale Serve, tailnet-only).

## Migration (existing `jtop-web` install → `llama-dashboard`)

If the dashboard is already deployed under the old name, rename it in place.  
This is a rename of the unit, user, and install path — **not** a fresh install.

```bash
# 1. Create the new user (keep the same socket group so /run/jtop.sock still works)
sudo useradd -r -s /sbin/nologin llama-dashboard
sudo usermod -aG jtop llama-dashboard

# 2. Move the app to the new path and fix ownership
sudo mv /opt/jtop-web /opt/llama-dashboard
# The service only READS these files, so ownership can be the user who deploys:
# sudo chown -R <your-user>:<your-user> /opt/llama-dashboard        # e.g. git-pull workflow
# sudo chown -R llama-dashboard:llama-dashboard /opt/llama-dashboard

# 3. Drop the new unit in place, then swap the user's sudoers file
sudo cp deploy/llama-dashboard.service /etc/systemd/system/
sudo visudo -f /etc/sudoers.d/llama-dashboard     # create (paste the two rules below)
sudo rm /etc/sudoers.d/jtop-web

# 4. Reload and switch services (brief downtime while old stops / new starts)
sudo systemctl daemon-reload
sudo systemctl disable --now jtop-web
sudo systemctl enable --now llama-dashboard

# 5. Verify, then remove the old user
sudo systemctl status llama-dashboard
journalctl -u llama-dashboard -f
sudo userdel jtop-web
```

The two sudoers rules (paste into `/etc/sudoers.d/llama-dashboard` in step 3):

```
llama-dashboard ALL=(ALL) NOPASSWD: /bin/systemctl restart llama-server.service
llama-dashboard ALL=(ALL) NOPASSWD: /usr/bin/git -C /mnt/ssd/llamacpp_models/models_ini pull
```

Notes:
- The `jtop` **group** membership is required (socket access) and is *not* renamed.
- Step 2 must run before the service starts; if `mv` fails because files are in use, stop the old unit first: `sudo systemctl disable --now jtop-web`.
- The old unit file `jtop-web.service` is replaced by `llama-dashboard.service`; if systemd warns about a lingering `jtop-web.service`, remove it: `sudo rm /etc/systemd/system/jtop-web.service`.

## Prerequisites

In the Tailscale admin console, enable **MagicDNS** and **HTTPS certificates**
for the tailnet — required for Serve to provision a cert automatically.

## 1. Copy files

```bash
sudo mkdir -p /opt/llama-dashboard/static
sudo cp app.py models_ini.py requirements.txt /opt/llama-dashboard/
sudo cp static/index.html /opt/llama-dashboard/static/
sudo rm -rf /opt/llama-dashboard/tests && sudo cp -r tests /opt/llama-dashboard/
# re-run the suite on the Orin (no pytest needed):
/usr/bin/python3 -m unittest -v tests.test_models_ini
```

## 2. Install Flask

`jetson-stats` is already present via `apt`. Only Flask is needed:

```bash
python3 -m pip install flask
```

## 3. Create service user

```bash
sudo useradd -r -s /sbin/nologin llama-dashboard
sudo usermod -aG jtop llama-dashboard     # grant access to /run/jtop.sock
sudo chown -R llama-dashboard:llama-dashboard /opt/llama-dashboard
```

### Grant restart permission (required for the Advanced controls button)

The service user needs to run exactly one `sudo` command without a password.
Add a narrow sudoers rule:

```bash
sudo visudo -f /etc/sudoers.d/llama-dashboard
```

Paste these lines and save:

```
llama-dashboard ALL=(ALL) NOPASSWD: /bin/systemctl restart llama-server.service
llama-dashboard ALL=(ALL) NOPASSWD: /usr/bin/git -C /mnt/ssd/llamacpp_models/models_ini pull
```

The first allows restarting only that service; the second (legacy) powers the old
"git pull" button and is replaced by the deploy-key setup below.

### Enable the models.ini editor (deploy key — no more sudo for git)

The dashboard edits `models.ini` inside the clone and commits/pulls/pushes as the
service user. One-time setup:

```bash
# 1. The clone (and everything it will write) belongs to the service user
sudo chown -R llama-dashboard:llama-dashboard /mnt/ssd/llamacpp_models/models_ini

# 2. A throwaway SSH key pair for the service user (it needs a home dir)
sudo usermod -d /home/llama-dashboard llama-dashboard 2>/dev/null || true
sudo -u llama-dashboard bash -c 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && ssh-keygen -t ed25519 -C llama-dashboard -N ""'
sudo -u llama-dashboard cat /home/llama-dashboard/.ssh/id_ed25519.pub

# 3. GitHub: model repo → Settings → Deploy keys → Add deploy key
#    paste the public key above, tick "Allow write access"

# 4. Commit identity (local to this repo only)
sudo -u llama-dashboard git config -C /mnt/ssd/llamacpp_models/models_ini user.name "llama-dashboard"
sudo -u llama-dashboard git config -C /mnt/ssd/llamacpp_models/models_ini user.email "llama-dashboard@localhost"

# 5. Verify both directions without touching the dashboard UI
sudo -u llama-dashboard git -C /mnt/ssd/llamacpp_models/models_ini pull --ff-only
# (commit a no-op or a trivial edit to test push)

# 6. The sudo git-pull line in /etc/sudoers.d/llama-dashboard is now legacy —
#    remove it (keep the systemctl line).
```

## 4. Install and start the systemd unit

```bash
sudo cp deploy/llama-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llama-dashboard
```

Check status:

```bash
sudo systemctl status llama-dashboard
journalctl -u llama-dashboard -f
```

## 5. Expose on the tailnet via Tailscale Serve

```bash
tailscale serve --bg 8080
```

This maps `https://orinserver.tailbf896b.ts.net` → `http://127.0.0.1:8080`,
terminates TLS, and restricts access to tailnet members only (not public internet).

Verify:

```bash
tailscale serve status
curl -N https://orinserver.tailbf896b.ts.net/healthz
```

## 6. Open the dashboard

Navigate to `https://orinserver.tailbf896b.ts.net` from any device on the tailnet.

---

## Tear down

```bash
tailscale serve --bg 8080 off
sudo systemctl disable --now llama-dashboard
```

## Switching to direct-bind (no Serve)

See the "Switching exposure" section in CLAUDE.md.  
Short version: set `BIND_HOST` in `app.py` to the Tailscale IP, stop `serve`,
add `After=tailscaled.service` + `Wants=tailscaled.service` to the unit.
