"""Low-frequency HTTP checks throughout each lab experiment."""
import json
import os
import time
import urllib.request

http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
expected_nodes = int(os.environ.get('TE_EXPECTED_NODES', '122'))
while True:
    sample = {'time': time.time(), 'ok': True}
    try:
        with http.open('http://127.0.0.1:8080/', timeout=3) as response:
            assert response.status == 200 and b'<div id="root">' in response.read()
        with http.open('http://127.0.0.1:8080/api/instance/', timeout=3) as response:
            body = json.load(response)
            assert body['code'] == 0 and len(body['data']) == expected_nodes
    except Exception as exc:
        sample.update(ok=False, error=str(exc))
    print(json.dumps(sample), flush=True)
    time.sleep(5)
