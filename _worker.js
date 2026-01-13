import { connect } from 'cloudflare:sockets';

// =============================================================================
// 🟣 配置区域
// =============================================================================

// Web 登录密码
const WEB_PASSWORD = "123456";  // 修改你的登录密码

// Telegram 机器人配置
const TG_BOT_TOKEN = "xxxxxxxxxxxxxxx"; // Telegram Bot Token
const TG_CHAT_ID = "xxxxxxxx"; // Telegram Chat ID

// Cloudflare 统计配置（支持多账号）
// 格式: CF_ACCOUNTS_1={"email":"...", "key":"..."}, CF_ACCOUNTS_2={"email":"...", "key":"..."}
// 或使用 CF_ID 和 CF_TOKEN 进行 API Token 验证

// 请求数限制配置（每日请求数）
const REQUEST_LIMIT = 200000; // 每日请求上限

// =============================================================================
// 🛠️ 工具函数
// =============================================================================

async function getSafeEnv(env, key, defaultValue = "") {
  if (env[key] !== undefined) return env[key];
  if (env.DB) {
    try {
      const { results } = await env.DB.prepare("SELECT value FROM config WHERE key = ?").bind(key).all();
      return results?.[0]?.value || defaultValue;
    } catch(e) {}
  }
  if (env.LH) {
    try {
      const val = await env.LH.get(key);
      return val || defaultValue;
    } catch(e) {}
  }
  return defaultValue;
}

// =============================================================================
// 📊 Cloudflare 统计函数（支持多账号）
// =============================================================================

async function getCloudflareUsage(env, accountConfig = null) {
  let email = accountConfig?.email || await getSafeEnv(env, 'CF_EMAIL', "");
  let globalKey = accountConfig?.key || await getSafeEnv(env, 'CF_KEY', "");
  let accountID = accountConfig?.id || await getSafeEnv(env, 'CF_ID', "");
  let apiToken = accountConfig?.token || await getSafeEnv(env, 'CF_TOKEN', "");

  if (!email && !globalKey && !accountID && !apiToken) {
    return { success: false, msg: "未配置 CF 凭证", requests: 0 };
  }

  const API = "https://api.cloudflare.com/client/v4";
  const cfg = { "Content-Type": "application/json" };

  try {
    // 获取 Account ID
    let finalAccountID = accountID;
    if (!finalAccountID && email && globalKey) {
      const r = await fetch(`${API}/accounts`, {
        method: "GET",
        headers: { ...cfg, "X-AUTH-EMAIL": email, "X-AUTH-KEY": globalKey }
      });
      if (!r.ok) {
        const errorData = await r.json().catch(() => ({}));
        throw new Error(`账户获取失败 (${r.status}): ${errorData.errors?.[0]?.message || r.statusText}`);
      }
      const d = await r.json();
      if (!d.success || !d.result || d.result.length === 0) {
        throw new Error("无效的 Cloudflare 凭证或账户不存在");
      }
      const idx = d.result?.findIndex(a => a.name?.toLowerCase().startsWith(email.toLowerCase()));
      finalAccountID = d.result?.[idx >= 0 ? idx : 0]?.id;
    }

    if (!finalAccountID) throw new Error("无法获取 Account ID，请检查凭证配置");

    // 获取今日请求统计
    const now = new Date();
    now.setUTCHours(0, 0, 0, 0);

    const hdr = apiToken 
      ? { ...cfg, "Authorization": `Bearer ${apiToken}` } 
      : { ...cfg, "X-AUTH-EMAIL": email, "X-AUTH-KEY": globalKey };

    const res = await fetch(`${API}/graphql`, {
      method: "POST",
      headers: hdr,
      body: JSON.stringify({
        query: `query getBillingMetrics($AccountID: String!, $filter: AccountWorkersInvocationsAdaptiveFilter_InputObject) { 
          viewer { 
            accounts(filter: {accountTag: $AccountID}) { 
              pagesFunctionsInvocationsAdaptiveGroups(limit: 1000, filter: $filter) { sum { requests } } 
              workersInvocationsAdaptive(limit: 10000, filter: $filter) { sum { requests } } 
            } 
          } 
        }`,
        variables: {
          AccountID: finalAccountID,
          filter: {
            datetime_geq: now.toISOString(),
            datetime_leq: new Date().toISOString()
          }
        }
      })
    });

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      throw new Error(`GraphQL 查询失败 (${res.status}): ${errorData.errors?.[0]?.message || res.statusText}`);
    }
    
    const result = await res.json();
    
    if (result.errors && result.errors.length > 0) {
      throw new Error(`GraphQL 错误: ${result.errors[0].message}`);
    }

    const acc = result?.data?.viewer?.accounts?.[0];
    if (!acc) {
      throw new Error("无法获取账户数据，请检查 Account ID 是否正确");
    }

    const pages = acc?.pagesFunctionsInvocationsAdaptiveGroups?.reduce((t, i) => t + (i?.sum?.requests || 0), 0) || 0;
    const workers = acc?.workersInvocationsAdaptive?.reduce((t, i) => t + (i?.sum?.requests || 0), 0) || 0;
    const totalRequests = pages + workers;

    return {
      success: true,
      requests: totalRequests,
      pages,
      workers,
      email: email || "API Token",
      accountID: finalAccountID
    };
  } catch (e) {
    console.error('Cloudflare API 错误:', e);
    return { 
      success: false, 
      msg: e.message || '获取数据失败',
      email: email || accountConfig?.email || "未知",
      requests: 0 
    };
  }
}

