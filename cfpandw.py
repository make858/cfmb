import sys
import json
import time
import threading
import datetime
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTextEdit, QGroupBox, QComboBox,
    QDialog, QDialogButtonBox, QFormLayout, QSpinBox, QListWidget,
    QListWidgetItem, QCheckBox, QMessageBox, QInputDialog, QTabWidget,
    QScrollArea
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QMutex, QMutexLocker
from PyQt6.QtGui import QFont, QIcon
import matplotlib
# 配置matplotlib支持中文
matplotlib.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
matplotlib.rcParams['axes.unicode_minus'] = False
matplotlib.use('QtAgg')
# 优化matplotlib渲染性能
matplotlib.rcParams['agg.path.chunksize'] = 10000
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates

# 配置文件路径（与主程序同一目录）
CONFIG_PATH = Path(__file__).parent / "cf_monitor_config.json"
# 默认每日请求上限
DEFAULT_REQUEST_LIMIT = 200000
# 最大并发请求数
MAX_WORKERS = 5
# 缓存过期时间（小时）
CACHE_EXPIRE_HOURS = 24

# --------------------------- 配置管理类 ---------------------------
class ConfigManager:
    def __init__(self):
        self.config = {
            "accounts": [],  # 新增account_id_cache字段
            "proxy": {
                "enable": False,
                "type": "http",
                "host": "",
                "port": "",
                "username": "",
                "password": ""
            },
            "refresh_interval": 300,
            "request_limit": DEFAULT_REQUEST_LIMIT
        }
        self.mutex = QMutex()
        self.load_config()

    def load_config(self):
        """加载配置文件（加锁保证线程安全）"""
        with QMutexLocker(self.mutex):
            if CONFIG_PATH.exists():
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        self.config.update(loaded)
                        # 兼容旧配置，添加account_id_cache字段
                        for acc in self.config["accounts"]:
                            if "account_id_cache" not in acc:
                                acc["account_id_cache"] = ""
                                acc["cache_update_time"] = ""
                except Exception as e:
                    QMessageBox.warning(None, "配置加载失败", f"配置文件损坏，使用默认配置：{str(e)}")

    def save_config(self):
        """保存配置文件（加锁保证线程安全）"""
        with QMutexLocker(self.mutex):
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
            except Exception as e:
                QMessageBox.critical(None, "配置保存失败", str(e))

    # 以下方法保持不变，仅添加缓存相关的更新
    def update_account_cache(self, index, account_id):
        """更新账号ID缓存"""
        with QMutexLocker(self.mutex):
            if 0 <= index < len(self.config["accounts"]):
                self.config["accounts"][index]["account_id_cache"] = account_id
                self.config["accounts"][index]["cache_update_time"] = datetime.datetime.now().isoformat()
        # 异步保存配置，避免在工作线程中频繁磁盘IO导致阻塞
        try:
            threading.Thread(target=self.save_config, daemon=True).start()
        except Exception:
            # 若线程启动失败则回退为同步保存，保证数据不丢失
            self.save_config()

    def get_accounts(self):
        return self.config.get("accounts", [])

    def add_account(self, account_info):
        account_info["account_id_cache"] = ""
        account_info["cache_update_time"] = ""
        self.config["accounts"].append(account_info)
        self.save_config()

    def update_account(self, index, account_info):
        if 0 <= index < len(self.config["accounts"]):
            # 保留缓存信息
            old_info = self.config["accounts"][index]
            account_info["account_id_cache"] = old_info.get("account_id_cache", "")
            account_info["cache_update_time"] = old_info.get("cache_update_time", "")
            self.config["accounts"][index] = account_info
            self.save_config()

    def delete_account(self, index):
        if 0 <= index < len(self.config["accounts"]):
            del self.config["accounts"][index]
            self.save_config()

    def get_proxy_config(self):
        return self.config.get("proxy", {})

    def update_proxy_config(self, proxy_config):
        self.config["proxy"] = proxy_config
        self.save_config()

    def get_refresh_interval(self):
        return self.config.get("refresh_interval", 300)

    def set_refresh_interval(self, interval):
        self.config["refresh_interval"] = interval
        self.save_config()

    def get_request_limit(self):
        return self.config.get("request_limit", DEFAULT_REQUEST_LIMIT)

    def set_request_limit(self, limit):
        self.config["request_limit"] = limit
        self.save_config()

