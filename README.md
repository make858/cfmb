# 此代码提取自天成大佬的1.32V2  感谢大佬
# 效果图
<img width="1255" height="625" alt="截图20260113152621" src="https://github.com/user-attachments/assets/6853939e-68b2-4023-8835-4da40b2a2097" />

# 🚀 Cloudflare Pages 部署完整指南

**修复完成日期**: 2026年1月13日  
**修复版本**: v2.0  
**部署难度**: ⭐⭐ (中等偏易)

---

## 📋 预检查清单

在开始部署前，请确保您已准备好以下内容：

### ✅ 文件准备
- [x] 已获取修改后的 `_worker_cleaned.js` 文件
- [x] 文件完整性已检查
- [x] 备份了原始文件（可选但推荐）

### ✅ Cloudflare 账户
- [x] 已登录 Cloudflare 控制面板
- [x] 拥有域名并配置了 DNS
- [x] 具有 Pages 和 Workers 编辑权限

### ✅ API 凭证
需要以下之一：
- [x] 邮箱 + 全局 API 密钥
- [x] Account ID + API Token

---

## 🔑 获取 Cloudflare 凭证

### 方法 A: 全局 API 密钥（推荐新手）

**步骤**:
1. 访问 https://dash.cloudflare.com/profile/api-tokens
2. 向下滚动找到 "API Keys" 部分
3. 在 "Global API Key" 旁边点击 "View"
4. 复制整个密钥（通常是 37 个字符）

**所需信息**:
```
CF_EMAIL = 您的 Cloudflare 邮箱
CF_KEY   = 复制的全局 API 密钥
```

### 方法 B: API Token（更安全，推荐）

**步骤**:
1. 访问 https://dash.cloudflare.com/profile/api-tokens
2. 点击 "Create Token"
3. 在 "Create Custom Token" 下点击 "Get started"
4. 配置权限：
   ```
   Permissions:
   - Account → Analytics → Read
   - Account → Account Filtering Rules → Read
   ```
5. 选择账户或"All accounts"
6. 点击 "Continue to summary"
7. 点击 "Create Token"
8. 复制生成的 Token

**获取 Account ID**:
1. 访问任何域名的 Cloudflare 控制面板
2. 检查 URL: `https://dash.cloudflare.com/abc123xyz/overview`
3. `abc123xyz` 就是您的 Account ID

**所需信息**:
```
CF_ID    = Account ID（从 URL 中获取）
CF_TOKEN = 复制的 API Token
```

---

## 📝 环境变量配置
<img width="1141" height="468" alt="截图20260113152533" src="https://github.com/user-attachments/assets/8e8dd605-2055-44ed-a735-56ef576b6ba4" />

### 在 Cloudflare Workers 中设置环境变量

**步骤**:
1. 访问 https://dash.cloudflare.com/
2. 选择 "Workers and Pages"
3. 找到您的 Worker（或创建新的）
4. 点击 Worker 名称进入设置
5. 选择 "Settings" → "Environment variables"
6. 点击 "Add variable"

### 配置示例

#### 选项 A: 全局密钥方式
```
Variable name: CF_EMAIL
Value: your-email@example.com

Variable name: CF_KEY
Value: (您的全局 API 密钥)

Variable name: WEB_PASSWORD
Value: (自定义登录密码)
```

#### 选项 B: API Token 方式
```
Variable name: CF_ID
Value: (您的 Account ID)

Variable name: CF_TOKEN
Value: (您的 API Token)

Variable name: WEB_PASSWORD
Value: (自定义登录密码)
```

#### 选项 C: 多账户配置
```
Variable name: CF_ACCOUNTS_1
Value: {"email":"account1@example.com","key":"api-key-1"}

Variable name: CF_ACCOUNTS_2
Value: {"id":"account-id-2","token":"api-token-2"}

Variable name: WEB_PASSWORD
Value: (自定义登录密码)
```

**重要**: 点击 "Add variable" 后，确保点击 "Save and deploy" 保存！

---

## 📦 部署 Worker 代码

### 步骤 1: 准备代码
1. 打开 `_worker_cleaned.js` 文件
2. 全选所有内容（Ctrl+A）
3. 复制（Ctrl+C）

### 步骤 2: 在 Cloudflare 中部署

**方式 A: 通过 Cloudflare 控制面板**