// 获取所有配置的 Cloudflare 账户的统计
async function getAllCloudflareStats(env) {
  const stats = [];

  try {
    // 首先尝试获取默认配置的账户
    const result = await getCloudflareUsage(env);
    if (result.success) {
      stats.push(result);
    } else if (result.msg && result.msg !== "未配置 CF 凭证") {
      // 如果有具体的错误信息，保存错误状态
      stats.push(result);
    }
  } catch(e) {
    console.error('获取默认账户数据失败:', e);
  }

  // 检查环境变量配置（CF_ACCOUNTS_1, CF_ACCOUNTS_2, ...）
  for (let i = 1; i <= 10; i++) {
    try {
      const key = `CF_ACCOUNTS_${i}`;
      const configStr = await getSafeEnv(env, key, "");
      if (configStr) {
        const config = JSON.parse(configStr);
        const result = await getCloudflareUsage(env, config);
        stats.push(result);
      }
    } catch(e) {
      console.error(`获取账户 ${i} 数据失败:`, e);
    }
  }

  return stats;
}

// =============================================================================
// 📱 Telegram 通知
// =============================================================================

async function sendTelegramMessage(env, message) {
  const token = await getSafeEnv(env, 'TG_BOT_TOKEN', TG_BOT_TOKEN);
  const chatId = await getSafeEnv(env, 'TG_CHAT_ID', TG_CHAT_ID);

  if (!token || !chatId) return false;

  try {
    const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        text: message,
        parse_mode: 'HTML'
      })
    });

    return response.ok;
  } catch(e) {
    return false;
  }
}

// 检查使用量是否超过 95% 并发送通知
async function checkAndNotifyHighUsage(env) {
  const stats = await getAllCloudflareStats(env);

  for (const stat of stats) {
    if (!stat.success) continue;

    const usage = (stat.requests / REQUEST_LIMIT) * 100;

    if (usage >= 95) {
      const message = `⚠️ <b>Cloudflare 请求数告警</b>\n\n` +
        `📧 <b>账户:</b> ${stat.email}\n` +
        `📊 <b>今日请求:</b> ${stat.requests.toLocaleString()}\n` +
        `📈 <b>使用率:</b> ${usage.toFixed(1)}%\n` +
        `⏰ <b>时间:</b> ${new Date().toLocaleString('zh-CN')}`;

      await sendTelegramMessage(env, message);
    }
  }
}

