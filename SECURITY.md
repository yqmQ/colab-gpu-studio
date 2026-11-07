# 安全说明

- **默认关闭真机**：`GPU_REAL_ENABLED` 默认是 `0`，没有你本人配置 Google OAuth、预算费率并完成工作流审核前，程序不会发起任何 Colab GPU 请求。
- **保护好密钥**：请勿在公开 Issue、PR 或聊天群里粘贴 `.env`、Google OAuth 客户端 JSON、令牌或数据库文件。一旦怀疑密钥泄露，请立刻去 Google Cloud Console 撤销重发，光删本地文件没用。
- **仅限个人自用**：本项目是单人工作台，没有做多用户权限隔离，请放在受控的本地环境或带鉴权的私人 VPS 上运行。
- **登录会话有限期**：浏览器会话默认 12 小时。`SESSION_TTL_SECONDS` 可在 60 到 2592000 秒之间调整（2592000 为 30 天，是上限，不是无限期）。持久副本是 `HttpOnly`、`SameSite=lax` 的 `studio_session` Cookie；HTTPS 站点同时带 `Secure`。写请求仍要 Bearer，并继续校验 Origin。退出登录会使已签发的会话失效。
