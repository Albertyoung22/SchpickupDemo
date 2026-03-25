import urllib.request
import json

data = json.dumps([]).encode('utf-8') # Initialize with empty list
req = urllib.request.Request("https://jsonblob.com/api/jsonBlob", data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
with urllib.request.urlopen(req) as r:
    print(r.headers.get("Location"))