// =============================================================================
// 📝 KV 存储和数据库函数
// =============================================================================

async function logAccess(env, ip, location, action) {
  const logEntry = JSON.stringify({
    timestamp: new Date().toISOString(),
    ip,
    location,
    action
  });

  if (env.DB) {
    try {
      await env.DB.prepare(
        "INSERT INTO logs (ip, location, action) VALUES (?, ?, ?)"
      ).bind(ip, location, action).run();
    } catch(e) {}
  }

  if (env.LH) {
    try {
      const logs = await env.LH.get('ACCESS_LOGS') || '[]';
      const arr = JSON.parse(logs);
      arr.push(JSON.parse(logEntry));
      if (arr.length > 100) arr.shift();
      await env.LH.put('ACCESS_LOGS', JSON.stringify(arr));
    } catch(e) {}
  }
}

async function addWhitelist(env, ip) {
  if (env.DB) {
    try {
      await env.DB.prepare(
        "INSERT OR IGNORE INTO whitelist (ip) VALUES (?)"
      ).bind(ip).run();
    } catch(e) {}
  }
  if (env.LH) {
    try {
      const list = await env.LH.get('WHITELIST') || '[]';
      const arr = JSON.parse(list);
      if (!arr.includes(ip)) {
        arr.push(ip);
        await env.LH.put('WHITELIST', JSON.stringify(arr));
      }
    } catch(e) {}
  }
}

async function checkWhitelist(env, ip) {
  if (env.DB) {
    try {
      const { results } = await env.DB.prepare(
        "SELECT ip FROM whitelist WHERE ip = ?"
      ).bind(ip).all();
      return results?.length > 0;
    } catch(e) {}
  }
  if (env.LH) {
    try {
      const list = await env.LH.get('WHITELIST') || '[]';
      const arr = JSON.parse(list);
      return arr.includes(ip);
    } catch(e) {}
  }
  return false;
}

// =============================================================================
// 🎨 Web 页面 - 包含进度条和自动刷新
// =============================================================================

