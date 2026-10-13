"""Deploy-config drift tests — deploy/Caddyfile + deploy/nginx-v42.ir.conf.

Run:  python -m unittest tests.test_deploy_configs -v

There is no way to run nginx/Caddy from a Windows dev box, so the shipped
configs are checked structurally instead: they must keep pointing at the
bot's loopback port, must not fight another service for ports 80/443, and
must carry the two proxy headers the dashboard's CSRF guard and Secure
cookie depend on (proved separately in tests/test_proxy_contract.py).

Topology being guarded (deploy/README.md):
    https://<name>:8787  ->  Caddy on the PUBLIC IP only  ->  127.0.0.1:8787
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
CADDY = DEPLOY / "Caddyfile"
NGINX = DEPLOY / "nginx-v42.ir.conf"


def live_lines(path: Path) -> list[str]:
    """Config lines with comments removed (a commented example must not fool
    the checks below)."""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def caddy_basic_auth_creds(path: Path) -> list[str]:
    """The credential lines inside the `basic_auth { ... }` block.

    Caddy refuses plaintext passwords, so each must be a hash — this is what
    keeps a copy-pasted plaintext password out of the repo.
    """
    out: list[str] = []
    inside, depth = False, 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("basic_auth"):
            inside = True
            depth = line.count("{") - line.count("}")
            continue
        if inside:
            depth += line.count("{") - line.count("}")
            if inside and line != "}" and depth > 0:
                out.append(line)
            if depth <= 0:
                inside = False
    return out


class DeployConfigCase(unittest.TestCase):
    def caddy_upstream(self) -> int:
        text = "\n".join(live_lines(CADDY))
        m = re.search(r"reverse_proxy\s+127\.0\.0\.1:(\d+)", text)
        self.assertIsNotNone(m, "Caddyfile must proxy to 127.0.0.1:<port>")
        return int(m.group(1))

    def caddy_public_port(self) -> int:
        for line in live_lines(CADDY):
            m = re.match(r"^[\w.*\-]+:(\d+)\s*\{", line)
            if m:
                return int(m.group(1))
        self.fail("Caddyfile must have a site address with an explicit port")

    def nginx_upstream(self) -> int:
        text = "\n".join(live_lines(NGINX))
        m = re.search(r"proxy_pass\s+http://127\.0\.0\.1:(\d+)", text)
        self.assertIsNotNone(m, "nginx conf must proxy to 127.0.0.1:<port>")
        return int(m.group(1))

    def nginx_public_port(self) -> int:
        for line in live_lines(NGINX):
            m = re.match(r"listen\s+([\d.]+|\*|\[::\]):(\d+)", line)
            if m:
                return int(m.group(2))
        self.fail("nginx conf must listen on an explicit ip:port")

    # ------------------------------------------------------------------ ports
    def test_both_configs_agree_on_the_upstream_port(self):
        self.assertEqual(self.caddy_upstream(), self.nginx_upstream())

    def test_upstream_is_the_loopback_port_the_bot_uses(self):
        self.assertEqual(self.caddy_upstream(), Config().engine.server_port)

    def test_public_listener_takes_a_specific_interface_not_a_wildcard(self):
        """The bot holds 127.0.0.1:8787 — a wildcard bind on the same port
        would collide, so both configs must name the public IP explicitly."""
        caddy = "\n".join(live_lines(CADDY))
        binds = re.findall(r"^\s*bind\s+(\S+)", caddy, flags=re.M)
        self.assertTrue(binds, "Caddyfile needs `bind <public-ip>`")
        for host in binds:
            self.assertNotIn(host, ("0.0.0.0", "::", "*"), host)
        for line in live_lines(NGINX):
            if line.startswith("listen"):
                self.assertNotRegex(line, r"^listen\s+(\*|\[::\]|0\.0\.0\.0):",
                                    "nginx must bind the public IP, not a wildcard")

    def test_no_config_competes_for_ports_80_or_443(self):
        """Another service owns those ports on this host (measured), so
        grabbing them would stop Caddy/nginx from starting at all.

        Checked line by line: `^` inside a multi-line assertNotRegex would
        only ever match at the start of the whole string and pass silently.
        """
        for line in live_lines(CADDY) + live_lines(NGINX):
            self.assertNotRegex(line, r"^listen\s+\S*:?(80|443)\s*;", line)
            self.assertNotRegex(line, r"^[\w.*\-]+:(80|443)\s*\{", line)

    def test_caddy_will_not_grab_port_80_for_the_redirect(self):
        self.assertIn("auto_https disable_redirects",
                      "\n".join(live_lines(CADDY)))

    # ------------------------------------------------------------------- auth
    def test_both_configs_require_a_proxy_password(self):
        self.assertIn("basic_auth", "\n".join(live_lines(CADDY)))
        self.assertIn("auth_basic", "\n".join(live_lines(NGINX)))
        self.assertIn("auth_basic_user_file", "\n".join(live_lines(NGINX)))

    def test_proxy_password_lines_are_hashes_not_plaintext(self):
        creds = caddy_basic_auth_creds(CADDY)
        self.assertEqual(len(creds), 1, creds)
        user, _, hashed = creds[0].partition(" ")
        self.assertTrue(user)
        self.assertTrue(hashed.startswith("$2") or
                        hashed.startswith("$argon2"),
                        "paste caddy hash-password output, not a password: %s"
                        % hashed)

    # ------------------------------------------------------- header contract
    def test_nginx_carries_the_headers_the_dashboard_needs(self):
        text = "\n".join(live_lines(NGINX))
        self.assertIn("proxy_set_header Host $http_host;", text)
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", text)
        # $proxy_host is nginx's default and breaks every POST (403)
        self.assertNotIn("Host $proxy_host", text)

    def test_readme_documents_the_same_topology(self):
        text = (DEPLOY / "README.md").read_text(encoding="utf-8")
        port = self.caddy_upstream()
        self.assertIn("https://v42.ir:%d" % port, text)
        self.assertIn("127.0.0.1:%d" % port, text)
        self.assertIn("bind 185.165.30.181", text)


if __name__ == "__main__":
    unittest.main()
