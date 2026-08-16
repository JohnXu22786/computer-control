"""Demo runner: exercises the plugin through the same protocol a harness uses.

By default it starts the server in dry-run mode (nothing touches the real
desktop). Pass --live to use the real Windows driver - only do that on a
machine you are prepared to have moved around, and keep the emergency hotkey
(ctrl+alt+f12) in mind.

Usage:
    python examples/demo.py            # dry-run rehearsal
    python examples/demo.py --live     # real desktop (input injection!)
"""

import argparse
import os
import sys
import time

# make the plugin package importable when running from a checkout
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description="computer-control demo runner")
    parser.add_argument("--live", action="store_true",
                        help="use the real Windows driver (injects real input)")
    parser.add_argument("--config", default=None, help="path to a config file")
    args = parser.parse_args()

    command = [sys.executable, "-m", "computer_control", "serve"]
    if args.config:
        command += ["--config", args.config]

    from computer_control.client import StdioClient, ClientError

    client = StdioClient(command=command)
    try:
        config = {"platform": {"name": "windows" if args.live else "dry-run"}}
        if args.live:
            config["safety"] = {"confirm_threshold": "moderate"}

        print("== session.start ==")
        result = client.call("session.start", config)
        print("   state:", result["result"]["state"], "| capabilities:", result["result"]["capabilities"])

        print("== tools.list ==")
        tools = client.call("tools.list")["result"]["tools"]
        available = [t["name"] for t in tools if t["available"]]
        print("   %d tools declared, %d available" % (len(tools), len(available)))
        print("   unavailable:", [t["name"] for t in tools if not t["available"]] or "none")

        print("== screen.capture (scale 0.5, jpeg) ==")
        cap = client.call("tools.call", {"tool": "screen.capture",
                                         "arguments": {"format": "jpeg", "scale": 0.5}})
        if cap["ok"]:
            print("   frame %s: %dx%d, %d bytes" % (cap["result"]["frame"],
                                                    cap["result"]["width"],
                                                    cap["result"]["height"],
                                                    cap["result"]["bytes"]))
        else:
            print("   FAILED:", cap["error"])

        print("== a11y.snapshot (skeleton) ==")
        snap = client.call("tools.call", {"tool": "a11y.snapshot",
                                          "arguments": {"level": "skeleton"}})
        if snap["ok"]:
            print("   snapshot %s: %d nodes (truncated=%s)"
                  % (snap["result"]["snapshot_id"], snap["result"]["node_count"],
                     snap["result"]["truncated"]))
        else:
            print("   unavailable:", snap["error"]["message"])

        print("== batch.execute (3 benign steps) ==")
        batch = client.call("tools.call_batch", {"items": [
            {"tool": "wait.pause", "arguments": {"ms": 50}},
            {"tool": "pointer.move", "arguments": {"x": 400, "y": 300}},
            {"tool": "screen.capture", "arguments": {"scale": 0.25}},
        ], "continue_on_error": True})
        if batch["ok"]:
            result = batch["result"]
            if result.get("status") == "awaiting_confirmation":
                print("   batch requires confirmation (live mode): %s" % result["request_id"])
                client.call("session.confirm", {"request_id": result["request_id"], "approve": True})
                time.sleep(0.3)
                batch = client.call("system.status")  # refresh state after execution
                print("   approved; state:", batch["result"]["state"])
            else:
                print("   status:", result["status"],
                      "| items:", [(i["ok"], (i.get("error") or {}).get("code")) for i in result["items"]])
        else:
            print("   FAILED:", batch["error"])

        print("== confirmation flow demo ==")
        combo = client.call("tools.call", {"tool": "keyboard.combo",
                                           "arguments": {"keys": ["win", "r"]}})
        if combo["ok"] and combo["result"].get("status") == "awaiting_confirmation":
            request_id = combo["result"]["request_id"]
            print("   high-risk action awaiting confirmation: %s" % request_id)
            print("   -> denying (no real keyboard input in a demo)")
            client.call("session.confirm", {"request_id": request_id, "approve": False})
            time.sleep(0.2)
        else:
            print("   no confirmation triggered (risk gating not hit):", combo)

        print("== system.status ==")
        status = client.call("system.status")["result"]
        print("   state:", status["state"], "| actions:", status["action_count"],
              "| surface:", status["surface"]["display_width_px"], "x",
              status["surface"]["display_height_px"])

        print("== session.stop ==")
        client.call("session.stop")
        print("done.")
        return 0
    except ClientError as exc:
        print("protocol error:", exc)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