function generateWebPage(cfStats) {
  const statsHtml = cfStats.map((stat, idx) => {
    if (!stat.success) {
      return `
        <div class="account-card">
          <div class="account-header">
            <span class="account-name">📧 ${stat.email || '未知账户'}</span>
            <span class="account-id">错误</span>
          </div>
          <div class="stats-row">
            <span style="color: #f44336;">❌ 错误: ${stat.msg || '获取数据失败'}</span>
          </div>
        </div>
      `;
    }

    const usage = (stat.requests / REQUEST_LIMIT) * 100;
    const progressColor = usage >= 95 ? '#f44336' : (usage >= 80 ? '#ff9800' : '#4caf50');

    return `
      <div class="account-card">
        <div class="account-header">
          <span class="account-name">📧 ${stat.email}</span>
          <span class="account-id">${stat.accountID}</span>
        </div>
        <div class="stats-row">
          <span>今日请求: <strong>${stat.requests.toLocaleString()}</strong></span>
          <span>使用率: <strong style="color: ${progressColor}">${usage.toFixed(1)}%</strong></span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" style="width: ${Math.min(usage, 100)}%; background-color: ${progressColor}"></div>
        </div>
        <div class="progress-label">
          <span>${stat.requests.toLocaleString()} / ${REQUEST_LIMIT.toLocaleString()}</span>
        </div>
      </div>
    `;
  }).join('');

  const lastUpdateTime = new Date().toLocaleString('zh-CN');
  const successStats = cfStats.filter(s => s.success);

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>☁️ Cloudflare 统计面板</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        :root {
            --primary: #0066cc;
            --success: #4caf50;
            --warning: #ff9800;
            --danger: #f44336;
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-tertiary: #334155;
            --text-primary: #f1f5f9;
            --text-secondary: #cbd5e1;
            --border: #475569;
        }

        body {
            background: linear-gradient(135deg, var(--bg-primary) 0%, #1a2332 100%);
            color: var(--text-primary);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            padding: 20px;
            min-height: 100vh;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        .header {
            text-align: center;
            margin-bottom: 40px;
            padding: 20px 0;
            border-bottom: 1px solid var(--border);
        }

        .header h1 {
            font-size: 32px;
            margin-bottom: 10px;
            background: linear-gradient(135deg, #00d4ff 0%, #0066cc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .header .status {
            font-size: 14px;
            color: var(--text-secondary);
        }

        .controls {
            display: flex;
            gap: 10px;
            justify-content: center;
            margin-bottom: 30px;
            flex-wrap: wrap;
        }

        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.3s ease;
            font-weight: 600;
        }

        .btn-primary {
            background: var(--primary);
            color: white;
        }

        .btn-primary:hover {
            background: #0052a3;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0, 102, 204, 0.3);
        }

        .btn-secondary {
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border: 1px solid var(--border);
        }

        .btn-secondary:hover {
            background: var(--border);
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }

        .account-card {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            transition: all 0.3s ease;
        }

        .account-card:hover {
            border-color: var(--primary);
            box-shadow: 0 8px 24px rgba(0, 102, 204, 0.1);
            transform: translateY(-4px);
        }

        .account-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border);
        }

        .account-name {
            font-size: 16px;
            font-weight: 600;
            color: #00d4ff;
        }

        .account-id {
            font-size: 12px;
            color: var(--text-secondary);
            font-family: monospace;
        }

        .stats-row {
            display: flex;
            justify-content: space-between;
            margin-bottom: 15px;
            font-size: 14px;
        }

        .stats-row span {
            color: var(--text-secondary);
        }

        .stats-row strong {
            color: var(--text-primary);
            font-weight: 600;
        }

        .progress-bar {
            width: 100%;
            height: 24px;
            background: var(--bg-tertiary);
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 8px;
            border: 1px solid var(--border);
        }

        .progress-fill {
            height: 100%;
            transition: width 0.3s ease;
            position: relative;
            overflow: hidden;
        }

        .progress-fill::after {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.3), transparent);
            animation: shimmer 2s infinite;
        }

        @keyframes shimmer {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }

        .progress-label {
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            color: var(--text-secondary);
        }

        .summary {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }

        .summary-title {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 15px;
            color: #00d4ff;
        }

        .summary-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
        }

        .summary-stat {
            text-align: center;
            padding: 10px;
            background: var(--bg-tertiary);
            border-radius: 4px;
            border: 1px solid var(--border);
        }

        .summary-stat-value {
            font-size: 20px;
            font-weight: 600;
            color: #00d4ff;
        }

        .summary-stat-label {
            font-size: 12px;
            color: var(--text-secondary);
            margin-top: 5px;
        }

        .footer {
            text-align: center;
            font-size: 12px;
            color: var(--text-secondary);
            padding-top: 20px;
            border-top: 1px solid var(--border);
        }

        .loading {
            display: inline-block;
            width: 4px;
            height: 4px;
            background: #00d4ff;
            border-radius: 50%;
            animation: blink 1s infinite;
        }

        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .login-form {
            max-width: 400px;
            margin: 100px auto;
            background: var(--bg-secondary);
            padding: 30px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }

        .login-form h2 {
            margin-bottom: 20px;
            text-align: center;
            color: #00d4ff;
        }

        .form-group {
            margin-bottom: 15px;
        }

        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-size: 14px;
            color: var(--text-secondary);
        }

        .form-group input {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border);
            border-radius: 4px;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            font-size: 14px;
        }

        .form-group input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 2px rgba(0, 102, 204, 0.1);
        }

        @media (max-width: 768px) {
            .stats-grid {
                grid-template-columns: 1fr;
            }

            .header h1 {
                font-size: 24px;
            }

            .summary-stats {
                grid-template-columns: repeat(2, 1fr);
            }
        }
    </style>
