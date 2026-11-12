"""诊断：查询任务 3f58ecf92a99 当前状态"""
import json
import urllib.request

url = "http://127.0.0.1:8091/api/womenswear-studio/status?task_id=3f58ecf92a99"
data = json.loads(urllib.request.urlopen(url, timeout=10).read())
print("status:", data.get("status"))
print("progress:", data.get("progress"))
print("stage:", data.get("stage"))
print("error:", data.get("error"))
print("outputs:", data.get("outputs"))
for log in data.get("logs", [])[-10:]:
    print(f"[{log.get('ts_str')}] {log.get('message')}")
