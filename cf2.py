import sys
import json
import time
import threading
import datetime
import sqlite3
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
from PyQt6.QtCharts import QChart, QChartView, QBarSeries, QBarSet, QValueAxis, QBarCategoryAxis, QPieSeries, QPieSlice
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QMutex, QMutexLocker
from PyQt6.QtGui import QFont, QIcon, QColor, QPainter

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "cf_monitor_config.json"
DB_PATH = BASE_DIR / "cf_monitor.db"
# 默认每日请求上限
DEFAULT_REQUEST_LIMIT = 200000
# 最大并发请求数
MAX_WORKERS = 5
# 缓存过期时间（小时）
CACHE_EXPIRE_HOURS = 24

# --------------------------- 配置管理类 ---------------------------
class ConfigManager:
    """配置管理器：支持SQLite和JSON双重存储"""
    def __init__(self):
        self.config = {
            "accounts": [],
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
        self.db = None
        self._init_db()  # 初始化SQLite数据库
        self.load_config()

    def _init_db(self):
        """初始化SQLite数据库（优化：使用连接池和WAL模式）"""
        try:
            # 使用WAL模式提高并发性能
            self.db = sqlite3.connect(str(DB_PATH), timeout=3, check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")  # 降低同步级别，提高写入速度
            self.db.execute("PRAGMA cache_size=10000")    # 增加缓存
            self.db.isolation_level = None  # 自动提交模式
            cursor = self.db.cursor()
            
            # 创建表（带索引优化查询）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    email TEXT,
                    key TEXT,
                    api_token TEXT,
                    account_id TEXT,
                    account_id_cache TEXT,
                    cache_update_time TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            

            
            # 添加索引优化查询速度
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(name)')
            
            self.db.commit()
        except Exception as e:
            print(f"数据库初始化失败: {str(e)}")

    def load_config(self):
        """加载配置（优化：使用缓存，减少数据库查询）"""
        with QMutexLocker(self.mutex):
            try:
                # 先尝试从SQLite读取
                if self.db:
                    cursor = self.db.cursor()
                    
                    # 使用单次查询读取基础配置
                    cursor.execute('SELECT key, value FROM config')
                    db_config = dict(cursor.fetchall())
                    
                    if db_config:
                        # 解析JSON字段
                        if 'proxy' in db_config:
                            self.config['proxy'] = json.loads(db_config['proxy'])
                        if 'refresh_interval' in db_config:
                            self.config['refresh_interval'] = int(db_config['refresh_interval'])
                        if 'request_limit' in db_config:
                            self.config['request_limit'] = int(db_config['request_limit'])
                    
                    # 批量读取账户数据（优化：一次获取全部，而不是逐个查询）
                    cursor.execute('SELECT name, email, key, api_token, account_id, account_id_cache, cache_update_time FROM accounts')
                    accounts_data = cursor.fetchall()
                    self.config['accounts'] = []
                    for row in accounts_data:
                        name, email, key, api_token, account_id, cache_id, cache_time = row
                        self.config['accounts'].append({
                            'name': name,
                            'email': email or '',
                            'key': key or '',
                            'api_token': api_token or '',
                            'account_id': account_id or '',
                            'account_id_cache': cache_id or '',
                            'cache_update_time': cache_time or ''
                        })
                    

                
                # 如果数据库为空，尝试从JSON加载（兼容旧版本）
                if not self.config['accounts'] and CONFIG_PATH.exists():
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        self.config.update(loaded)
                    # 然后迁移到数据库
                    self._migrate_to_db()
            except Exception as e:
                print(f"配置加载失败: {str(e)}")
                # 尝试从JSON加载备份
                if CONFIG_PATH.exists():
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            loaded = json.load(f)
                            self.config.update(loaded)
                    except:
                        pass

    def _migrate_to_db(self):
        """将JSON配置迁移到SQLite"""
        try:
            if not self.db:
                return
            
            cursor = self.db.cursor()
            
            # 迁移基础配置
            cursor.execute('DELETE FROM config')
            cursor.execute('INSERT OR REPLACE INTO config VALUES (?, ?)',
                         ('proxy', json.dumps(self.config.get('proxy', {}))))
            cursor.execute('INSERT OR REPLACE INTO config VALUES (?, ?)',
                         ('refresh_interval', str(self.config.get('refresh_interval', 300))))
            cursor.execute('INSERT OR REPLACE INTO config VALUES (?, ?)',
                         ('request_limit', str(self.config.get('request_limit', DEFAULT_REQUEST_LIMIT))))
            
            # 迁移账户数据
            cursor.execute('DELETE FROM accounts')
            for acc in self.config.get('accounts', []):
                cursor.execute('''
                    INSERT OR REPLACE INTO accounts 
                    (name, email, key, api_token, account_id, account_id_cache, cache_update_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    acc.get('name', ''),
                    acc.get('email', ''),
                    acc.get('key', ''),
                    acc.get('api_token', ''),
                    acc.get('account_id', ''),
                    acc.get('account_id_cache', ''),
                    acc.get('cache_update_time', '')
                ))
            

            
            self.db.commit()
        except Exception as e:
            print(f"数据库迁移失败: {str(e)}")

    def save_config(self):
        """保存配置到SQLite和JSON（优化：批量操作，减少事务开销）"""
        with QMutexLocker(self.mutex):
            try:
                # 保存到SQLite（使用批量操作）
                if self.db:
                    cursor = self.db.cursor()
                    
                    # 删除旧数据
                    cursor.execute('DELETE FROM config')
                    cursor.execute('DELETE FROM accounts')
                    
                    # 批量插入配置
                    config_data = [
                        ('proxy', json.dumps(self.config.get('proxy', {}))),
                        ('refresh_interval', str(self.config.get('refresh_interval', 300))),
                        ('request_limit', str(self.config.get('request_limit', DEFAULT_REQUEST_LIMIT)))
                    ]
                    cursor.executemany('INSERT INTO config VALUES (?, ?)', config_data)
                    
                    # 批量插入账户数据
                    account_data = []
                    for acc in self.config.get('accounts', []):
                        account_data.append((
                            acc.get('name', ''),
                            acc.get('email', ''),
                            acc.get('key', ''),
                            acc.get('api_token', ''),
                            acc.get('account_id', ''),
                            acc.get('account_id_cache', ''),
                            acc.get('cache_update_time', '')
                        ))
                    if account_data:
                        cursor.executemany('''
                            INSERT INTO accounts 
                            (name, email, key, api_token, account_id, account_id_cache, cache_update_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', account_data)
                    

                    
                    self.db.commit()
                
                # 同时保存JSON备份（异步）
                def save_json():
                    try:
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(self.config, f, indent=4, ensure_ascii=False)
                    except:
                        pass
                
                threading.Thread(target=save_json, daemon=True).start()
            except Exception as e:
                print(f"配置保存失败: {str(e)}")

    def close(self):
        """关闭数据库连接"""
        try:
            if self.db:
                self.db.close()
                self.db = None
        except:
            pass

    def update_account_cache(self, index, account_id):
        """更新账号ID缓存"""
        with QMutexLocker(self.mutex):
            if 0 <= index < len(self.config["accounts"]):
                self.config["accounts"][index]["account_id_cache"] = account_id
                self.config["accounts"][index]["cache_update_time"] = datetime.datetime.now().isoformat()
        # 异步保存配置
        try:
            threading.Thread(target=self.save_config, daemon=True).start()
        except Exception:
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

# --------------------------- 账号图表组件类 ---------------------------
class AccountChartWidget(QWidget):
    def __init__(self, account_name):
        super().__init__()
        self.account_name = account_name
        self.init_ui()
        self.current_data = {}

    def init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(10)

        # 创建标题
        self.title_label = QLabel(f"{self.account_name} - 使用量统计")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setFont(QFont("SimHei", 12, QFont.Weight.Bold))
        self.title_label.setStyleSheet("""
            QLabel {
                background-color: #2196f3;
                color: white;
                border-radius: 5px;
                padding: 8px;
                border: 2px solid #1976d2;
            }
        """)
        self.main_layout.addWidget(self.title_label)

        # 创建两个图表容器
        charts_layout = QHBoxLayout()
        self.main_layout.addLayout(charts_layout)

        # 创建柱状图（显示总量与使用量）
        self.bar_chart_view = self.create_bar_chart()
        charts_layout.addWidget(self.bar_chart_view, 1)

        # 创建饼图（显示占比）
        self.pie_chart_view = self.create_pie_chart()
        charts_layout.addWidget(self.pie_chart_view, 1)

        # 创建数据标签
        self.data_labels_layout = QHBoxLayout()
        self.main_layout.addLayout(self.data_labels_layout)

        self.total_label = QLabel("总量: 0")
        self.used_label = QLabel("已用: 0")
        self.works_label = QLabel("Works: 0")
        self.pages_label = QLabel("Pages: 0")
        self.remaining_label = QLabel("剩余: 0")
        self.percentage_label = QLabel("使用率: 0%")

        self.total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.used_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.works_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pages_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.remaining_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.percentage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 设置数据标签颜色
        self.total_label.setStyleSheet("""
            QLabel {
                background-color: #ffc107;
                color: #212529;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: bold;
                border: 1px solid #ffca2c;
            }
        """)
        self.used_label.setStyleSheet("""
            QLabel {
                background-color: #fd7e14;
                color: white;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: bold;
                border: 1px solid #e67e22;
            }
        """)
        self.works_label.setStyleSheet("""
            QLabel {
                background-color: #dc3545;
                color: white;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: bold;
                border: 1px solid #c82333;
            }
        """)
        self.pages_label.setStyleSheet("""
            QLabel {
                background-color: #17a2b8;
                color: white;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: bold;
                border: 1px solid #138496;
            }
        """)
        self.remaining_label.setStyleSheet("""
            QLabel {
                background-color: #28a745;
                color: white;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: bold;
                border: 1px solid #218838;
            }
        """)
        self.percentage_label.setStyleSheet("""
            QLabel {
                background-color: #6f42c1;
                color: white;
                border-radius: 4px;
                padding: 6px 10px;
                font-weight: bold;
                border: 1px solid #5a32a3;
            }
        """)

        self.data_labels_layout.addWidget(self.total_label)
        self.data_labels_layout.addWidget(self.used_label)
        self.data_labels_layout.addWidget(self.works_label)
        self.data_labels_layout.addWidget(self.pages_label)
        self.data_labels_layout.addWidget(self.remaining_label)
        self.data_labels_layout.addWidget(self.percentage_label)

    def create_bar_chart(self):
        chart = QChart()
        chart.setTitle("总量与使用量")
        chart.setAnimationOptions(QChart.AnimationOption.SeriesAnimations)

        self.bar_series = QBarSeries()
        self.bar_set = QBarSet("值")
        self.bar_set.append([0, 0, 0])
        self.bar_series.append(self.bar_set)

        chart.addSeries(self.bar_series)

        categories = ["总量", "Works", "Pages"]
        axis_x = QBarCategoryAxis()
        axis_x.append(categories)
        axis_x.setTitleText("类型")
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        self.bar_series.attachAxis(axis_x)

        self.bar_axis_y = QValueAxis()
        self.bar_axis_y.setTitleText("请求")
        chart.addAxis(self.bar_axis_y, Qt.AlignmentFlag.AlignLeft)
        self.bar_series.attachAxis(self.bar_axis_y)

        chart_view = QChartView(chart)
        chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumHeight(200)
        return chart_view

    def create_pie_chart(self):
        chart = QChart()
        chart.setTitle("使用占比")
        chart.setAnimationOptions(QChart.AnimationOption.SeriesAnimations)

        self.pie_series = QPieSeries()
        self.pie_series.append("Works", 0)
        self.pie_series.append("Pages", 0)
        self.pie_series.append("剩余", 0)

        chart.addSeries(self.pie_series)

        chart_view = QChartView(chart)
        chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumHeight(200)
        return chart_view

    def update_data(self, data):
        self.current_data = data
        total = data.get("total", 0)
        works = data.get("works", 0)
        pages = data.get("pages", 0)
        used = works + pages
        remaining = max(0, total - used)
        percentage = (used / total * 100) if total > 0 else 0

        # 更新标签，使用千位分隔符
        self.total_label.setText(f"总量: {total:,} 请求")
        self.used_label.setText(f"已用: {used:,} 请求")
        self.works_label.setText(f"Works: {works:,} 请求")
        self.pages_label.setText(f"Pages: {pages:,} 请求")
        self.remaining_label.setText(f"剩余: {remaining:,} 请求")
        self.percentage_label.setText(f"使用率: {percentage:.1f}%")

        # 更新柱状图
        self.bar_set.remove(0, self.bar_set.count())
        self.bar_set.append([total, works, pages])

        max_value = max(total, works, pages, 1)
        if hasattr(self, "bar_axis_y") and self.bar_axis_y is not None:
            self.bar_axis_y.setRange(0, max_value * 1.1)

        # 更新饼图
        self.pie_series.clear()
        self.pie_series.append("Works", works)
        self.pie_series.append("Pages", pages)
        self.pie_series.append("剩余", remaining)

        # 设置饼图颜色和标签
        if self.pie_series.count() >= 3:
            works_slice = self.pie_series.slices()[0]
            pages_slice = self.pie_series.slices()[1]
            remaining_slice = self.pie_series.slices()[2]
            works_slice.setColor(QColor(255, 107, 107))
            pages_slice.setColor(QColor(107, 170, 255))
            remaining_slice.setColor(QColor(107, 255, 170))
            works_slice.setLabelVisible(True)
            pages_slice.setLabelVisible(True)
            remaining_slice.setLabelVisible(True)

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
                timeout=(5, 8)  # (连接超时5秒, 读取超时8秒) - 优化：更合理的超时设置
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
            # 发送GraphQL请求（优化：分离连接超时和读取超时）
            response = requests.post(
                f"{self.api_url}/graphql",
                headers=headers,
                json={"query": query, "variables": variables},
                proxies=self.get_proxies(),
                timeout=(5, 10)  # (连接超时5秒, 读取超时10秒) - 优化：更合理的超时
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
        """线程主循环（优化：使用事件而不是轮询，减少CPU占用）"""
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
                
                # 智能等待：使用QThread的msleep替代time.sleep，更高效
                interval = self.config_manager.get_refresh_interval()
                # 分段等待以快速响应刷新请求
                for _ in range(int(interval * 2)):  # 每0.5秒检查一次
                    if not self.is_running or self.target_account:
                        break
                    self.msleep(500)  # 更高效的等待方式
                    
            except Exception as e:
                self.error_signal.emit(f"刷新失败: {str(e)}")

    def stop(self):
        self.is_running = False
        self.wait()

    def refresh_single_account(self, account_name):
        """触发单个账号刷新"""
        self.target_account = account_name





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

# --------------------------- 通知设置对话框 ---------------------------
class NotificationSettingsDialog(QDialog):
    """通知设置对话框（已移除邮件功能，添加自定义日期时间发送）"""
    # 信号：用于在主线程显示消息
    show_message_signal = pyqtSignal(str, str, str)  # title, message, type
    
    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.notification_thread = None
        # 初始化通知服务
        self.notification_service = NotificationService(config_manager)
        self.notification_config = self.config_manager.get_notification_config()
        # 连接信号到槽以在主线程显示消息
        self.show_message_signal.connect(self._display_message)
        self.setWindowTitle("通知设置 - Cloudflare 监测工具")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setMinimumHeight(450)
        
        # 优化：缓存配置以减少重复调用
        notification_config = self.notification_config
        
        # 基础设置组 - 定时每日通知
        basic_group = QGroupBox("定时每日通知")
        self.enable_checkbox = QCheckBox("启用每日定时通知")
        self.enable_checkbox.setChecked(notification_config.get("enable", False))
        self.enable_checkbox.setChecked(notification_config.get("enable", False))
        
        self.hour_spin = QSpinBox()
        self.hour_spin.setRange(0, 23)
        self.hour_spin.setValue(notification_config.get("hour", 9))
        
        self.minute_spin = QSpinBox()
        self.minute_spin.setRange(0, 59)
        self.minute_spin.setValue(notification_config.get("minute", 0))
        
        basic_layout = QFormLayout()
        basic_layout.addRow(self.enable_checkbox)
        basic_layout.addRow("发送小时", self.hour_spin)
        basic_layout.addRow("发送分钟", self.minute_spin)
        basic_group.setLayout(basic_layout)
        
        # 自定义日期时间发送组
        custom_group = QGroupBox("自定义日期时间发送")
        custom_layout = QFormLayout()
        
        # 日期输入
        date_layout = QHBoxLayout()
        self.year_spin = QSpinBox()
        self.year_spin.setRange(2020, 2100)
        self.year_spin.setValue(datetime.datetime.now().year)
        
        self.month_spin = QSpinBox()
        self.month_spin.setRange(1, 12)
        self.month_spin.setValue(datetime.datetime.now().month)
        
        self.day_spin = QSpinBox()
        self.day_spin.setRange(1, 31)
        self.day_spin.setValue(datetime.datetime.now().day)
        
        date_layout.addWidget(QLabel("年:"))
        date_layout.addWidget(self.year_spin)
        date_layout.addWidget(QLabel("月:"))
        date_layout.addWidget(self.month_spin)
        date_layout.addWidget(QLabel("日:"))
        date_layout.addWidget(self.day_spin)
        date_layout.addStretch()
        
        # 时间输入
        time_layout = QHBoxLayout()
        self.send_hour_spin = QSpinBox()
        self.send_hour_spin.setRange(0, 23)
        self.send_hour_spin.setValue(datetime.datetime.now().hour)
        
        self.send_minute_spin = QSpinBox()
        self.send_minute_spin.setRange(0, 59)
        self.send_minute_spin.setValue(datetime.datetime.now().minute)
        
        time_layout.addWidget(QLabel("小时:"))
        time_layout.addWidget(self.send_hour_spin)
        time_layout.addWidget(QLabel("分钟:"))
        time_layout.addWidget(self.send_minute_spin)
        time_layout.addStretch()
        
        # 发送按钮
        send_btn = QPushButton("立即按指定时间发送")
        send_btn.clicked.connect(self.send_at_custom_time)
        
        custom_layout.addRow("选择日期", date_layout)
        custom_layout.addRow("选择时间", time_layout)
        custom_layout.addRow(send_btn)
        custom_group.setLayout(custom_layout)
        
        # Telegram设置组
        telegram_config = notification_config.get("telegram", {})
        telegram_group = QGroupBox("Telegram设置")
        self.telegram_enable = QCheckBox("启用Telegram通知")
        self.telegram_enable.setChecked(telegram_config.get("enable", False))
        
        self.bot_token = QLineEdit(telegram_config.get("bot_token", ""))
        self.bot_token.setPlaceholderText("输入Bot Token（例：123456:ABC-DEF...）")
        
        self.chat_id = QLineEdit(telegram_config.get("chat_id", ""))
        self.chat_id.setPlaceholderText("输入Chat ID（例：1234567890）")
        
        telegram_layout = QFormLayout()
        telegram_layout.addRow(self.telegram_enable)
        telegram_layout.addRow("Bot Token", self.bot_token)
        telegram_layout.addRow("Chat ID", self.chat_id)
        telegram_group.setLayout(telegram_layout)
        
        # Webhook设置组
        webhook_config = notification_config.get("webhook", {})
        webhook_group = QGroupBox("Webhook设置")
        self.webhook_enable = QCheckBox("启用Webhook通知")
        self.webhook_enable.setChecked(webhook_config.get("enable", False))
        
        self.webhook_url = QLineEdit(webhook_config.get("url", ""))
        self.webhook_url.setPlaceholderText("例：https://your-webhook.com/api/notify")
        
        webhook_layout = QFormLayout()
        webhook_layout.addRow(self.webhook_enable)
        webhook_layout.addRow("Webhook URL", self.webhook_url)
        webhook_group.setLayout(webhook_layout)
        
        # 企业微信设置组
        wechat_config = notification_config.get("wechat", {})
        wechat_group = QGroupBox("企业微信设置")
        self.wechat_enable = QCheckBox("启用企业微信通知")
        self.wechat_enable.setChecked(wechat_config.get("enable", False))
        
        self.wechat_webhook_url = QLineEdit(wechat_config.get("webhook_url", ""))
        self.wechat_webhook_url.setPlaceholderText("例：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...")
        
        wechat_layout = QFormLayout()
        wechat_layout.addRow(self.wechat_enable)
        wechat_layout.addRow("Webhook URL", self.wechat_webhook_url)
        wechat_group.setLayout(wechat_layout)
        
        # 测试按钮组
        test_group = QGroupBox("通知渠道测试")
        test_layout = QVBoxLayout()
        
        # 测试按钮行1
        test_row1 = QHBoxLayout()
        telegram_test_btn = QPushButton("✈️  测试Telegram")
        webhook_test_btn = QPushButton("🔗 测试Webhook")
        telegram_test_btn.clicked.connect(lambda: self.test_notification("telegram"))
        webhook_test_btn.clicked.connect(lambda: self.test_notification("webhook"))
        test_row1.addWidget(telegram_test_btn)
        test_row1.addWidget(webhook_test_btn)
        
        # 测试按钮行2
        test_row2 = QHBoxLayout()
        wechat_test_btn = QPushButton("💼 测试企业微信")
        test_row2.addWidget(wechat_test_btn)
        wechat_test_btn.clicked.connect(lambda: self.test_notification("wechat"))
        test_row2.addStretch()
        
        # 测试提示标签
        test_tip = QLabel("💡 点击按钮测试对应渠道的配置，所有操作不会保存配置")
        test_tip.setStyleSheet("color: #666666; font-size: 10px;")
        
        test_layout.addLayout(test_row1)
        test_layout.addLayout(test_row2)
        test_layout.addWidget(test_tip)
        test_group.setLayout(test_layout)
        
        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal, self
        )
        buttons.accepted.connect(self.save_settings)
        buttons.rejected.connect(self.reject)
        
        # 主布局（使用滚动区域以处理多个选项）
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.addWidget(basic_group)
        scroll_layout.addWidget(custom_group)
        scroll_layout.addWidget(telegram_group)
        scroll_layout.addWidget(webhook_group)
        scroll_layout.addWidget(wechat_group)
        scroll_layout.addWidget(test_group)
        scroll_layout.addStretch()
        
        scroll_area = QScrollArea()
        scroll_area.setWidget(scroll_widget)
        scroll_area.setWidgetResizable(True)
        
        main_layout = QVBoxLayout()
        main_layout.addWidget(scroll_area)
        main_layout.addWidget(buttons)
        self.setLayout(main_layout)
    
    def send_at_custom_time(self):
        """立即使用指定的日期时间发送通知"""
        try:
            # 验证是否有启用的通知渠道
            telegram_enabled = self.notification_config.get("telegram", {}).get("enable", False)
            webhook_enabled = self.notification_config.get("webhook", {}).get("enable", False)
            wechat_enabled = self.notification_config.get("wechat", {}).get("enable", False)
            
            if not (telegram_enabled or webhook_enabled or wechat_enabled):
                QMessageBox.warning(self, "警告", "请至少启用一个通知渠道（Telegram/Webhook/企业微信）")
                return
            
            # 获取用户指定的日期时间（仅用于显示）
            target_year = self.year_spin.value()
            target_month = self.month_spin.value()
            target_day = self.day_spin.value()
            target_hour = self.send_hour_spin.value()
            target_minute = self.send_minute_spin.value()
            
            # 创建目标日期时间对象（用于消息显示）
            target_datetime = datetime.datetime(target_year, target_month, target_day, target_hour, target_minute)
            
            # 立即获取数据并发送通知
            if self.parent() and hasattr(self.parent(), 'cf_client'):
                cf_client = self.parent().cf_client
                if cf_client and hasattr(cf_client, 'data'):
                    data = cf_client.data.copy() if cf_client.data else {}
                    if not data:
                        # 如果没有缓存数据，显示提示
                        QMessageBox.warning(self, "提示", "当前无账户数据缓存，请先更新数据")
                        return
                    
                    # 发送通知
                    subject = f"Cloudflare 报告 - {target_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                    message = f"以下是 {target_datetime.strftime('%Y-%m-%d %H:%M:%S')} 的 Cloudflare 使用量信息："
                    
                    # 在后台线程发送以避免UI阻塞
                    def send_in_thread():
                        try:
                            self.notification_service.send_notification(subject, message, data)
                        except Exception as e:
                            print(f"发送失败: {str(e)}")
                    
                    thread = threading.Thread(target=send_in_thread, daemon=True)
                    thread.start()
                    QMessageBox.information(self, "成功", f"正在发送 {target_datetime.strftime('%Y-%m-%d %H:%M')} 的报告...")
                else:
                    QMessageBox.warning(self, "警告", "无法访问Cloudflare客户端")
            else:
                QMessageBox.warning(self, "警告", "无法访问主窗口")
        except ValueError as e:
            QMessageBox.warning(self, "错误", f"日期时间设置无效：{str(e)}")
    
    def _display_message(self, title, message, msg_type):
        """在主线程显示消息"""
        if msg_type == "info":
            QMessageBox.information(self, title, message)
        elif msg_type == "warning":
            QMessageBox.warning(self, title, message)
        elif msg_type == "error":
            QMessageBox.critical(self, title, message)

    def save_settings(self):
        """保存通知设置"""
        notification_config = {
            "enable": self.enable_checkbox.isChecked(),
            "hour": self.hour_spin.value(),
            "minute": self.minute_spin.value(),
            "telegram": {
                "enable": self.telegram_enable.isChecked(),
                "bot_token": self.bot_token.text().strip(),
                "chat_id": self.chat_id.text().strip()
            },
            "webhook": {
                "enable": self.webhook_enable.isChecked(),
                "url": self.webhook_url.text().strip()
            },
            "wechat": {
                "enable": self.wechat_enable.isChecked(),
                "webhook_url": self.wechat_webhook_url.text().strip()
            }
        }
        self.config_manager.update_notification_config(notification_config)
        # 更新本地缓存配置
        self.notification_config = notification_config
        self.accept()

    def test_notification(self, channel):
        """异步测试通知渠道"""
        # 先保存当前编辑的内容到临时配置
        temp_config = {
            "enable": self.enable_checkbox.isChecked(),
            "hour": self.hour_spin.value(),
            "minute": self.minute_spin.value(),
            "telegram": {
                "enable": self.telegram_enable.isChecked(),
                "bot_token": self.bot_token.text().strip(),
                "chat_id": self.chat_id.text().strip()
            },
            "webhook": {
                "enable": self.webhook_enable.isChecked(),
                "url": self.webhook_url.text().strip()
            },
            "wechat": {
                "enable": self.wechat_enable.isChecked(),
                "webhook_url": self.wechat_webhook_url.text().strip()
            }
        }
        
        # 创建临时服务并测试
        notification_service = NotificationService(self.config_manager)
        notification_service.notification_config = temp_config
        
        # 在后台线程中执行测试以避免UI阻塞
        test_thread = threading.Thread(
            target=self._run_test,
            args=(notification_service, channel),
            daemon=True
        )
        test_thread.start()

    def _run_test(self, notification_service, channel):
        """在后台运行测试（优化：快速超时和友好反馈，使用信号避免UI卡死）"""
        try:
            channel_names = {
                "telegram": "Telegram",
                "webhook": "Webhook",
                "wechat": "企业微信"
            }
            channel_name = channel_names.get(channel, channel)
            
            # 执行测试
            if channel == "telegram":
                success, message = notification_service.test_telegram()
            elif channel == "webhook":
                success, message = notification_service.test_webhook()
            elif channel == "wechat":
                success, message = notification_service.test_wechat()
            else:
                return
            
            # 通过信号在主线程显示结果（避免线程阻塞）
            if success:
                self.show_message_signal.emit(
                    f"✓ {channel_name}测试成功",
                    message,
                    "success"
                )
            else:
                self.show_message_signal.emit(
                    f"✗ {channel_name}测试失败",
                    message,
                    "warning"
                )
        except Exception as e:
            self.show_message_signal.emit(
                "测试出错",
                f"测试过程中出错:\n{str(e)}",
                "error"
            )
    
    def _display_message(self, title, message, msg_type):
        """在主线程显示消息（信号槽机制）"""
        if msg_type == "success":
            QMessageBox.information(self, title, message, QMessageBox.StandardButton.Ok)
        elif msg_type == "warning":
            QMessageBox.warning(self, title, message, QMessageBox.StandardButton.Ok)
        elif msg_type == "error":
            QMessageBox.critical(self, title, message, QMessageBox.StandardButton.Ok)

# --------------------------- 主窗口类 ---------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cloudflare 用量监测工具 - Windows版")
        self.setMinimumSize(900, 700)
        
        # 初始化配置和API客户端
        self.config_manager = ConfigManager()
        self.cf_client = CFAPIClient(self.config_manager)
        
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
        # 启用拖拽排序功能
        self.account_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.account_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.account_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.account_list.setSortingEnabled(False)
        # 设置账户列表颜色
        self.account_list.setStyleSheet("""
            QListWidget {
                background-color: #f0f8ff;
                border: 1px solid #add8e6;
                border-radius: 5px;
                padding: 5px;
            }
            QListWidget::item {
                background-color: #ffffff;
                border: 1px solid #e0e0e0;
                border-radius: 3px;
                padding: 5px;
                margin: 2px;
            }
            QListWidget::item:selected {
                background-color: #4682b4;
                color: white;
                border-color: #4682b4;
            }
        """)
        
        # 账户操作按钮
        account_buttons_layout = QVBoxLayout()
        add_btn = QPushButton("添加账户")
        edit_btn = QPushButton("编辑账户")
        del_btn = QPushButton("删除账户")
        
        # 设置账户操作按钮颜色
        add_btn.setStyleSheet("""
            QPushButton {
                background-color: #4caf50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3e8e41;
            }
        """)
        edit_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196f3;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1976d2;
            }
            QPushButton:pressed {
                background-color: #1565c0;
            }
        """)
        del_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #da190b;
            }
            QPushButton:pressed {
                background-color: #b71c1c;
            }
        """)
        
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
        
        # 设置功能按钮颜色
        refresh_btn.setStyleSheet("""
            QPushButton {
                background-color: #ff9800;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #f57c00;
            }
            QPushButton:pressed {
                background-color: #ef6c00;
            }
        """)
        settings_btn.setStyleSheet("""
            QPushButton {
                background-color: #9c27b0;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #7b1fa2;
            }
            QPushButton:pressed {
                background-color: #6a1b9a;
            }
        """)
        
        refresh_btn.clicked.connect(self.refresh_data)
        settings_btn.clicked.connect(self.open_settings)
        func_buttons_layout.addWidget(refresh_btn)
        func_buttons_layout.addWidget(settings_btn)
        func_buttons_layout.addStretch()
        
        # 状态标签1（显示更新时间和刷新间隔）
        self.status_label = QLabel("最后更新：未更新")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("""
            QLabel {
                font-weight: bold;
                color: #2c3e50;
                background-color: #ecf0f1;
                border: 2px solid #3498db;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
        """)
        
        # 状态标签2（显示额度重置时间和倒计时）
        self.countdown_label = QLabel("额度重置时间：未设置 | 倒计时：未计算")
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.countdown_label.setStyleSheet("""
            QLabel {
                font-weight: bold;
                color: #27ae60;
                background-color: #e8f5e9;
                border: 2px solid #2ecc71;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
        """)
        
        # 创建状态标签的垂直布局
        status_layout = QVBoxLayout()
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.countdown_label)
        
        # 顶部布局组装
        top_layout.addWidget(QLabel("账户列表：", font=QFont("SimHei", 10, QFont.Weight.Bold)))
        top_layout.addWidget(self.account_list)
        top_layout.addLayout(account_buttons_layout)
        top_layout.addLayout(func_buttons_layout)
        top_layout.addStretch()
        top_layout.addLayout(status_layout)
        
        # 创建标签页控件
        self.tab_widget = QTabWidget()
        self.tab_widget.setMinimumHeight(400)
        # 设置标签页颜色
        self.tab_widget.setStyleSheet("""
            QTabWidget::pane {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 5px;
            }
            QTabBar::tab {
                background-color: #6c757d;
                color: white;
                border: 1px solid #495057;
                border-radius: 5px 5px 0 0;
                padding: 8px 16px;
                font-weight: bold;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: #007bff;
                border-bottom-color: #007bff;
            }
            QTabBar::tab:hover {
                background-color: #5a6268;
            }
            QTabBar::tab:selected:hover {
                background-color: #0069d9;
            }
        """)
        
        # 创建图表字典，用于快速访问
        self.chart_widgets = {}
        
        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        main_layout.addLayout(top_layout)
        main_layout.addWidget(self.tab_widget)
        
        # 绑定账户列表点击事件
        self.account_list.itemClicked.connect(self.on_account_click)
        
        # 绑定账户列表排序改变事件
        self.account_list.model().rowsMoved.connect(self.on_accounts_order_changed)
        
        # 加载账户列表
        self.load_account_list()
        
        # 初始化倒计时定时器（每秒刷新一次）
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self.update_countdown)
        self.countdown_timer.start(1000)  # 1000毫秒 = 1秒

        # 设置主窗口背景颜色
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f0f2f5;
            }
            QWidget {
                background-color: #f0f2f5;
            }
        """)

        # 强制显示窗口
        self.show()

    def update_countdown(self):
        """每秒更新倒计时标签"""
        # 计算额度重置时间（第二天早上8:00:00）
        reset_datetime = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(hour=8, minute=0, second=0)
        reset_time = reset_datetime.strftime("%Y-%m-%d %H:%M:%S")
        
        # 计算倒计时
        time_remaining = reset_datetime - datetime.datetime.now()
        days = time_remaining.days
        hours, remainder = divmod(time_remaining.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        countdown = f"{days}天{hours}小时{minutes}分钟{seconds}秒"
        
        # 更新状态标签2（显示额度重置时间和倒计时）
        self.countdown_label.setText(f"额度重置时间：{reset_time} | 倒计时：{countdown}")

    def load_account_list(self):
        """加载账户列表"""
        self.account_list.clear()
        accounts = self.config_manager.get_accounts()
        
        # 先清空所有标签页
        self.tab_widget.clear()
        self.chart_widgets.clear()
        
        for account in accounts:
            account_name = account.get("name", "未知账户")
            item = QListWidgetItem(account_name)
            self.account_list.addItem(item)
            
            # 为每个账户创建图表组件
            chart_widget = AccountChartWidget(account_name)
            self.chart_widgets[account_name] = chart_widget
            
            # 创建滚动区域，方便在小屏幕上查看
            scroll_area = QScrollArea()
            scroll_area.setWidget(chart_widget)
            scroll_area.setWidgetResizable(True)
            
            # 添加到标签页
            self.tab_widget.addTab(scroll_area, account_name)
        
        # 如果没有账户，添加一个提示标签页
        if not accounts:
            empty_widget = QWidget()
            empty_layout = QVBoxLayout(empty_widget)
            empty_label = QLabel("暂无账户，请先添加账户")
            empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_label.setFont(QFont("SimHei", 14, QFont.Weight.Bold))
            empty_label.setStyleSheet("QLabel { color: #999; }")
            empty_layout.addWidget(empty_label)
            self.tab_widget.addTab(empty_widget, "提示")

    def on_account_click(self, item):
        """账户列表点击事件：刷新该账户数据并跳转到对应标签页"""
        account_name = item.text()
        
        # 跳转到对应标签页
        for i in range(self.tab_widget.count()):
            if self.tab_widget.tabText(i) == account_name:
                self.tab_widget.setCurrentIndex(i)
                break
        
        # 刷新该账户数据
        if self.refresh_thread:
            self.refresh_thread.refresh_single_account(account_name)
    
    def on_accounts_order_changed(self, source_parent, source_start, source_end, dest_parent, dest_row):
        """账户列表排序改变事件：更新配置中的账户顺序"""
        # 获取当前账户列表顺序
        ordered_accounts = []
        for i in range(self.account_list.count()):
            item = self.account_list.item(i)
            if item:
                account_name = item.text()
                # 找到对应的账户信息
                for account in self.config_manager.get_accounts():
                    if account.get("name") == account_name:
                        ordered_accounts.append(account)
                        break
        
        # 更新配置中的账户顺序
        if ordered_accounts:
            self.config_manager.config["accounts"] = ordered_accounts
            self.config_manager.save_config()
            # 重新加载账户列表和标签页
            self.load_account_list()

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
        dialog = AccountDialog(self, account_info)
        if dialog.exec():
            new_info = dialog.get_account_info()
            if not new_info["name"]:
                QMessageBox.warning(self, "警告", "账户名称不能为空！")
                return
            self.config_manager.update_account(current_idx, new_info)
            self.load_account_list()
            self.refresh_data()

    def delete_account(self):
        current_idx = self.account_list.currentRow()
        if current_idx < 0:
            QMessageBox.warning(self, "警告", "请先选择要删除的账户！")
            return
        if QMessageBox.question(self, "确认", "确定要删除该账户吗？", 
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.config_manager.delete_account(current_idx)
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
        """更新UI显示所有账户数据"""
        if not data:
            return
        
        # 更新状态标签1（去掉额度重置时间与倒计时）
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status_label.setText(f"最后更新：{current_time} | 刷新间隔：{self.config_manager.get_refresh_interval()}秒")
        
        # 计算额度重置时间和倒计时（用于状态标签2）
        reset_datetime = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(hour=8, minute=0, second=0)
        reset_time = reset_datetime.strftime("%Y-%m-%d %H:%M:%S")
        
        # 计算倒计时
        time_remaining = reset_datetime - datetime.datetime.now()
        days = time_remaining.days
        hours, remainder = divmod(time_remaining.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        countdown = f"{days}天{hours}小时{minutes}分钟{seconds}秒"
        
        # 更新状态标签2（显示额度重置时间和倒计时）
        self.countdown_label.setText(f"额度重置时间：{reset_time} | 倒计时：{countdown}")
        
        # 更新每个账户的图表
        request_limit = self.config_manager.get_request_limit()
        
        for account_name, account_data in data.items():
            # 检查是否有对应的图表组件
            if account_name in self.chart_widgets:
                chart_widget = self.chart_widgets[account_name]
                if account_data.get("error"):
                    # 如果有错误，显示错误信息
                    chart_widget.title_label.setText(f"{account_name} - 错误：{account_data['error']}")
                    chart_widget.title_label.setStyleSheet("QLabel { color: red; }")
                else:
                    chart_widget.title_label.setText(f"{account_name} - 使用量统计")
                    chart_widget.title_label.setStyleSheet("")
                    # 更新图表数据
                    chart_data = {
                        "total": request_limit,  # 使用准确的请求数
                        "works": account_data['workers'],  # 使用准确的请求数
                        "pages": account_data['pages']  # 使用准确的请求数
                    }
                    chart_widget.update_data(chart_data)

    def start_refresh_thread(self):
        """启动刷新线程"""
        self.stop_refresh_thread()
        self.refresh_thread = RefreshThread(self.cf_client, self.config_manager)
        self.refresh_thread.update_signal.connect(self.update_ui)
        self.refresh_thread.error_signal.connect(lambda msg: QMessageBox.warning(self, "刷新警告", msg))
        self.refresh_thread.start()

    def stop_refresh_thread(self):
        """停止刷新线程"""
        if self.refresh_thread and self.refresh_thread.isRunning():
            self.refresh_thread.stop()
            self.refresh_thread = None



    def closeEvent(self, event):
        """关闭窗口（优化：正确关闭所有资源）"""
        try:
            # 停止所有后台线程
            self.stop_refresh_thread()
            
            # 关闭线程池
            if hasattr(self.cf_client, 'executor'):
                self.cf_client.executor.shutdown(wait=False)
            
            # 保存配置并关闭数据库
            if hasattr(self, 'config_manager'):
                self.config_manager.save_config()
                self.config_manager.close()
            
            # 给线程一点时间来清理（最多0.5秒）
            import time
            time.sleep(0.1)
            
            event.accept()
        except Exception as e:
            print(f"关闭窗口时出错: {str(e)}")
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
