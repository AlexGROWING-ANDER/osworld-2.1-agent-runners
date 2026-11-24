"""Serve the verified benchmark attachment to the local QEMU guest."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
from pathlib import Path
import ssl


NAME = "AI-Assisted_Healthcare.zip"
ASSET = Path("H:/GUI_AGENT_OSWorld/v2_assets/task_026") / NAME
EXPECTED = "c9d83c187b7f6d055108d9838d0f65fb64401fe0755274d190b9af816802e951"


class AttachmentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != f"/{NAME}":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{NAME}"')
        self.send_header("Content-Length", str(len(attachment)))
        self.end_headers()
        self.wfile.write(attachment)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    attachment = ASSET.read_bytes()
    if hashlib.sha256(attachment).hexdigest() != EXPECTED:
        raise SystemExit("Official #026 attachment hash mismatch")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    with ThreadingHTTPServer(("127.0.0.1", 19426), AttachmentHandler) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        server.serve_forever()
