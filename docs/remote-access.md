# Remote access and custom domains

By default the Odin server only listens on `127.0.0.1:8788`. This page covers opening the dashboard from other devices, serving it over HTTPS, and putting it behind a local domain such as `odin.dev`. All settings go in the `server` block of `~/.config/odin/config.json` (see [Configure](../README.md#configure-optional)).

## Access from your network

By default the server only listens on `127.0.0.1`. To open the dashboard from a phone or another computer on the same network, add a `server` block to `~/.config/odin/config.json` and restart the server:

```json
"server": { "host": "0.0.0.0" }
```

The server prints the network URL, for example `http://192.168.1.20:8788/`. Pages render and every button works over plain HTTP.

**Optional: HTTPS, for browser notifications on other computers.** Browsers only allow notifications on a secure origin. iPhones don't support Odin's page notifications at all, so this only helps on other desktops and laptops.

1. Create a certificate other devices can trust:
   ```bash
   brew install mkcert
   mkcert -install
   mkdir -p ~/.config/odin/certs
   mkcert -cert-file ~/.config/odin/certs/odin.pem -key-file ~/.config/odin/certs/odin-key.pem localhost 127.0.0.1 ::1 "$(hostname)" "$(ipconfig getifaddr en0)"
   ```
   `mkcert -install` adds a local root certificate to this machine's trust store. Each other device needs to trust it once too: copy `"$(mkcert -CAROOT)/rootCA.pem"` to it and install it as a trusted certificate (on iOS, also turn it on in Settings › General › About › Certificate Trust Settings).
2. Add the certificate to the `server` block and restart the server:
   ```json
   "server": { "host": "0.0.0.0", "cert": "~/.config/odin/certs/odin.pem", "key": "~/.config/odin/certs/odin-key.pem" }
   ```

With a cert configured, the server speaks HTTPS on every interface, local `https://127.0.0.1:8788` included, and Odin's scripts and links switch to `https` automatically. `--host`, `--cert` and `--key` override the config for one run.

**There is no authentication.** Anyone who can reach the port can read and write files in your repositories and approve, reject or wake the agent. Only bind to `0.0.0.0` on a network you trust. The server does refuse requests whose `Host` is an unknown domain name (DNS rebinding) and POSTs from other websites (CSRF); add extra hostnames you use, such as a router-assigned name, to `server.allowed_hosts`.

## Custom local domain (`odin.dev`)

To open the dashboard at `https://odin.dev/` (and staging at `https://staging.odin.dev/`) instead of `127.0.0.1:8788`, put an `nginx` reverse proxy in front of Odin. Browsers force `https` on port `443` for every `.dev` domain (HSTS), so you also need a trusted local certificate and an `/etc/hosts` entry. Odin itself keeps serving plain HTTP on `127.0.0.1:8788`; leave `server.cert` and `server.key` unset, or change the `proxy_pass` lines below to `https://`.

1. **Install the tools and trust mkcert's local CA** (skip `mkcert -install` if you already ran it for the HTTPS section above):
   ```bash
   brew install nginx mkcert
   mkcert -install
   ```
2. **Map the names to this machine:**
   ```bash
   printf '127.0.0.1 odin.dev staging.odin.dev\n::1 odin.dev staging.odin.dev\n' | sudo tee -a /etc/hosts && sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder
   ```
   `odin.dev` is a real public domain. Without the `::1` line, a browser that looks up IPv6 addresses can reach the public site instead of your dashboard.
3. **Create a certificate for both names:**
   ```bash
   mkdir -p ~/.config/odin/certs
   mkcert -cert-file ~/.config/odin/certs/odin.dev.pem -key-file ~/.config/odin/certs/odin.dev-key.pem odin.dev staging.odin.dev
   ```
4. **Add an `nginx` server file** at `/opt/homebrew/etc/nginx/servers/odin.conf`. nginx doesn't expand `~`, so write the certificate paths out in full:
   ```nginx
   server {
       listen 80;
       listen [::]:80;
       server_name odin.dev staging.odin.dev;
       return 301 https://$host$request_uri;
   }

   server {
       listen 443 ssl;
       listen [::]:443 ssl;
       server_name odin.dev;
       ssl_certificate     /Users/<you>/.config/odin/certs/odin.dev.pem;
       ssl_certificate_key /Users/<you>/.config/odin/certs/odin.dev-key.pem;

       location / {
           proxy_pass http://127.0.0.1:8788;
           proxy_http_version 1.1;
           proxy_set_header Host $http_host;
           proxy_set_header X-Forwarded-Host $http_host;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }

   server {
       listen 443 ssl;
       listen [::]:443 ssl;
       server_name staging.odin.dev;
       ssl_certificate     /Users/<you>/.config/odin/certs/odin.dev.pem;
       ssl_certificate_key /Users/<you>/.config/odin/certs/odin.dev-key.pem;

       location / {
           proxy_pass http://127.0.0.1:8789;
           proxy_http_version 1.1;
           proxy_set_header Host $http_host;
           proxy_set_header X-Forwarded-Host $http_host;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```
5. **Start nginx as root**, because ports 80 and 443 need it. Check the config first:
   ```bash
   sudo nginx -t && sudo brew services start nginx
   ```
   After later edits to `odin.conf`, run `sudo nginx -s reload`.
   Homebrew warns that it is taking `root` ownership of some nginx paths; that is expected when the service runs as root.
6. **Allow the names** in `~/.config/odin/config.json`, then restart the Odin server:
   ```json
   "server": { "allowed_hosts": ["odin.dev", "staging.odin.dev"] }
   ```

Pages served through the proxy rewrite their `127.0.0.1:8788` and `:8789` links to `https://odin.dev` and `https://staging.odin.dev` (using `X-Forwarded-Proto` for the scheme), and the dashboard treats `staging.odin.dev` as the staging view. Links that Odin prints in chat and in desktop notifications still use `127.0.0.1:8788`. `https://staging.odin.dev/` returns `502 Bad Gateway` until the staging server on `:8789` is running.

