import urllib.request, urllib.error, sys
url = sys.argv[1]
req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as r:
        print("status", r.status, r.read().decode()[:200])
except urllib.error.HTTPError as e:
    print("status", e.code, e.read().decode()[:200])
except Exception as e:
    print(type(e).__name__, e)