1. 访问 https://dash.cloudflare.com/
2. 选择 "Workers and Pages"
3. 如果是新 Worker:
   - 点击 "Create application"
   - 选择 "Create a Worker"
   - 给 Worker 命名（例如 `cloudflare-stats`）
   - 点击 "Create service"

4. 进入 Worker 编辑界面：
   - 在编辑器中删除默认代码
   - 粘贴修改后的 `_worker_cleaned.js` 代码
   - 点击 "Save and deploy"

**方式 B: 通过 Wrangler CLI**

```powershell
# 安装 Wrangler（如未安装）
npm install -g wrangler

# 进入代码目录
cd e:\pm\Sourcecode\cfdataweb

# 登录 Cloudflare
wrangler login

# 部署
wrangler deploy _worker_cleaned.js --name cloudflare-stats
```

---

## 🔗 配置 Pages 路由

### 步骤 1: 进入 Pages 设置

1. 访问 https://dash.cloudflare.com/
2. 选择您的 Pages 项目
3. 进入 "Settings"
4. 选择 "Functions"

### 步骤 2: 配置 Worker 路由

如果已有前端文件：

1. 进入 "Build & deploy"
2. 确保构建命令和输出目录配置正确
3. 进入 "Settings" → "Functions"
4. 配置路由：
   ```
   Pattern: /*
   Function: cloudflare-stats  (您的 Worker 名称)
   ```

如果只有 Worker（无独立前端）：

1. 进入 "Settings"
2. 在项目 URL 下配置 Worker
3. 或将 Worker URL 直接作为站点 URL

---

## ✅ 验证部署

### 步骤 1: 访问站点

1. 打开浏览器
2. 访问您的 Pages 站点 URL（或 Worker 直接 URL）
3. 应该看到登录页面

### 步骤 2: 登录

1. 输入配置的 WEB_PASSWORD
2. 点击登录
3. 应该重定向到主页面

### 步骤 3: 验证数据显示

✅ **成功的表现**:
```
☁️ Cloudflare 统计面板

最后更新: 2026年1月13日 15:30:45 🔵

[🔄 立即刷新] [⏱️ 启用自动刷新] [⏻ 退出登录]

📊 统计概览
┌─────────────┬──────────────┬─────────────┐
│ 账户数: 2   │ 总请求数: 45,234 │ 平均使用率: 22.6% │
└─────────────┴──────────────┴─────────────┘

┌──────────────────────────────────────┐
│ 📧 account@example.com               │
│ Account ID: abc123def456             │
│ 今日请求: 45,234  使用率: 22.6%      │
│ [████████──────────────████] 22.6%   │
│ 45,234 / 200,000                     │
└──────────────────────────────────────┘
```

❌ **问题的表现**:
- 页面空白
- 显示"暂无配置"
- 显示"错误: xxx"
- 浏览器控制台有红色错误

---

## 🔧 故障排查

### 问题 1: 显示"暂无 Cloudflare 账户配置"

**可能原因**: 环境变量未设置或设置错误

**解决步骤**:
1. 打开 Worker 设置
2. 检查 "Environment variables" 部分
3. 确认变量名称完全正确：
   - `CF_EMAIL` (区分大小写)
   - `CF_KEY`
4. 或者：
   - `CF_ID`
   - `CF_TOKEN`
5. 检查值不为空
6. 点击 "Save and deploy"
7. 等待部署完成后重新访问页面

### 问题 2: 显示"错误: 无效的 Cloudflare 凭证"

**可能原因**: API 凭证错误或过期

**解决步骤**:
1. 重新获取 API 密钥：
   - 访问 https://dash.cloudflare.com/profile/api-tokens
   - 生成新的 Global API Key 或 Token
2. 更新环境变量
3. 保存并重新部署
4. 清除浏览器缓存（Ctrl+Shift+Delete）
5. 重新访问页面

### 问题 3: 只显示某个账户的错误

**可能原因**: 该账户的配置有问题

**解决步骤**:
1. 检查错误信息的具体内容
2. 根据错误类型修复：
   - "无法获取 Account ID" → 检查邮箱拼写
   - "GraphQL 错误" → 检查 API Token 权限
   - "无法获取账户数据" → 检查 Account ID 是否正确
3. 更新环境变量
4. 重新部署

### 问题 4: 数据无法刷新

**可能原因**: 网络问题或 API 调用被阻止

