"""诊断：对比三个账号调用 get_workflow_json 的真实返回，验证 wf_exists 判断是否准确"""
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runninghub_sdk import RunningHubClient

WORKFLOW_ID = "2087348783733628930"  # 坏节点动作迁移工作流

accounts = [
    {"phone": "18035142166", "password": "zhh235689"},
    {"phone": "18900279795", "password": "zhh235689"},
    {"phone": "18159181026", "password": "scx231s231scx"},
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(BASE_DIR, "rh_profiles")
os.makedirs(PROFILES_DIR, exist_ok=True)

for acc in accounts:
    phone = acc["phone"]
    print(f"\n========== 账号: {phone} ==========")
    try:
        # Step 1: 登录
        token_cache_file = os.path.join(PROFILES_DIR, f"rh_{phone}_token.json")
        client = RunningHubClient.from_login(phone, acc["password"], token_cache=token_cache_file)

        # Step 2: 获取 API Key
        api_keys = client.get_user_api_key(user_id="")
        real_api_key = api_keys.shared_api.api_key or api_keys.normal_api_key
        print(f"  API Key: {real_api_key[:10]}...")

        # Step 3: 查询工作流 JSON - 看完整返回内容
        client2 = RunningHubClient(api_key=real_api_key)
        try:
            wf_json = client2.get_workflow_json(workflow_id=WORKFLOW_ID)
            # 截取关键内容（避免过长）
            content = str(wf_json)
            print(f"  响应长度: {len(content)} 字符")
            print(f"  响应前 800 字符: {content[:800]}")
            print(f"  是否包含 285 节点: {'285' in content}")
            print(f"  是否包含 600 节点: {'600' in content}")
            print(f"  是否包含 '失败' / 'error' / '不存在': {('失败' in content) or ('error' in content.lower()) or ('不存在' in content)}")
        except Exception as e:
            print(f"  ✗ get_workflow_json 抛异常: {type(e).__name__}: {e}")

        # Step 4: 调用 accountStatus
        try:
            acc_status = client2.get_account_status()
            print(f"  账号状态 - 当前任务数: {acc_status.current_task_counts}, 余额: {acc_status.remain_money}")
        except Exception as e:
            print(f"  ✗ get_account_status 失败: {e}")

    except Exception as e:
        print(f"  ✗ 账号处理失败: {type(e).__name__}: {e}")
