#!/usr/bin/env python3
"""Send WxPusher notification. Args: apptoken uid title body"""
import sys, requests

if len(sys.argv) < 5:
    print("Usage: send_wxpusher.py <apptoken> <uid> <title> <body>")
    sys.exit(1)

resp = requests.post("https://wxpusher.zjiecode.com/api/send/message", json={
    "appToken": sys.argv[1],
    "content": f"{sys.argv[3]}\n\n{sys.argv[4]}",
    "contentType": 1,
    "uids": [sys.argv[2]],
}, timeout=10)
print(f"告警已发送: HTTP {resp.status_code}")
