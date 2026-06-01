# Deployment

Target: Jetson AGX Orin running JetPack 5.1.2 / Ubuntu 20.04.  
URL: `https://orinserver.tailbf896b.ts.net` (Tailscale Serve, tailnet-only).

## Prerequisites

In the Tailscale admin console, enable **MagicDNS** and **HTTPS certificates**
for the tailnet — required for Serve to provision a cert automatically.

## 1. Copy files

```bash
sudo mkdir -p /opt/jtop-web/static
sudo cp app.py requirements.txt /opt/jtop-web/
sudo cp static/index.html /opt/jtop-web/static/
```

## 2. Install Flask

`jetson-stats` is already present via `apt`. Only Flask is needed:

```bash
python3 -m pip install flask
```

## 3. Create service user

```bash
sudo useradd -r -s /sbin/nologin jtop-web
sudo usermod -aG jtop jtop-web     # grant access to /run/jtop.sock
sudo chown -R jtop-web:jtop-web /opt/jtop-web
```

### Grant restart permission (required for the Advanced controls button)

The service user needs to run exactly one `sudo` command without a password.
Add a narrow sudoers rule:

```bash
sudo visudo -f /etc/sudoers.d/jtop-web
```

Paste these lines and save:

```
jtop-web ALL=(ALL) NOPASSWD: /bin/systemctl restart llama-server.service
jtop-web ALL=(ALL) NOPASSWD: /usr/bin/git -C /ssd/llamacpp_models/models_ini pull
```

The first allows restarting only that service; the second allows `git pull` in only that directory.
Verify with:
```bash
sudo -u jtop-web sudo systemctl restart llama-server.service
sudo -u jtop-web sudo git -C /ssd/llamacpp_models/models_ini pull
```

## 4. Install and start the systemd unit

```bash
sudo cp deploy/jtop-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jtop-web
```

Check status:

```bash
sudo systemctl status jtop-web
journalctl -u jtop-web -f
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
sudo systemctl disable --now jtop-web
```

## Switching to direct-bind (no Serve)

See the "Switching exposure" section in CLAUDE.md.  
Short version: set `BIND_HOST` in `app.py` to the Tailscale IP, stop `serve`,
add `After=tailscaled.service` + `Wants=tailscaled.service` to the unit.