**解决步骤**:
1. 按 F12 打开浏览器开发者工具
2. 查看 "Network" 标签
3. 点击页面的"刷新"按钮
4. 观察 `?flag=stats_api` 请求：
   - 状态码应该是 200
   - 响应应该是有效的 JSON
5. 如果有错误，查看 "Console" 标签获取详细错误信息
6. 根据错误信息调试

### 问题 5: 自动刷新不工作

**可能原因**: JavaScript 错误或网络连接问题

**解决步骤**:
1. 按 F12 打开开发者工具
2. 选择 "Console" 标签
3. 查看是否有错误信息
4. 尝试手动点击"刷新"按钮测试
5. 检查网络连接
6. 尝试清除浏览器缓存

---

## 📊 常见配置示例

### 示例 1: 基础配置（全局密钥）

```
CF_EMAIL = john@example.com
CF_KEY = 1234567890abcdef1234567890abcdef1
WEB_PASSWORD = mysecurepassword123
```

### 示例 2: API Token 配置

```
CF_ID = a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
CF_TOKEN = Bearer_token_very_long_string_here
WEB_PASSWORD = mysecurepassword123
```

### 示例 3: 多账户配置

```
CF_ACCOUNTS_1 = {"email":"account1@example.com","key":"key1"}
CF_ACCOUNTS_2 = {"email":"account2@example.com","key":"key2"}
CF_ACCOUNTS_3 = {"id":"account-id-3","token":"token3"}
WEB_PASSWORD = mysecurepassword123
```

---

## 🔐 安全建议

1. **定期轮换 API 密钥**
   - 每季度更新一次 API 凭证

2. **使用强密码**
   - WEB_PASSWORD 至少 12 个字符
   - 包含大小写字母、数字和符号

3. **API Token 权限最小化**
   - 仅分配必要的权限（Account → Analytics → Read）
   - 不要使用 Global API Key 的"全部权限"功能

4. **定期检查访问日志**
   - 如果配置了 KV 存储，检查 ACCESS_LOGS
   - 查看是否有异常访问

5. **启用双因素认证**
   - 在 Cloudflare 账户上启用 2FA

---

## 📞 获取帮助

### 调试信息位置

1. **浏览器控制台** (F12):
   - 打开 "Console" 标签
   - 查看是否有错误或警告信息
   - 这是最快的诊断方式

2. **Network 请求** (F12 → Network):
   - 检查 API 调用的状态码
   - 查看响应数据的内容
   - 确认请求是否被成功发送

3. **Cloudflare Worker 日志**:
   - 访问 Workers 控制面板
   - 查看 "Logs" 部分
   - 查看 console.error 输出的错误信息

### 获取帮助资源

- **Cloudflare 官方文档**: https://developers.cloudflare.com/
- **API 文档**: https://api.cloudflare.com/
- **Cloudflare 社区**: https://community.cloudflare.com/

---

## ✨ 部署完成检查

在部署完成后，请按以下清单验证：

- [ ] 页面成功加载并显示登录界面
- [ ] 使用正确的密码能够登录
- [ ] 登录后看到 Cloudflare 账户信息
- [ ] 账户邮箱或 ID 正确显示
- [ ] 使用量数据显示正确
- [ ] 进度条颜色随着使用率变化（绿→黄→红）
- [ ] 点击"刷新"按钮后数据更新
- [ ] 浏览器控制台没有红色错误
- [ ] 自动刷新功能工作正常（每 5 分钟）

如果所有项目都是✅，说明部署成功！

---

## 🎯 后续配置（可选）

### 启用 Telegram 通知

在环境变量中添加：

```
TG_BOT_TOKEN = your_telegram_bot_token
TG_CHAT_ID = your_chat_id
```

这样当使用率超过 95% 时会自动发送 Telegram 通知。

### 调整刷新间隔

在代码中找到这行：

```javascript
setInterval(refreshStats, 5 * 60 * 1000);  // 5 分钟
```

修改数字改变刷新频率（单位：毫秒）：
- `60 * 1000` = 1 分钟
- `5 * 60 * 1000` = 5 分钟（默认）
- `10 * 60 * 1000` = 10 分钟

---

🙏 特别感谢与致谢
  源代码作者：天诚大佬 [链接](<https://github.com/xtgm/stallTCP1.32V2>)
---

**部署指南完成！祝您部署顺利！** 🎉

如有问题，请查看对应的故障排查部分或参考其他文档。