# --------------------------- CF API请求类 ---------------------------
class CFAPIClient:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.api_url = "https://api.cloudflare.com/client/v4"
        self.data = {}  # {account_name: {数据}, ...}
        self.last_update = {}  # {account_name: 更新时间}
        self.mutex = QMutex()
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    def get_proxies(self):
        """构建requests代理配置（优化：缓存代理配置）"""
        proxy_config = self.config_manager.get_proxy_config()
        if not proxy_config.get("enable", False):
            return None
        
        proxy_type = proxy_config.get("type", "http")
        host = proxy_config.get("host", "")
        port = proxy_config.get("port", "")
        if not host or not port:
            return None
        
        proxy_url = f"{proxy_type}://{host}:{port}"
        if proxy_config.get("username") and proxy_config.get("password"):
            proxy_url = f"{proxy_type}://{proxy_config['username']}:{proxy_config['password']}@{host}:{port}"
        
        return {
            "http": proxy_url,
            "https": proxy_url
        }

    def get_account_id(self, email, key, api_token, cache_id, cache_time):
        """获取账户ID（优化：使用缓存）"""
        # 检查缓存是否有效
        if cache_id:
            try:
                if cache_time:
                    cache_dt = datetime.datetime.fromisoformat(cache_time)
                    if (datetime.datetime.now() - cache_dt).total_seconds() < CACHE_EXPIRE_HOURS * 3600:
                        return cache_id
            except:
                pass

        headers = {"Content-Type": "application/json"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        else:
            headers["X-AUTH-EMAIL"] = email
            headers["X-AUTH-KEY"] = key

        try:
            response = requests.get(
                f"{self.api_url}/accounts",
                headers=headers,
                proxies=self.get_proxies(),
                timeout=10  # 缩短超时时间
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("success") or not data.get("result"):
                raise Exception("未获取到账户信息")
            
            idx = next((i for i, acc in enumerate(data["result"]) 
                        if acc["name"].lower().startswith(email.lower())), 0)
            return data["result"][idx]["id"]
        except Exception as e:
            raise Exception(f"获取Account ID失败: {str(e)}")

    def query_usage_single(self, account_info):
        """查询单个账户的使用量（独立方法，用于并行执行）"""
        email = account_info.get("email", "")
        key = account_info.get("key", "")
        api_token = account_info.get("api_token", "")
        account_id = account_info.get("account_id", "")
        account_name = account_info.get("name", "未知账户")
        cache_id = account_info.get("account_id_cache", "")
        cache_time = account_info.get("cache_update_time", "")

        # 验证凭证
        if not (email and key) and not api_token:
            return {
                "name": account_name,
                "data": {"error": "未配置CF凭证（邮箱+Key或API Token）"}
            }

        # 获取Account ID（使用缓存）
        try:
            if not account_id:
                account_id = self.get_account_id(email, key, api_token, cache_id, cache_time)
                # 更新缓存
                acc_index = next(i for i, acc in enumerate(self.config_manager.get_accounts()) 
                               if acc.get("name") == account_name)
                self.config_manager.update_account_cache(acc_index, account_id)
        except Exception as e:
            return {
                "name": account_name,
                "data": {"error": str(e)}
            }

        # 构建请求头
        headers = {"Content-Type": "application/json"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        else:
            headers["X-AUTH-EMAIL"] = email
            headers["X-AUTH-KEY"] = key

        # 构建GraphQL查询（优化：缩短时间范围，减少数据量）
        now = datetime.datetime.now(datetime.timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        query = """
        query getBillingMetrics($AccountID: String!, $filter: AccountWorkersInvocationsAdaptiveFilter_InputObject) {
            viewer {
                accounts(filter: {accountTag: $AccountID}) {
                    pagesFunctionsInvocationsAdaptiveGroups(limit: 500, filter: $filter) {
                        sum { requests }
                    }
                    workersInvocationsAdaptive(limit: 5000, filter: $filter) {
                        sum { requests }
                    }
                }
            }
        }
        """

        variables = {
            "AccountID": account_id,
            "filter": {
                "datetime_geq": today.isoformat(),
                "datetime_leq": now.isoformat()
            }
        }

        try:
            # 发送GraphQL请求（优化：缩短超时）
            response = requests.post(
                f"{self.api_url}/graphql",
                headers=headers,
                json={"query": query, "variables": variables},
                proxies=self.get_proxies(),
                timeout=15
            )
            response.raise_for_status()
            result = response.json()

            if result.get("errors"):
                raise Exception(f"GraphQL错误: {result['errors'][0]['message']}")

            # 解析数据
            acc_data = result.get("data", {}).get("viewer", {}).get("accounts", [])
            if not acc_data:
                raise Exception("未获取到账户使用数据")
            
            acc = acc_data[0]
            pages = sum(group.get("sum", {}).get("requests", 0) for group in acc.get("pagesFunctionsInvocationsAdaptiveGroups", []))
            workers = sum(group.get("sum", {}).get("requests", 0) for group in acc.get("workersInvocationsAdaptive", []))
            total = pages + workers

            return {
                "name": account_name,
                "data": {
                    "requests": total,
                    "pages": pages,
                    "workers": workers,
                    "account_id": account_id,
                    "error": ""
                }
            }
        except Exception as e:
            return {
                "name": account_name,
                "data": {"error": str(e)}
            }

    def update_all_accounts(self):
        """更新所有账户的使用数据（优化：并行请求）"""
        accounts = self.config_manager.get_accounts()
        if not accounts:
            return {}
        # 并行执行请求
        futures = [self.executor.submit(self.query_usage_single, account) for account in accounts]

        # 收集结果（先在本地汇总，避免长时间持锁）
        results = {}
        current_time = datetime.datetime.now()
        for future in as_completed(futures):
            try:
                result = future.result()
                results[result["name"]] = result["data"]
            except Exception:
                continue

        # 批量更新共享数据
        with QMutexLocker(self.mutex):
            self.data.clear()
            self.data.update(results)
            for name in results:
                self.last_update[name] = current_time

        return self.data.copy()

    def update_single_account(self, account_name):
        """更新单个账户数据（用于点击刷新）"""
        accounts = self.config_manager.get_accounts()
        account_info = next((acc for acc in accounts if acc.get("name") == account_name), None)
        if not account_info:
            return {}

        try:
            result = self.query_usage_single(account_info)
            with QMutexLocker(self.mutex):
                self.data[result["name"]] = result["data"]
                self.last_update[result["name"]] = datetime.datetime.now()
            return {result["name"]: result["data"]}
        except Exception as e:
            return {account_name: {"error": str(e)}}

# --------------------------- 刷新线程类 ---------------------------
class RefreshThread(QThread):
    update_signal = pyqtSignal(dict)  # {account_name: 数据}
    single_update_signal = pyqtSignal(str, dict)  # account_name, 数据
    error_signal = pyqtSignal(str)

    def __init__(self, cf_client, config_manager):
        super().__init__()
        self.cf_client = cf_client
        self.config_manager = config_manager
        self.is_running = True
        self.target_account = None  # 用于单个账号刷新

    def run(self):
        """线程主循环（优化：减少无效等待）"""
        while self.is_running:
            try:
                # 如果有指定刷新的账号，只刷新该账号
                if self.target_account:
                    data = self.cf_client.update_single_account(self.target_account)
                    self.single_update_signal.emit(self.target_account, data.get(self.target_account, {}))
                    self.target_account = None
                else:
                    # 刷新所有账号
                    data = self.cf_client.update_all_accounts()
                    self.update_signal.emit(data)
                
                # 智能等待：检查是否需要立即刷新单个账号
                interval = self.config_manager.get_refresh_interval()
                wait_count = 0
                while wait_count < interval and self.is_running and not self.target_account:
                    time.sleep(0.5)
                    wait_count += 0.5
                    
            except Exception as e:
                self.error_signal.emit(f"刷新失败: {str(e)}")

    def stop(self):
        self.is_running = False
        self.wait()

    def refresh_single_account(self, account_name):
        """触发单个账号刷新"""
        self.target_account = account_name

# --------------------------- 单个账号图表组件类 ---------------------------
class SingleChartWidget(FigureCanvas):
    def __init__(self, account_name, parent=None):
        self.account_name = account_name
        self.fig = Figure(figsize=(8, 4), dpi=90)  # 优化：减小图表尺寸，降低渲染耗时
        self.ax = self.fig.add_subplot(111)
        super().__init__(self.fig)
        self.setParent(parent)
        self.data = {}
        self.request_limit = DEFAULT_REQUEST_LIMIT
        self.history = []  # [(time, requests), ...]
        self.max_history = 20  # 优化：减少历史数据点
        # 绘图节流：避免频繁重绘导致UI卡顿
        self._draw_timer = QTimer(self)
        self._draw_timer.setSingleShot(True)
        self._draw_timer.setInterval(200)  # 合并短时间内的多次绘制请求
        self._draw_timer.timeout.connect(self._do_draw)

    def update_data(self, data, request_limit):
        """更新单个账号图表数据（优化：仅数据变化时重绘）"""
        if not data or data.get("error"):
            self.data = data
            self.clear_chart()
            return

        # 只在数据变化时更新历史
        current_requests = data.get("requests", 0)
        last_requests = self.history[-1][1] if self.history else -1
        
        if current_requests != last_requests:
            self.data = data
            self.request_limit = request_limit
            now = datetime.datetime.now()
            self.history.append((now, current_requests))
            
            # 限制历史数据量
            if len(self.history) > self.max_history:
                self.history.pop(0)
            
            self.draw_chart()

    def draw_chart(self):
        """绘制单个账号图表（优化：减少绘图操作）"""
        self.ax.clear()
        
        # 设置图表样式（简化样式，提高渲染速度）
        self.ax.set_title(f"{self.account_name} - Workers/Pages 日请求量", fontsize=11, fontweight="bold")
        self.ax.set_xlabel("时间", fontsize=9)
        self.ax.set_ylabel("请求数", fontsize=9)
        self.ax.grid(True, alpha=0.2)
        
        # 绘制总量上限线
        self.ax.axhline(y=self.request_limit, color="red", linestyle="--", alpha=0.6, label="每日上限")
        
        # 绘制数据
        if self.history:
            times = [h[0] for h in self.history]
            requests = [h[1] for h in self.history]
            
            self.ax.plot(times, requests, marker="o", color="blue", markersize=3, linewidth=1.5)
            
            # 标注最新值
            last_time = times[-1]
            last_req = requests[-1]
            self.ax.annotate(f"{last_req:,}", (last_time, last_req), 
                            xytext=(5, 5), textcoords="offset points", fontsize=8)
        
        # 设置X轴时间格式
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        self.fig.autofmt_xdate()
        self.ax.legend(loc="upper left", fontsize=8)
        
        # 使用定时器合并绘图请求，降低短时间频繁绘制的开销
        self._schedule_draw()

    def clear_chart(self):
        """清空图表"""
        self.ax.clear()
        self.ax.set_title(f"{self.account_name} - 暂无数据", fontsize=11)
        self._schedule_draw()

    def _schedule_draw(self):
        """启动或重置定时器以延迟实际绘图，从而合并快速连续的绘图请求"""
        try:
            if not self._draw_timer.isActive():
                self._draw_timer.start()
            else:
                # 重新启动以延长合并窗口
                self._draw_timer.start()
        except RuntimeError:
            # 在某些极端情况下（比如控件已被销毁）调用可能失败，降级为立即绘制
            try:
                self.draw_idle()
            except Exception:
                pass

    def _do_draw(self):
        """实际进行绘图（在主线程通过QTimer触发）"""
        try:
            # 使用draw_idle以让Qt自行合并绘制事件，避免重复阻塞
            self.draw_idle()
        except Exception:
            pass

# --------------------------- 账户管理对话框 ---------------------------
# 保持不变
class AccountDialog(QDialog):
    def __init__(self, parent=None, account_info=None):
        super().__init__(parent)
        self.setWindowTitle("添加/编辑账户")
        self.setModal(True)
        self.setMinimumWidth(400)
        
        self.account_info = account_info or {"name": "", "email": "", "key": "", "api_token": "", "account_id": ""}
        
        self.name_edit = QLineEdit(self.account_info["name"])
        self.email_edit = QLineEdit(self.account_info["email"])
        self.key_edit = QLineEdit(self.account_info["key"])
        self.api_token_edit = QLineEdit(self.account_info["api_token"])
        self.account_id_edit = QLineEdit(self.account_info["account_id"])
        
        layout = QFormLayout()
        layout.addRow("账户名称*", self.name_edit)
        layout.addRow("CF邮箱（全局Key模式）", self.email_edit)
        layout.addRow("CF全局Key（全局Key模式）", self.key_edit)
        layout.addRow("CF API Token（Token模式）", self.api_token_edit)
        layout.addRow("Account ID（可选）", self.account_id_edit)
        
        tip_label = QLabel("提示：二选一模式（邮箱+全局Key 或 API Token），Account ID可选（留空会自动获取）")
        tip_label.setStyleSheet("color: #666; font-size: 9px;")
        layout.addRow(tip_label)
        
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        
        main_layout = QVBoxLayout()
        main_layout.addLayout(layout)
        main_layout.addWidget(tip_label)
        main_layout.addWidget(buttons)
        self.setLayout(main_layout)

    def get_account_info(self):
        return {
            "name": self.name_edit.text().strip(),
            "email": self.email_edit.text().strip(),
            "key": self.key_edit.text().strip(),
            "api_token": self.api_token_edit.text().strip(),
            "account_id": self.account_id_edit.text().strip()
        }

# --------------------------- 设置对话框 ---------------------------
# 保持不变
class SettingsDialog(QDialog):
    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.setWindowTitle("系统设置")
        self.setModal(True)
        self.setMinimumWidth(450)
        
        proxy_config = self.config_manager.get_proxy_config()
        refresh_interval = self.config_manager.get_refresh_interval()
        request_limit = self.config_manager.get_request_limit()
        
        proxy_group = QGroupBox("代理设置")
        self.proxy_enable = QCheckBox("启用代理")
        self.proxy_enable.setChecked(proxy_config.get("enable", False))
        
        self.proxy_type = QComboBox()
        self.proxy_type.addItems(["http", "https", "socks5"])
        self.proxy_type.setCurrentText(proxy_config.get("type", "http"))
        
        self.proxy_host = QLineEdit(proxy_config.get("host", ""))
        self.proxy_port = QLineEdit(proxy_config.get("port", ""))
        self.proxy_username = QLineEdit(proxy_config.get("username", ""))
        self.proxy_password = QLineEdit(proxy_config.get("password", ""))
        
        proxy_layout = QFormLayout()
        proxy_layout.addRow(self.proxy_enable)
        proxy_layout.addRow("代理类型", self.proxy_type)
        proxy_layout.addRow("代理主机", self.proxy_host)
        proxy_layout.addRow("代理端口", self.proxy_port)
        proxy_layout.addRow("用户名（可选）", self.proxy_username)
        proxy_layout.addRow("密码（可选）", self.proxy_password)
        proxy_group.setLayout(proxy_layout)
        
        refresh_group = QGroupBox("刷新设置")
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(30, 3600)
        self.refresh_spin.setValue(refresh_interval)
        self.refresh_spin.setSuffix(" 秒")
        
        refresh_layout = QFormLayout()
        refresh_layout.addRow("自动刷新间隔", self.refresh_spin)
        refresh_group.setLayout(refresh_layout)
        
        limit_group = QGroupBox("用量设置")
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1000, 1000000)
        self.limit_spin.setValue(request_limit)
        self.limit_spin.setSuffix(" 次/日")
        
        limit_layout = QFormLayout()
        limit_layout.addRow("每日请求上限", self.limit_spin)
        limit_group.setLayout(limit_layout)
        
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal, self
        )
        buttons.accepted.connect(self.save_settings)
        buttons.rejected.connect(self.reject)
        
        main_layout = QVBoxLayout()
        main_layout.addWidget(proxy_group)
        main_layout.addWidget(refresh_group)
        main_layout.addWidget(limit_group)
        main_layout.addWidget(buttons)
        self.setLayout(main_layout)

    def save_settings(self):
        proxy_config = {
            "enable": self.proxy_enable.isChecked(),
            "type": self.proxy_type.currentText(),
            "host": self.proxy_host.text().strip(),
            "port": self.proxy_port.text().strip(),
            "username": self.proxy_username.text().strip(),
            "password": self.proxy_password.text().strip()
        }
        self.config_manager.update_proxy_config(proxy_config)
        
        self.config_manager.set_refresh_interval(self.refresh_spin.value())
        self.config_manager.set_request_limit(self.limit_spin.value())
        
        self.accept()

# --------------------------- 主窗口类 ---------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cloudflare 用量监测工具 - Windows版")
        self.setMinimumSize(900, 700)
        
        # 初始化配置和API客户端
        self.config_manager = ConfigManager()
        self.cf_client = CFAPIClient(self.config_manager)
        
        # 图表缓存
        self.chart_widgets = {}  # {account_name: SingleChartWidget}
        
        # 初始化刷新线程
        self.refresh_thread = None
        
        # 先初始化UI，确保窗口能显示
        self.init_ui()
        
        # 初始刷新（放到UI初始化后）
        QTimer.singleShot(100, self.refresh_data)
        
        # 启动自动刷新
        QTimer.singleShot(200, self.start_refresh_thread)

    def init_ui(self):
        """初始化UI（修复窗口显示问题的核心）"""
        # 创建中心部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 顶部工具栏
        top_layout = QHBoxLayout()
        
        # 账户列表
        self.account_list = QListWidget()
        self.account_list.setMaximumWidth(200)
        
        # 账户操作按钮
        account_buttons_layout = QVBoxLayout()
        add_btn = QPushButton("添加账户")
        edit_btn = QPushButton("编辑账户")
        del_btn = QPushButton("删除账户")
        add_btn.clicked.connect(self.add_account)
        edit_btn.clicked.connect(self.edit_account)
        del_btn.clicked.connect(self.delete_account)
        account_buttons_layout.addWidget(add_btn)
        account_buttons_layout.addWidget(edit_btn)
        account_buttons_layout.addWidget(del_btn)
        account_buttons_layout.addStretch()
        
        # 功能按钮
        func_buttons_layout = QVBoxLayout()
        refresh_btn = QPushButton("手动刷新全部")
        settings_btn = QPushButton("系统设置")
        refresh_btn.clicked.connect(self.refresh_data)
        settings_btn.clicked.connect(self.open_settings)
        func_buttons_layout.addWidget(refresh_btn)
        func_buttons_layout.addWidget(settings_btn)
        func_buttons_layout.addStretch()
        
        # 状态标签
        self.status_label = QLabel("最后更新：未更新")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # 图表区域（改为TabWidget，每个账号一个Tab）
        self.chart_tabs = QTabWidget()
        self.chart_tabs.setMinimumHeight(400)
        
        # 顶部布局组装
        top_layout.addWidget(QLabel("账户列表："))
        top_layout.addWidget(self.account_list)
        top_layout.addLayout(account_buttons_layout)
        top_layout.addLayout(func_buttons_layout)
        top_layout.addStretch()
        top_layout.addWidget(self.status_label)
        
        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.addLayout(top_layout)
        main_layout.addWidget(QLabel("用量图表（每个账户独立图表）："))
        main_layout.addWidget(self.chart_tabs)
        
        # 绑定账户列表点击事件
        self.account_list.itemClicked.connect(self.on_account_click)
        
        # 加载账户列表（确保chart_tabs已创建）
        self.load_account_list()
        
        # 强制显示窗口
        self.show()

    def create_account_tab(self, account_name):
        """为单个账号创建Tab页（包含图表和详情）"""
        if account_name in self.chart_widgets:
            return
        
        # 创建Tab页面
        tab_widget = QWidget()
        tab_layout = QVBoxLayout(tab_widget)
        
        # 创建单个账号图表
        chart_widget = SingleChartWidget(account_name)
        self.chart_widgets[account_name] = chart_widget
        
        # 创建详情文本框
        detail_text = QTextEdit()
        detail_text.setReadOnly(True)
        detail_text.setMaximumHeight(100)
        self.chart_widgets[f"{account_name}_detail"] = detail_text
        
        # 添加到Tab布局
        tab_layout.addWidget(chart_widget)
        tab_layout.addWidget(QLabel("数据详情："))
        tab_layout.addWidget(detail_text)
        
        # 添加到TabWidget
        self.chart_tabs.addTab(tab_widget, account_name)

    def remove_account_tab(self, account_name):
        """移除账号对应的Tab页"""
        if account_name not in self.chart_widgets:
            return
        
        # 找到Tab索引并移除
        for i in range(self.chart_tabs.count()):
            if self.chart_tabs.tabText(i) == account_name:
                self.chart_tabs.removeTab(i)
                break
        
        # 清理缓存
        del self.chart_widgets[f"{account_name}_detail"]
        del self.chart_widgets[account_name]

    def load_account_list(self):
        """加载账户列表（同步更新Tab页）"""
        self.account_list.clear()
        accounts = self.config_manager.get_accounts()
        
        # 先移除不存在的Tab
        existing_tabs = [self.chart_tabs.tabText(i) for i in range(self.chart_tabs.count())]
        for tab_name in existing_tabs:
            if tab_name not in [acc.get("name") for acc in accounts]:
                self.remove_account_tab(tab_name)
        
        # 添加新账户的Tab
        for account in accounts:
            account_name = account.get("name", "未知账户")
            item = QListWidgetItem(account_name)
            self.account_list.addItem(item)
            self.create_account_tab(account_name)

    def on_account_click(self, item):
        """账户列表点击事件：刷新该账户数据"""
        account_name = item.text()
        if self.refresh_thread:
            self.refresh_thread.refresh_single_account(account_name)
        # 切换到对应Tab
        for i in range(self.chart_tabs.count()):
            if self.chart_tabs.tabText(i) == account_name:
                self.chart_tabs.setCurrentIndex(i)
                break

    def add_account(self):
        dialog = AccountDialog(self)
        if dialog.exec():
            account_info = dialog.get_account_info()
            if not account_info["name"]:
                QMessageBox.warning(self, "警告", "账户名称不能为空！")
                return
            if not (account_info["email"] and account_info["key"]) and not account_info["api_token"]:
                QMessageBox.warning(self, "警告", "必须填写邮箱+全局Key 或 API Token！")
                return
            self.config_manager.add_account(account_info)
            self.load_account_list()
            self.refresh_data()

    def edit_account(self):
        current_idx = self.account_list.currentRow()
        if current_idx < 0:
            QMessageBox.warning(self, "警告", "请先选择要编辑的账户！")
            return
        accounts = self.config_manager.get_accounts()
        account_info = accounts[current_idx]
        old_name = account_info.get("name")
        dialog = AccountDialog(self, account_info)
        if dialog.exec():
            new_info = dialog.get_account_info()
            if not new_info["name"]:
                QMessageBox.warning(self, "警告", "账户名称不能为空！")
                return
            # 如果名称修改，更新Tab
            if new_info["name"] != old_name:
                self.remove_account_tab(old_name)
            self.config_manager.update_account(current_idx, new_info)
            self.load_account_list()
            self.refresh_data()

    def delete_account(self):
        current_idx = self.account_list.currentRow()
        if current_idx < 0:
            QMessageBox.warning(self, "警告", "请先选择要删除的账户！")
            return
        account_name = self.account_list.currentItem().text()
        if QMessageBox.question(self, "确认", "确定要删除该账户吗？", 
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.config_manager.delete_account(current_idx)
            self.remove_account_tab(account_name)
            self.load_account_list()
            self.refresh_data()

    def open_settings(self):
        dialog = SettingsDialog(self.config_manager, self)
        if dialog.exec():
            self.stop_refresh_thread()
            self.start_refresh_thread()
            self.refresh_data()

    def refresh_data(self):
        """手动刷新所有数据（优化：非阻塞）"""
        if self.refresh_thread and self.refresh_thread.isRunning():
            # 触发全量刷新
            self.refresh_thread.target_account = None
        else:
            # 直接刷新（备用）
            try:
                data = self.cf_client.update_all_accounts()
                self.update_ui(data)
            except Exception as e:
                QMessageBox.critical(self, "刷新失败", str(e))

    def update_ui(self, data):
        """更新所有账号UI"""
        if not data:
            return
        
        # 更新状态标签
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status_label.setText(f"最后更新：{current_time} | 刷新间隔：{self.config_manager.get_refresh_interval()}秒")
        
        # 更新每个账号的图表和详情
        request_limit = self.config_manager.get_request_limit()
        for account_name, account_data in data.items():
            self.update_single_account_ui(account_name, account_data, request_limit)

    def update_single_account_ui(self, account_name, account_data, request_limit=None):
        """更新单个账号UI（优化：仅更新变化的部分）"""
        if account_name not in self.chart_widgets:
            return
        
        if request_limit is None:
            request_limit = self.config_manager.get_request_limit()
        
        # 更新图表
        chart_widget = self.chart_widgets[account_name]
        chart_widget.update_data(account_data, request_limit)
        
        # 更新详情
        detail_text = self.chart_widgets[f"{account_name}_detail"]
        detail_content = f"【{account_name}】\n"
        if account_data.get("error"):
            detail_content += f"  错误：{account_data['error']}\n"
        else:
            update_time = self.cf_client.last_update.get(account_name, datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            detail_content += f"  更新时间：{update_time}\n"
            detail_content += f"  今日总请求：{account_data['requests']:,}\n"
            detail_content += f"  Pages请求：{account_data['pages']:,}\n"
            detail_content += f"  Workers请求：{account_data['workers']:,}\n"
            detail_content += f"  账户ID：{account_data['account_id']}\n"
            usage_rate = (account_data['requests'] / request_limit) * 100
            detail_content += f"  使用率：{usage_rate:.1f}% ({account_data['requests']:,}/{request_limit:,})\n"
        
        detail_text.setText(detail_content)

    def start_refresh_thread(self):
        """启动刷新线程（优化：线程管理）"""
        self.stop_refresh_thread()
        self.refresh_thread = RefreshThread(self.cf_client, self.config_manager)
        self.refresh_thread.update_signal.connect(self.update_ui)
        self.refresh_thread.single_update_signal.connect(lambda name, data: self.update_single_account_ui(name, data))
        self.refresh_thread.error_signal.connect(lambda msg: QMessageBox.warning(self, "刷新警告", msg))
        self.refresh_thread.start()

    def stop_refresh_thread(self):
        """停止刷新线程"""
        if self.refresh_thread and self.refresh_thread.isRunning():
            self.refresh_thread.stop()
            self.refresh_thread = None

    def closeEvent(self, event):
        """关闭窗口（优化：关闭线程池）"""
        self.stop_refresh_thread()
        if hasattr(self.cf_client, 'executor'):
            self.cf_client.executor.shutdown(wait=False)
        event.accept()

# --------------------------- 主程序入口 ---------------------------
if __name__ == "__main__":
    # 兼容不同PyQt6版本的高分屏适配（核心修复）
    try:
        # 尝试设置高分屏适配（兼容新老版本）
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    except AttributeError:
        # 如果属性不存在，跳过适配（不影响程序运行）
        pass
    
    # 优化QApplication性能
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # 使用更高效的样式
    
    # 创建主窗口并强制显示
    window = MainWindow()
    window.raise_()  # 提升窗口到前台
    window.activateWindow()  # 激活窗口
    
    # 确保程序正常退出
    sys.exit(app.exec())
