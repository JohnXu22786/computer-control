"""Command line interface: serve (stdio), serve over HTTP, diagnostics, listing."""

from __future__ import annotations

import argparse
import json
import sys

from computer_control.config import load_config
from computer_control.version import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="computer-control",
        description="Desktop control plugin for the dsh harness (version %s)." % __version__,
    )
    parser.add_argument("--config", help="path to a JSON configuration file (or set COMPUTER_CONTROL_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the plugin server (stdio by default)")
    serve.add_argument("--config", help="path to a JSON configuration file (or set COMPUTER_CONTROL_CONFIG)")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    check = sub.add_parser("check", help="diagnose the environment: platform, backends, capture, UIA")
    check.add_argument("--config", help="path to a JSON configuration file (or set COMPUTER_CONTROL_CONFIG)")
    list_parser = sub.add_parser("list", help="print the declared tools and events")
    list_parser.add_argument("--config", help="path to a JSON configuration file (or set COMPUTER_CONTROL_CONFIG)")
    list_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _config(args):
    if args.config:
        return load_config(args.config)
    from computer_control.config import load_config as load

    return load(None)


def cmd_serve(args) -> int:
    from computer_control.protocol import Router
    from computer_control.server import log, run_http

    try:
        cfg = _config(args)
    except Exception as exc:
        print("config: invalid (%s)" % exc)
        return 2
    log("starting computer-control %s (platform=%s)" % (__version__, cfg.platform.name))

    # Sessions are created lazily by the router; its event sink is installed
    # by the transport, so events reach the wire automatically. The config
    # file becomes the base for every session.start.
    router = Router(base_config=cfg.as_dict())
    if args.transport == "http":
        run_http(router, args.host, args.port)
    else:
        _wire_stdio(router)
    return 0


def _wire_stdio(router) -> None:
    from computer_control.server import serve_stdio

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    serve_stdio(router, stdin, stdout)


def cmd_check(args) -> int:
    from computer_control.drivers.dummy import make_png_1x1
    from computer_control.keys import parse_hotkey, parse_key

    issues = []  # fatal problems: required pieces missing or broken
    notes = []  # optional pieces not installed (informational)
    out = []
    out.append("computer-control %s environment check" % __version__)
    out.append("python: %s" % sys.version.split()[0])
    out.append("platform: %s" % sys.platform)
    try:
        cfg = _config(args)
    except Exception as exc:
        print("config: invalid (%s)" % exc)
        return 2
    out.append("config platform: %s" % cfg.platform.name)

    try:
        import PIL

        out.append("Pillow: available (%s)" % PIL.__version__)
    except ImportError:
        out.append("Pillow: MISSING (required)")
        issues.append("Pillow")

    for package, label in (("mss", "mss (optional, faster captures)"),
                           ("comtypes", "comtypes (optional, UIA)"),
                           ("keyboard", "keyboard (optional, non-Windows hotkeys)")):
        try:
            __import__(package)
            out.append("%s: available" % label)
        except ImportError:
            out.append("%s: not installed" % label)
            notes.append(label)

    try:
        probe = make_png_1x1()
        out.append("synthetic PNG probe: %d bytes" % len(probe))
    except Exception as exc:
        out.append("synthetic PNG probe: failed (%s)" % exc)
        issues.append("png probe")

    from computer_control.drivers import create_driver

    try:
        driver = create_driver(cfg, log=lambda m: None)
    except Exception as exc:
        out.append("driver: cannot create (%s)" % exc)
        issues.append("driver")
        print("\n".join(out))
        print("\nISSUES FOUND")
        return 2

    try:
        info = getattr(driver, "desktop_info", lambda: None)()
        if info:
            vs = info["virtual_screen"]
            out.append("dpi mode: %s" % info["dpi_mode"])
            out.append("virtual desktop: x=%d y=%d %dx%d" % (vs["x"], vs["y"], vs["width"], vs["height"]))
            out.append("capture backend: %s" % info["capture_backend"])
        out.append("capabilities: %s" % json.dumps(driver.capabilities))
        if driver.capabilities.get("capture"):
            try:
                payload = driver.capture((0, 0, 64, 64), canvas_width=64)
                out.append("capture probe: %s %dx%d (%d bytes)" % (payload.format, payload.width, payload.height, len(payload.bytes)))
            except Exception as exc:
                out.append("capture probe: failed (%s)" % exc)
                issues.append("capture")
        if driver.capabilities.get("a11y"):
            try:
                result = driver.a11y_snapshot({"level": "skeleton"})
                out.append("a11y probe: %d nodes" % result.get("node_count", -1))
            except Exception as exc:
                out.append("a11y probe: failed (%s)" % exc)
                issues.append("a11y")
    finally:
        driver.close()

    try:
        keys = parse_hotkey(cfg.safety.emergency_hotkey)
        out.append("emergency hotkey: %s (keys: %s)" % (cfg.safety.emergency_hotkey or "(disabled)", [parse_key(k) for k in keys] if keys else "n/a"))
    except Exception as exc:
        out.append("emergency hotkey: invalid (%s)" % exc)
        issues.append("hotkey")

    out.append("")
    if issues:
        out.append("ISSUES FOUND: %s" % ", ".join(sorted(set(issues))))
        print("\n".join(out))
        return 1
    out.append("OK")
    print("\n".join(out))
    return 0


def cmd_list(args) -> int:
    from computer_control.manifest import manifest_tools, manifest_events

    if args.json:
        print(json.dumps({"tools": manifest_tools(), "events": manifest_events()}, ensure_ascii=False, indent=2))
        return 0
    print("Tools:")
    for entry in manifest_tools():
        print("  %-22s %s" % (entry["name"], entry["summary"]))
    print("Events:")
    for entry in manifest_events():
        print("  %-32s %s" % (entry["name"], entry["summary"]))
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "list":
        return cmd_list(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
