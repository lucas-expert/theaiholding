"""
Vercel Serverless Function (Python runtime) — spustí týdenní refresh.
Volá se:
  - Vercel Cronem každé pondělí 06:00 UTC (viz vercel.json)
  - Ručně: GET /api/cron-refresh?token=<CRON_SECRET>
"""
import os
import sys
import json
import subprocess
from http.server import BaseHTTPRequestHandler

# Nastav cestu tak, aby šel import skriptu jako modulu
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "fetch_and_rank.py")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Ověření: Vercel posílá Authorization: Bearer <CRON_SECRET>,
        # nebo ručně přes ?token=...
        expected = os.environ.get("CRON_SECRET", "")
        auth = self.headers.get("Authorization", "")
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        token = qs.get("token", [""])[0]

        authorized = (
            expected and (
                auth == f"Bearer {expected}" or token == expected
            )
        )
        if not authorized:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
            return

        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT],
                capture_output=True, text=True, timeout=290,
                env=os.environ.copy(),
            )
            ok = proc.returncode == 0
            out = proc.stdout.strip().split("\n")[-1] if proc.stdout else ""
            try:
                result = json.loads(out) if out.startswith("{") else {"raw": out}
            except Exception:
                result = {"raw": out}

            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": ok,
                "returncode": proc.returncode,
                "result": result,
                "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
            }).encode())
        except subprocess.TimeoutExpired:
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "timeout"}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