</head>
<body>
    <div class="container" id="mainContent">
        <div class="header">
            <h1>☁️ Cloudflare 统计面板</h1>
            <div class="status">
                最后更新: <span id="updateTime">${lastUpdateTime}</span>
                <span class="loading"></span>
            </div>
        </div>

        <div class="controls">
            <button class="btn btn-primary" onclick="refreshStats()">🔄 立即刷新</button>
            <button class="btn btn-secondary" onclick="toggleAutoRefresh()" id="autoRefreshBtn">⏱️ 启用自动刷新 (60分钟)</button>
            <button class="btn btn-secondary" onclick="logout()">⏻ 退出登录</button>
        </div>

        <div id="statsContainer">
            ${cfStats.length > 0 ? `
                ${successStats.length > 0 ? `
                <div class="summary">
                    <div class="summary-title">📊 统计概览</div>
                    <div class="summary-stats">
                        <div class="summary-stat">
                            <div class="summary-stat-value">${successStats.length}</div>
                            <div class="summary-stat-label">账户数</div>
                        </div>
                        <div class="summary-stat">
                            <div class="summary-stat-value">${successStats.reduce((t, s) => t + s.requests, 0).toLocaleString()}</div>
                            <div class="summary-stat-label">总请求数</div>
                        </div>
                        <div class="summary-stat">
                            <div class="summary-stat-value">${(successStats.reduce((t, s) => t + s.requests, 0) / (REQUEST_LIMIT * successStats.length) * 100).toFixed(1)}%</div>
                            <div class="summary-stat-label">平均使用率</div>
                        </div>
                    </div>
                </div>
                ` : ''}

                <div class="stats-grid">
                    ${statsHtml}
                </div>
            ` : `
                <div class="summary">
                    <p style="text-align: center; color: var(--text-secondary);">暂无 Cloudflare 账户配置，请检查环境变量设置</p>
                </div>
            `}
        </div>

        <div class="footer">
            <p>Cloudflare 统计面板 | 每 5 分钟自动刷新 | <span id="nextRefreshTime"></span></p>
        </div>
    </div>

    <script>
        let autoRefreshInterval = null;
        let isAutoRefreshEnabled = localStorage.getItem('autoRefreshEnabled') === 'true';

        function updateNextRefreshTime() {
            const now = new Date();
            const next = new Date(now.getTime() + 60 * 60 * 1000);
            document.getElementById('nextRefreshTime').textContent = \`下次手动刷新: \${next.toLocaleTimeString('zh-CN')}\`;
        }

        async function refreshStats() {
            try {
                const response = await fetch('/?flag=stats_api', {
                    method: 'GET',
                    headers: {
                        'Content-Type': 'application/json'
                    }
                });
                if (!response.ok) {
                    throw new Error('HTTP error status: ' + response.status);
                }
                const data = await response.json();
                if (data.html) {
                    document.getElementById('statsContainer').innerHTML = data.html;
                    document.getElementById('updateTime').textContent = new Date().toLocaleString('zh-CN');
                    updateNextRefreshTime();
                } else {
                    console.error('响应格式错误:', data);
                }
            } catch(e) {
                console.error('刷新失败:', e);
                document.getElementById('statsContainer').innerHTML = '<div class="summary"><p style="text-align: center; color: var(--text-secondary);">数据加载失败，请稍后重试或刷新页面</p></div>';
            }
        }

        function toggleAutoRefresh() {
            isAutoRefreshEnabled = !isAutoRefreshEnabled;
            localStorage.setItem('autoRefreshEnabled', isAutoRefreshEnabled);
            const btn = document.getElementById('autoRefreshBtn');

            if (isAutoRefreshEnabled) {
                btn.textContent = '⏱️ 禁用自动刷新';
                autoRefreshInterval = setInterval(refreshStats, 60 * 60 * 1000); // 60 分钟
                updateNextRefreshTime();
            } else {
                btn.textContent = '⏱️ 启用自动刷新 (60分钟)';
                clearInterval(autoRefreshInterval);
            }
        }

        function logout() {
            document.cookie = "auth=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
            location.reload();
        }

        // 页面加载时初始化自动刷新
        if (isAutoRefreshEnabled) {
            toggleAutoRefresh();
        }

        // 定期检查使用量（每 5 分钟）
        setInterval(refreshStats, 5 * 60 * 1000);
        
        // 初始化下次刷新时间显示
        updateNextRefreshTime();
    </script>
</body>
</html>`;
}

