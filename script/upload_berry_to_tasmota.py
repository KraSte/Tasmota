import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple


@dataclass(frozen=True)
class UploadResult:
    url: str
    status: int
    body_text: str


def _guess_mime_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".be"):
        return "text/plain"
    if lower.endswith(".bat"):
        return "text/plain"
    if lower.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def _multipart_form_data(
    fields: Dict[str, str],
    files: Iterable[Tuple[str, str, bytes, str]],
    boundary: str,
) -> bytes:
    crlf = "\r\n"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}{crlf}".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"{crlf}{crlf}'.encode(
                "utf-8"
            )
        )
        chunks.append(value.encode("utf-8"))
        chunks.append(crlf.encode("utf-8"))

    for field_name, filename, data, content_type in files:
        chunks.append(f"--{boundary}{crlf}".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"{crlf}'
            ).encode("utf-8")
        )
        chunks.append(f"Content-Type: {content_type}{crlf}{crlf}".encode("utf-8"))
        chunks.append(data)
        chunks.append(crlf.encode("utf-8"))

    chunks.append(f"--{boundary}--{crlf}".encode("utf-8"))
    return b"".join(chunks)


def upload_file_to_tasmota_ufs(
    *,
    host: str,
    port: int,
    use_https: bool,
    endpoint: str,
    file_path: str,
    dest_name: str,
    user: Optional[str],
    password: Optional[str],
    timeout_s: float,
    file_field_name: Optional[str],
) -> UploadResult:
    scheme = "https" if use_https else "http"

    # endpoint can be either a plain path (/ufsu) or include query params (/ufsu?fsz=)
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    endpoint_path, sep, endpoint_query = endpoint.partition("?")
    merged_query: Dict[str, str] = dict(
        urllib.parse.parse_qsl(endpoint_query, keep_blank_values=True)
    )
    if user:
        merged_query["user"] = user
    if password:
        merged_query["password"] = password

    base_url = f"{scheme}://{host}:{port}{endpoint_path}"
    url = base_url
    if merged_query:
        url = f"{base_url}?{urllib.parse.urlencode(merged_query, doseq=True)}"

    with open(file_path, "rb") as f:
        data = f.read()

    # /ufsu expects query param fsz=<filesize> (WebUI uses /ufsu?fsz=...)
    if "fsz" in merged_query and (merged_query.get("fsz") == ""):
        merged_query["fsz"] = str(len(data))

    boundary = "----berryupload" + os.urandom(8).hex()

    fields: Dict[str, str] = {}
    # Some Tasmota builds accept additional fields; keeping empty by default.

    inferred_field_name = "u2" if endpoint_path.rstrip("/") == "/ufsu" else "ufile"
    field_name = (file_field_name or "").strip() or inferred_field_name

    files = [(field_name, dest_name, data, _guess_mime_type(dest_name))]

    body = _multipart_form_data(fields, files, boundary)

    req = urllib.request.Request(url=url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_body = resp.read()
            try:
                body_text = resp_body.decode("utf-8", errors="replace")
            except Exception:
                body_text = repr(resp_body[:200])
            return UploadResult(
                url=url,
                status=getattr(resp, "status", 200),
                body_text=body_text,
            )
    except urllib.error.HTTPError as e:
        err_body = e.read() if hasattr(e, "read") else b""
        body_text = err_body.decode("utf-8", errors="replace")
        return UploadResult(url=url, status=e.code, body_text=body_text)
    except urllib.error.URLError as e:
        return UploadResult(url=url, status=0, body_text=str(e))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a local Berry (.be) file to a Tasmota device filesystem (UFS/LittleFS).\n\n"
            "Note: This uses the same upload mechanism as the WebUI 'Manage File System' page.\n"
            "Default endpoint is /ufsu?fsz= (as used by some Tasmota builds)."
        )
    )
    parser.add_argument("--host", required=True, help="Tasmota IP/hostname, e.g. 192.168.0.94")
    parser.add_argument("--port", type=int, default=80, help="HTTP port (default: 80)")
    parser.add_argument(
        "--https",
        action="store_true",
        default=False,
        help="Use HTTPS (only if your Tasmota build supports it)",
    )
    parser.add_argument(
        "--endpoint",
        default="/ufsu?fsz=",
        help="Upload endpoint path (default: /ufsu?fsz=)",
    )
    parser.add_argument(
        "--field",
        default=None,
        help=(
            "Multipart file field name. If omitted, auto-detects: "
            "'/ufsu' -> 'u2', otherwise 'ufile'."
        ),
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Local .be file to upload (tip: in VS Code tasks use ${file})",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Destination filename on device (default: basename of --file)",
    )
    parser.add_argument("--user", default="", help="WebUI username (optional)")
    parser.add_argument("--password", default="", help="WebUI password (optional)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Request timeout in seconds")

    args = parser.parse_args(argv)

    file_path = os.path.abspath(args.file)
    if not os.path.isfile(file_path):
        print(f"ERROR: File not found: {file_path}", file=sys.stderr)
        return 2

    dest_name = args.dest or os.path.basename(file_path)

    result = upload_file_to_tasmota_ufs(
        host=args.host,
        port=args.port,
        use_https=bool(args.https),
        endpoint=args.endpoint,
        file_path=file_path,
        dest_name=dest_name,
        user=args.user,
        password=args.password,
        timeout_s=float(args.timeout),
        file_field_name=args.field,
    )

    if 200 <= result.status < 300:
        print(f"Uploaded '{file_path}' as '{dest_name}'")
        print(f"URL: {result.url}")
        # Tasmota often responds with an HTML page; show only a small preview.
        preview = result.body_text.strip().replace("\r", "")
        #if preview:
        #    print("Response (first 30000 chars):")
        #    print(preview[:30000])
        if "Successful" in preview:
            print("Upload successful.")
            return 0
        return 1

    if result.status == 0:
        print("Upload failed (connection error)")
    else:
        print(f"Upload failed (HTTP {result.status})")
    print(f"URL: {result.url}")
    body = (result.body_text or "").strip().replace("\r", "")
    if body:
        print("Response (first 600 chars):")
        print(body[:6000])
    print(
        "\nTroubleshooting:\n"
        "- Ensure your Tasmota build has UFS enabled and Webserver is on.\n"
        "- If upload says 'No file selected', your endpoint likely expects a different form field name (try --field u2).\n"
        "- If the browser uses /ufsu?fsz=, keep that as --endpoint; this script auto-fills fsz with the file size.\n"
        "- If authentication is enabled, pass --user/--password."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
