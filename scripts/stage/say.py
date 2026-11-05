#!/usr/bin/env python3
"""Send ordinary text through JWT ingress, the real queue, and app polling."""

import argparse
import json
import sys
import time
import uuid

from stack import request, runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--thread", default="stage")
    args = parser.parse_args()
    started = time.monotonic()
    token = runtime("token").strip()
    threads = request("/api/v1/chat/threads/", token=token)["threads"]
    thread = next((item for item in threads if item["title"] == args.thread), None)
    if thread is None:
        thread = request("/api/v1/chat/threads/", {"title": args.thread}, token=token)
    client_id = uuid.uuid4().hex
    result = request(
        "/api/v1/chat/", {"text": args.message, "thread_id": thread["id"], "client_msg_id": client_id}, token=token
    )
    while result.get("status") not in ("ready", "error", "failed") and time.monotonic() - started < 900:
        time.sleep(1.5 if time.monotonic() - started < 180 else 5)
        result = request(f"/api/v1/chat/messages/{client_id}/", token=token)
    print(
        json.dumps(
            {
                "reply_text": result.get("reply_text", ""),
                "quick_replies": result.get("quick_replies", []),
                "seconds_taken": round(time.monotonic() - started, 2),
                "status": result.get("status", "timeout"),
                "error": result.get("error", ""),
                "source": result.get("source"),
                "thread_id": thread["id"],
                "client_msg_id": client_id,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.get("status") == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
