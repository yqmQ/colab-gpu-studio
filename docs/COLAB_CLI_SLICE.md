# Colab CLI 脚本切片

官方 CLI 源码核对自 `googlecolab/google-colab-cli` 提交 `d2d97794941723ab806c0f3642416691a951ec7d`（2026-11-04）。PyPI 包名是 `google-colab-cli`，命令名是 `colab`，需要 Python 3.12+。上游声明只支持 **Linux 和 macOS**，不支持 Windows。

本切片默认关闭，不把 `google-colab-cli` 加进 Studio 的依赖，测试也不会安装它或申请 GPU。

## 上游语义（以源码为准，不轻信 README）

- 全局参数是 `--auth {oauth2,adc}` 和 `-c/--client-oauth-config`。README 写默认 `adc`，但 `cli.py` 的默认值是 `AuthProvider.OAUTH2`。无界面服务如果走 oauth2，会在 `input()` 上等待粘贴授权码，因此适配器固定传 `--auth adc`。
- `oauth2` 打印 “To authorize colab-cli, visit this URL” 后等待授权码。`adc` 失败时打印 `gcloud auth application-default login` 以及 colaboratory 等 scope。适配器把这些原文当作阻塞说明返回，不会假装已经登录。
- `colab run` 的 `finally` 会调用 `_teardown`。`_teardown` 吞掉 `unassign` 的全部异常，然后仍然打印 `Session terminated.`。这句话不能当作运行时已经释放。未知 `--gpu` 还会被映射成 A100。所以适配器不用 `colab run`。
- 实际调用只有固定参数：`new -s <studio-任务>`、`exec -s <同名> -f <任务目录里的 script.py> --timeout <整数>`、`stop -s <同名>`、`sessions`、`usage`。加速器只允许省略（CPU）或 `T4|L4|G4|H100|A100`。
- `colab sessions` 会列出账号上的全部分配；没有本地状态的显示为 `[?]`。停止时始终带上我们生成的 `-s`，不会省略会话名，因此不会选中“唯一的另一个会话”。
- 释放是否成功，要看随后的 `sessions` 输出里不再出现本任务的 `[studio-…]` 和已记录的 endpoint。做不到这一点就标成残留，并只允许重试这个会话。
- `colab usage` 打印 `Current balance`、`Usage rate`、`Active assignments`。解析失败就显示不可用，不填 1000 CU，也不编造单价。

## 环境

```bash
# Linux 或 macOS，Python 3.12+
uv tool install google-colab-cli
# 或 pip install google-colab-cli

# 服务器上准备 ADC，不要指望 Studio 替你完成交互式粘贴：
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
```

Studio 进程：

```bash
COLAB_CLI_MODE=off          # 默认。fake 只用替身；real 才会调用真的 colab
COLAB_CLI_BIN=              # real 时可留空，用 PATH 里的 colab
COLAB_CLI_ALLOW_REAL=0      # 与 GPU_REAL_ENABLED=1 必须同时为 1 才允许 real
GPU_REAL_ENABLED=0          # 保持 0 时，即使 MODE=real 也不会申请运行时
```

会话状态写在 `DATA_DIR/colab-cli/sessions.json`，不使用操作者家目录里的 `~/.config/colab-cli/sessions.json`，避免停掉无关会话。该文件含有 CLI 的运行时代理令牌，接口和日志会打码，不要把它提交到仓库。

Linux 上官方 CLI 还会访问密钥环。无界面环境更适合 ADC。macOS 同样只支持上面的命令；Studio 不会打开公网隧道。

## 没有做端到端验证的部分

没有安装真实 `colab`，没有登录 Google，没有申请 CPU/GPU，没有读取真实 CU。测试只用 `scripts/colab_cli_fake.py`。`colab run` 的自动释放可靠性只根据源码判断，没有在真实 VM 上复现。
