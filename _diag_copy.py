"""诊断：测试 copy_workflow 接口在三个账号上的行为"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runninghub_sdk import RunningHubClient, RunningHubError

WORKFLOW_ID = "2087348783733628930"  # 坏节点动作迁移工作流（原 ID）

accounts = [
    {"phone": "18900279795", "password": "zhh235689", "has_wf": True},
    {"phone": "18159181026", "password": "scx231s231scx", "has_wf": False},
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(BASE_DIR, "rh_profiles")
os.makedirs(PROFILES_DIR, exist_ok=True)

for acc in accounts:
    phone = acc["phone"]
    print(f"\n========== 账号: {phone} (预期 has_wf={acc['has_wf']}) ==========")
    try:
        # 登录拿 token
        token_cache_file = os.path.join(PROFILES_DIR, f"rh_{phone}_token.json")
        client = RunningHubClient.from_login(phone, acc["password"], token_cache=token_cache_file)

        # 获取 API Key（顺便拿余额）
        api_keys = client.get_user_api_key(user_id="")
        real_api_key = api_keys.shared_api.api_key or api_keys.normal_api_key
        print(f"  API Key: {real_api_key[:10]}...")

        # 用登录后的 client（带 token）调 copy_workflow
        try:
            resp = client.copy_workflow(workflow_id=WORKFLOW_ID, copy_mode=1)
            print(f"  ✅ copy_workflow 成功")
            print(f"     新工作流 ID: {resp.id}")
            print(f"     名称: {resp.name}")
            print(f"     owner: {resp.owner}")
            print(f"     nodeCount: {resp.nodeCount}")
            print(f"     workflowState: {resp.workflowState}")
            print(f"     status: {resp.status}")
            print(f"     webappId: {resp.webappId}")
        except RunningHubError as e:
            print(f"  ⚠️  copy_workflow 报错: code={e.code} message={e}")
        except Exception as e:
            print(f"  ⚠️  其他异常: {type(e).__name__}: {e}")

    except Exception as e:
        print(f"  ✗ 账号处理失败: {type(e).__name__}: {e}")
