# 女装带货独立封装

只含女装带货 5 步工作室。RPA 有头模式。

## 启动

任意目录双击 `start.bat`，或把 bat 的完整路径丢给 cmd 都能起（脚本会切到自身所在目录）：

```bat
start.bat
D:\任意拷贝位置\nzdh\start.bat
```

页面：http://127.0.0.1:8091/womenswear  
健康检查：http://127.0.0.1:8091/health

首次在新机器：

```bat
pip install -r requirements.txt
python -m playwright install chromium
```

## 密钥

只保留本工作流用到的：

| 用途 | 模型 | 配置 |
|------|------|------|
| Step2 平铺 / Step3 橱窗参考图 | gpt-image-2 | `IMAGE2_API_KEY` |
| upload 识别是否含模特、发布文案 | mimo-v2.5 | `MIMO_API_KEY` |
| upload 从模仿视频挑模特帧 | gpt-5.6-sol | `OPENAI_API_KEY` |

RunningHub 走浏览器 RPA + `rh_accounts.json` 账号，不用其它产品线的模型 Key。

## e2e

从任意工作目录：

```bat
python e2e_check.py
```
