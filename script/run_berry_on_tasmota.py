import argparse
import json
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class CommandResult:
    url: str
    status: int
    body_text: str
    json_body: Optional[Any]


def _quote_berry_string(value: str) -> str:
    # Berry supports both single and double quoted strings.
    if "'" not in value:
        return "'" + value + "'"
    escaped = value.replace('"', "\\\"")
    return '"' + escaped + '"'


def send_tasmota_command(
    *,
    host: str,
    port: int,
    use_https: bool,
    cm_path: str,
    command: str,
    user: str,
    password: str,
    timeout_s: float,
) -> CommandResult:
    scheme = "https" if use_https else "http"

    if not cm_path.startswith("/"):
        cm_path = "/" + cm_path

    query = {"cmnd": command}
    if user:
        query["user"] = user
    if password:
        query["password"] = password

    url = f"{scheme}://{host}:{port}{cm_path}?{urllib.parse.urlencode(query)}"

    req = urllib.request.Request(url=url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            parsed: Optional[Any] = None
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            return CommandResult(
                url=url,
                status=getattr(resp, "status", 200),
                body_text=text,
                json_body=parsed,
            )
    except urllib.error.HTTPError as e:
        err_body = e.read() if hasattr(e, "read") else b""
        text = err_body.decode("utf-8", errors="replace")
        parsed: Optional[Any] = None
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        return CommandResult(url=url, status=e.code, body_text=text, json_body=parsed)
    except urllib.error.URLError as e:
        return CommandResult(url=url, status=0, body_text=str(e), json_body=None)


def fetch_tasmota_console(
    *,
    host: str,
    port: int,
    use_https: bool,
    console_path: str,
    lines: int,
    user: str,
    password: str,
    timeout_s: float,
) -> list[str]:
    scheme = "https" if use_https else "http"

    if not console_path.startswith("/"):
        console_path = "/" + console_path

    query: dict[str, str] = {"c2": str(int(lines))}
    if user:
        query["user"] = user
    if password:
        query["password"] = password

    url = f"{scheme}://{host}:{port}{console_path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()

    text = raw.decode("utf-8", errors="replace")
    all_lines = [ln.rstrip("\r").rstrip() for ln in text.split("\n") if ln.strip()]

    def looks_like_log_line(line: str) -> bool:
        return len(line) >= 8 and line[2] == ":" and line[5] == ":"

    # /cs payload often starts with a short non-log header like "196}11}1".
    while all_lines and not looks_like_log_line(all_lines[0]):
        all_lines.pop(0)

    # Coalesce wrapped lines into stable log entries.
    entries: list[str] = []
    for line in all_lines:
        if looks_like_log_line(line) or not entries:
            entries.append(line)
        else:
            # Continuation line (often a long JSON that wrapped). Preserve it.
            entries[-1] = entries[-1] + "\n" + line

    return entries


def _extract_between_markers(entries: list[str], *, begin: str, end: str) -> list[str]:
    begin_index = -1
    for idx, entry in enumerate(entries):
        if begin in entry:
            begin_index = idx

    if begin_index < 0:
        return []

    for idx in range(begin_index + 1, len(entries)):
        if end in entries[idx]:
            return entries[begin_index + 1 : idx]

    return []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute a Berry script already uploaded to a Tasmota device filesystem.\n\n"
            "This sends a Tasmota console command via HTTP: Br load(<filename>).\n"
            "(Berry load() loads and runs the file from UFS.)"
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
        "--cm-path",
        default="/cm",
        help="Tasmota command endpoint path (default: /cm)",
    )
    parser.add_argument(
        "--capture-console",
        action="store_true",
        default=False,
        help=(
            "Capture Berry output from the web console by reading /cs?c2=... before and after running. "
            "(This captures print() output and other console log lines.)"
        ),
    )
    parser.add_argument(
        "--console-path",
        default="/cs",
        help="Web console endpoint path (default: /cs)",
    )
    parser.add_argument(
        "--console-lines",
        type=int,
        default=300,
        help="How many console lines to fetch each poll (default: 300)",
    )
    parser.add_argument(
        "--console-timeout",
        type=float,
        default=3.0,
        help="Max seconds to wait for new console output (default: 3.0)",
    )
    parser.add_argument(
        "--console-poll",
        type=float,
        default=0.25,
        help="Seconds between console polls (default: 0.25)",
    )
    parser.add_argument(
        "--file",
        help=(
            "Filename on the device to execute, e.g. rest.be or /rest.be. "
            "This will run: Br load(<file>)."
        ),
    )
    parser.add_argument(
        "--expr",
        help=(
            "Optional: run raw Berry code instead of --file. "
            "Example: --expr \"print('hello')\""
        ),
    )
    parser.add_argument("--user", default="", help="WebUI username (optional)")
    parser.add_argument("--password", default="", help="WebUI password (optional)")
    parser.add_argument("--timeout", type=float, default=10.0, help="Request timeout in seconds")

    args = parser.parse_args(argv)

    if bool(args.file) == bool(args.expr):
        print("ERROR: Provide exactly one of --file or --expr", file=sys.stderr)
        return 2

    if args.file:
        berry_code = f"load({_quote_berry_string(args.file)})"
    else:
        berry_code = str(args.expr)

    tasmota_command = f"Br {berry_code}"

    begin_marker = ""
    end_marker = ""
    if args.capture_console:
        run_id = uuid.uuid4().hex
        begin_marker = f"COPILOT_BEGIN {run_id}"
        end_marker = f"COPILOT_END {run_id}"
        # Best-effort: place a marker in the console log so we can extract only
        # the relevant output lines later.
        send_tasmota_command(
            host=args.host,
            port=int(args.port),
            use_https=bool(args.https),
            cm_path=str(args.cm_path),
            command=f"Br print({_quote_berry_string(begin_marker)})",
            user=str(args.user),
            password=str(args.password),
            timeout_s=float(args.timeout),
        )

    result = send_tasmota_command(
        host=args.host,
        port=int(args.port),
        use_https=bool(args.https),
        cm_path=str(args.cm_path),
        command=tasmota_command,
        user=str(args.user),
        password=str(args.password),
        timeout_s=float(args.timeout),
    )

    if args.capture_console and begin_marker and end_marker and (200 <= result.status < 300):
        # Add an end marker after the run, then extract everything between
        # the markers from the web console buffer.
        send_tasmota_command(
            host=args.host,
            port=int(args.port),
            use_https=bool(args.https),
            cm_path=str(args.cm_path),
            command=f"Br print({_quote_berry_string(end_marker)})",
            user=str(args.user),
            password=str(args.password),
            timeout_s=float(args.timeout),
        )

        deadline = time.monotonic() + float(args.console_timeout)
        captured: list[str] = []
        while time.monotonic() < deadline and not captured:
            time.sleep(float(args.console_poll))
            try:
                entries = fetch_tasmota_console(
                    host=args.host,
                    port=int(args.port),
                    use_https=bool(args.https),
                    console_path=str(args.console_path),
                    lines=int(args.console_lines),
                    user=str(args.user),
                    password=str(args.password),
                    timeout_s=float(args.timeout),
                )
            except Exception:
                entries = []

            captured = _extract_between_markers(entries, begin=begin_marker, end=end_marker)

        if captured:
            cleaned = [entry for entry in captured if '"Br":"nil"' not in entry]
            if cleaned:
                print("\n".join(cleaned))
                return 0

    if result.status == 0:
        print("Request failed (connection error)")
        print(f"URL: {result.url}")
        print(result.body_text)
        return 1

    if not (200 <= result.status < 300):
        print(f"Request failed (HTTP {result.status})")
        print(f"URL: {result.url}")
        if result.body_text.strip():
            print(result.body_text.strip()[:6000])
        return 1

    if result.json_body is not None:
        print(json.dumps(result.json_body, indent=2, ensure_ascii=False))
    else:
        print(result.body_text.strip())

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