// =============================================================================
// 🟢 主入口
// =============================================================================

export default {
  async fetch(r, env, ctx) {
    try {
      const url = new URL(r.url);
      const clientIP = r.headers.get('cf-connecting-ip');
      const password = await getSafeEnv(env, 'WEB_PASSWORD', WEB_PASSWORD);

      // 检查认证
      let isAuthenticated = false;
      if (password) {
        const cookie = r.headers.get('Cookie') || "";
        const regex = new RegExp(`auth=${password.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(;|$)`);
        if (regex.test(cookie)) {
          isAuthenticated = true;
        }
      }

      // 处理登录
      if (url.pathname === '/login' && r.method === 'POST') {
        const body = await r.text();
        const params = new URLSearchParams(body);
        const inputPassword = params.get('password');

        if (inputPassword === password) {
          const setCookie = `Set-Cookie: auth=${password}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict`;
          ctx.waitUntil(logAccess(env, clientIP, 'Login', 'Web登录'));
          return new Response(
            `登录成功，正在重定向...`,
            {
              status: 302,
              headers: { 'Location': '/', 'Set-Cookie': setCookie }
            }
          );
        } else {
          return new Response('密码错误', { status: 401 });
        }
      }

      // 未认证请求显示登录页
      if (!isAuthenticated) {
        return new Response(`<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 - Cloudflare 统计面板</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: linear-gradient(135deg, #0f172a 0%, #1a2332 100%);
            color: #f1f5f9;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .login-form {
            max-width: 400px;
            width: 100%;
            background: #1e293b;
            padding: 30px;
            border-radius: 8px;
            border: 1px solid #475569;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);
        }
        .login-form h2 {
            margin-bottom: 20px;
            text-align: center;
            background: linear-gradient(135deg, #00d4ff 0%, #0066cc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-size: 24px;
        }
        .form-group {
            margin-bottom: 15px;
        }
        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-size: 14px;
            color: #cbd5e1;
        }
        .form-group input {
            width: 100%;
            padding: 10px;
            border: 1px solid #475569;
            border-radius: 4px;
            background: #334155;
            color: #f1f5f9;
            font-size: 14px;
        }
        .form-group input:focus {
            outline: none;
            border-color: #0066cc;
            box-shadow: 0 0 0 2px rgba(0, 102, 204, 0.1);
        }
        .btn {
            width: 100%;
            padding: 10px;
            background: #0066cc;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.3s;
        }
        .btn:hover {
            background: #0052a3;
        }
    </style>
</head>
<body>
    <div class="login-form">
        <h2>☁️ Cloudflare 统计</h2>
        <form method="POST" action="/login">
            <div class="form-group">
                <label for="password">登录密码</label>
                <input type="password" id="password" name="password" required autofocus>
            </div>
            <button type="submit" class="btn">登录</button>
        </form>
    </div>
</body>
</html>`, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
      }

      // API 端点：获取统计数据 JSON
      if (url.searchParams.get('flag') === 'stats' && r.method === 'GET') {
        const cfStats = await getAllCloudflareStats(env);
        
        // 检查是否有超过 95% 使用量的账户
        ctx.waitUntil(checkAndNotifyHighUsage(env));

        return new Response(JSON.stringify({ success: true, stats: cfStats }), {
          headers: { 
            'Content-Type': 'application/json; charset=utf-8',
            'Access-Control-Allow-Origin': '*'
          }
        });
      }

      // API 端点：生成统计页面 HTML（用于前端 AJAX 更新）
      if (url.searchParams.get('flag') === 'stats_api' && r.method === 'GET') {
        const cfStats = await getAllCloudflareStats(env);
        
        if (cfStats.length === 0) {
          return new Response(JSON.stringify({ 
            html: '<div class="summary"><p style="text-align: center; color: var(--text-secondary);">暂无 Cloudflare 账户配置或获取数据失败</p></div>' 
          }), {
            headers: { 
              'Content-Type': 'application/json; charset=utf-8',
              'Access-Control-Allow-Origin': '*'
            }
          });
        }

        const statsHtml = cfStats.map((stat) => {
          if (!stat.success) {
            return `
              <div class="account-card">
                <div class="account-header">
                  <span class="account-name">📧 ${stat.email || '未知账户'}</span>
                  <span class="account-id">错误</span>
                </div>
                <div class="stats-row">
                  <span style="color: #f44336;">错误: ${stat.msg || '获取数据失败'}</span>
                </div>
              </div>
            `;
          }

          const usage = (stat.requests / REQUEST_LIMIT) * 100;
          const progressColor = usage >= 95 ? '#f44336' : (usage >= 80 ? '#ff9800' : '#4caf50');

          return `
            <div class="account-card">
              <div class="account-header">
                <span class="account-name">📧 ${stat.email}</span>
                <span class="account-id">${stat.accountID}</span>
              </div>
              <div class="stats-row">
                <span>今日请求: <strong>${stat.requests.toLocaleString()}</strong></span>
                <span>使用率: <strong style="color: ${progressColor}">${usage.toFixed(1)}%</strong></span>
              </div>
              <div class="progress-bar">
                <div class="progress-fill" style="width: ${Math.min(usage, 100)}%; background-color: ${progressColor}"></div>
              </div>
              <div class="progress-label">
                <span>${stat.requests.toLocaleString()} / ${REQUEST_LIMIT.toLocaleString()}</span>
              </div>
            </div>
          `;
        }).join('');

        // 生成摘要
        const successStats = cfStats.filter(s => s.success);
        const summaryHtml = successStats.length > 0 ? `
          <div class="summary">
            <div class="summary-title">📊 统计概览</div>
            <div class="summary-stats">
              <div class="summary-stat">
                <div class="summary-stat-value">${successStats.length}</div>
                <div class="summary-stat-label">账户数</div>
              </div>
              <div class="summary-stat">
                <div class="summary-stat-value">${successStats.reduce((t, s) => t + s.requests, 0).toLocaleString()}</div>
                <div class="summary-stat-label">总请求数</div>
              </div>
              <div class="summary-stat">
                <div class="summary-stat-value">${(successStats.reduce((t, s) => t + s.requests, 0) / (REQUEST_LIMIT * successStats.length) * 100).toFixed(1)}%</div>
                <div class="summary-stat-label">平均使用率</div>
              </div>
            </div>
          </div>
        ` : '';

        const html = successStats.length > 0 
          ? summaryHtml + '<div class="stats-grid">' + statsHtml + '</div>'
          : '<div class="summary"><p style="text-align: center; color: var(--text-secondary);">暂无 Cloudflare 账户配置</p></div>';

        return new Response(JSON.stringify({ html }), {
          headers: { 
            'Content-Type': 'application/json; charset=utf-8',
            'Access-Control-Allow-Origin': '*'
          }
        });
      }

      // 主页面
      if (url.pathname === '/' && r.method === 'GET') {
        const cfStats = await getAllCloudflareStats(env);
        
        // 分离成功和失败的统计
        const successStats = cfStats.filter(s => s.success);
        const failedStats = cfStats.filter(s => !s.success);
        
        ctx.waitUntil(logAccess(env, clientIP, 'Dashboard', '访问仪表板'));
        ctx.waitUntil(checkAndNotifyHighUsage(env));

        return new Response(generateWebPage(cfStats), {
          headers: { 'Content-Type': 'text/html; charset=utf-8' }
        });
      }

      return new Response('Not Found', { status: 404 });
    } catch (e) {
      return new Response('Internal Server Error: ' + e.message, { status: 500 });
    }
  }
};

