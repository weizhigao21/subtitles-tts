import sys
import os
import re
import json
import threading
import queue
import time
import requests
import ctypes
from PyQt6.QtWidgets import (
    QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QTextEdit, QSpinBox, QMessageBox, QProgressBar,
    QListWidget, QTabWidget, QCheckBox, QGroupBox,
    QComboBox, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QFont

# 防休眠常量
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

def set_sleep_mode(prevent=True):
    if os.name == 'nt':
        try:
            if prevent:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception as e:
            print(f"防休眠设置失败: {e}")

def generate_filename(index, timestamp, text, save_dir):
    # 提取前两个字
    safe_text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', text)
    short_text = safe_text[:2] if safe_text else "无"
    file_name = f"{index:04d}_{timestamp}_{short_text}.wav"
    return os.path.join(save_dir, file_name)

def tts_task(index, timestamp, text, api_url, model_name, save_dir):
    api_base_url = api_url.rstrip('/')
    api_endpoint = f"{api_base_url}/infer_single"
    
    save_path = generate_filename(index, timestamp, text, save_dir)
    file_name = os.path.basename(save_path)
    
    payload = {
        "batch_size": 10,
        "batch_threshold": 0.75,
        "dl_url": api_base_url,
        "emotion": "默认",
        "fragment_interval": 0.3,
        "if_sr": False,
        "media_type": "wav",
        "model_name": model_name,
        "parallel_infer": True,
        "prompt_text_lang": "中文",
        "repetition_penalty": 1.35,
        "sample_steps": 16,
        "seed": -1,
        "speed_facter": 1,
        "split_bucket": True,
        "text": text,
        "text_lang": "中文",
        "text_split_method": "按标点符号切",
        "top_k": 10,
        "top_p": 1,
        "version": "v4"
    }
    
    try:
        resp = requests.post(api_endpoint, json=payload, timeout=60)
        resp.raise_for_status()
        res_data = resp.json()
        if "audio_url" in res_data:
            audio_url = res_data["audio_url"]
            audio_resp = requests.get(audio_url)
            with open(save_path, 'wb') as f:
                f.write(audio_resp.content)
            return True, f"完成: {file_name}"
        return False, f"错误: {res_data.get('msg', '无返回URL')}"
    except Exception as e:
        return False, f"异常: {str(e)}"

class TTSWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    total_tasks_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(bool)
    
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.paused = False
        self.stop_flag = False
        self.pause_lock = threading.Lock()
    
    def pause(self):
        with self.pause_lock:
            self.paused = True
            self.log_signal.emit("合成已暂停")
    
    def resume(self):
        with self.pause_lock:
            self.paused = False
            self.log_signal.emit("合成已恢复")
    
    def stop(self):
        self.stop_flag = True
        self.resume()  # 恢复线程以便退出
    
    def is_paused(self):
        with self.pause_lock:
            return self.paused
    
    def run(self):
        prevent_sleep = self.config.get('prevent_sleep', True)
        if prevent_sleep:
            set_sleep_mode(True)
            self.log_signal.emit("防休眠已启用")
        
        try:
            lrc_files = self.config['lrc_files']
            base_output = os.path.abspath(self.config['output_dir'])
            all_tasks = []
            lrc_pattern = re.compile(r'\[(\d{2}:\d{2}\.\d{2})\](.*)')
            
            # 生成任务ID并创建任务文件夹
            # 使用字幕文件名称的MD5值作为任务ID，确保相同文件生成相同ID
            import hashlib
            # 收集所有字幕文件名称并排序（确保顺序一致）
            file_names = sorted([os.path.basename(f) for f in lrc_files])
            # 计算MD5哈希值
            md5_hash = hashlib.md5(''.join(file_names).encode('utf-8')).hexdigest()
            task_id = md5_hash[:8]  # 使用前8位作为任务ID
            task_dir = os.path.join(base_output, task_id)
            os.makedirs(task_dir, exist_ok=True)
            self.log_signal.emit(f"开始新任务：任务ID = {task_id}")
            self.log_signal.emit(f"任务文件夹：{task_dir}")
            self.log_signal.emit(f"基于 {len(file_names)} 个字幕文件计算MD5生成任务ID")
            
            for lrc_path in lrc_files:
                lrc_name = os.path.splitext(os.path.basename(lrc_path))[0]
                lrc_name = re.sub(r'[\\/:*?"<>|]', '', lrc_name).strip()
                save_dir = os.path.join(task_dir, lrc_name)
                os.makedirs(save_dir, exist_ok=True)
                
                try:
                    with open(lrc_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                except:
                    try:
                        with open(lrc_path, 'r', encoding='gbk') as f:
                            lines = f.readlines()
                    except Exception as e:
                        self.log_signal.emit(f"读取文件失败 {lrc_path}: {e}")
                        continue
                
                idx = 1
                file_ext = os.path.splitext(lrc_path)[1].lower()
                
                if file_ext == '.lrc':
                    # LRC格式解析
                    for line in lines:
                        match = lrc_pattern.match(line.strip())
                        if match and match.group(2).strip():
                            timestamp = match.group(1).replace(':', '-').replace('.', '-')
                            text = match.group(2).strip()
                            all_tasks.append((idx, timestamp, text, save_dir))
                            idx += 1
                elif file_ext == '.vtt':
                    # VTT格式解析
                    # 跳过WEBVTT头
                    is_content = False
                    current_text = []
                    current_timestamp = None
                    
                    # VTT时间戳格式：00:00:00.000 --> 00:00:02.000
                    vtt_time_pattern = re.compile(r'^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) -->')
                    
                    for line in lines:
                        line = line.strip()
                        
                        if line == 'WEBVTT' or line.startswith('NOTE'):
                            continue
                        
                        if not line:
                            # 空行，处理之前的字幕
                            if current_timestamp and current_text:
                                text = ' '.join(current_text).strip()
                                if text:
                                    all_tasks.append((idx, current_timestamp, text, save_dir))
                                    idx += 1
                                current_text = []
                                current_timestamp = None
                            continue
                        
                        # 检查是否是时间戳行
                        time_match = vtt_time_pattern.match(line)
                        if time_match:
                            # 提取开始时间：HH:MM:SS.mmm -> MM:SS.mm -> MM-SS-mm
                            hours = time_match.group(1)
                            minutes = time_match.group(2)
                            seconds = time_match.group(3)
                            milliseconds = time_match.group(4)[:2]  # 只取前两位
                            
                            # 转换为 LRC 格式的时间戳：MM-SS-mm
                            timestamp_str = f"{minutes}-{seconds}-{milliseconds}"
                            current_timestamp = timestamp_str
                            continue
                        
                        # 是文本内容
                        if current_timestamp:
                            current_text.append(line)
                    
                    # 处理文件末尾的字幕
                    if current_timestamp and current_text:
                        text = ' '.join(current_text).strip()
                        if text:
                            all_tasks.append((idx, current_timestamp, text, save_dir))
                            idx += 1
                elif file_ext == '.srt':
                    # SRT格式解析
                    current_text = []
                    current_timestamp = None
                    
                    # SRT时间戳格式：00:00:00,000 --> 00:00:02,000（注意是逗号分隔毫秒）
                    srt_time_pattern = re.compile(r'^(\d{2}):(\d{2}):(\d{2}),(\d{3}) -->')
                    
                    for line in lines:
                        line = line.strip()
                        
                        if not line:
                            # 空行，处理之前的字幕
                            if current_timestamp and current_text:
                                text = ' '.join(current_text).strip()
                                if text:
                                    all_tasks.append((idx, current_timestamp, text, save_dir))
                                    idx += 1
                                current_text = []
                                current_timestamp = None
                            continue
                        
                        # 跳过序号行（纯数字行）
                        if line.isdigit():
                            continue
                        
                        # 检查是否是时间戳行
                        time_match = srt_time_pattern.match(line)
                        if time_match:
                            # 提取开始时间：HH:MM:SS,mmm -> MM:SS.mm -> MM-SS-mm
                            hours = time_match.group(1)
                            minutes = time_match.group(2)
                            seconds = time_match.group(3)
                            milliseconds = time_match.group(4)[:2]  # 只取前两位
                            
                            # 转换为统一格式
                            timestamp_str = f"{minutes}-{seconds}-{milliseconds}"
                            current_timestamp = timestamp_str
                            continue
                        
                        # 是文本内容
                        if current_timestamp:
                            current_text.append(line)
                    
                    # 处理文件末尾的字幕
                    if current_timestamp and current_text:
                        text = ' '.join(current_text).strip()
                        if text:
                            all_tasks.append((idx, current_timestamp, text, save_dir))
                            idx += 1
            
            if not all_tasks:
                self.log_signal.emit("未找到有效歌词")
                self.finished_signal.emit(False)
                return
            
            total = len(all_tasks)
            self.total_tasks_signal.emit(total)
            completed = 0
            
            # 检查已存在的文件，跳过已生成的任务
            pending_tasks = []
            for task in all_tasks:
                idx, timestamp, text, save_dir = task
                file_path = generate_filename(idx, timestamp, text, save_dir)
                if os.path.exists(file_path):
                    completed += 1
                    self.progress_signal.emit(completed)
                    file_name = os.path.basename(file_path)
                    self.log_signal.emit(f"跳过已存在文件: {file_name}")
                else:
                    pending_tasks.append(task)
            
            skipped_count = total - len(pending_tasks)
            if skipped_count > 0:
                self.log_signal.emit(f"发现 {skipped_count} 个文件已存在，已跳过")
            self.log_signal.emit(f"剩余 {len(pending_tasks)} 个任务待处理")
            
            # 确定要使用的API配置
            if self.config.get('use_multi_api', False):
                # 使用所有在线的API
                available_apis = [api for api in self.config.get('api_configs', []) if api.get('status') == 'success']
                if not available_apis:
                    # 如果没有在线API，使用当前选中的API
                    current_idx = self.config.get('current_api_index', 0)
                    if current_idx < len(self.config.get('api_configs', [])):
                        available_apis = [self.config['api_configs'][current_idx]]
                    else:
                        available_apis = self.config.get('api_configs', [])[:1]
                self.log_signal.emit(f"使用多API模式，共 {len(available_apis)} 个在线API")
            else:
                # 使用当前选中的API
                current_idx = self.config.get('current_api_index', 0)
                if current_idx < len(self.config.get('api_configs', [])):
                    available_apis = [self.config['api_configs'][current_idx]]
                else:
                    available_apis = self.config.get('api_configs', [])[:1]
                self.log_signal.emit("使用单API模式")
            
            if not available_apis:
                self.log_signal.emit("错误：没有可用的API配置")
                self.finished_signal.emit(False)
                return
            
            # 如果没有待处理任务，直接完成
            if len(pending_tasks) == 0:
                self.log_signal.emit("所有文件已存在，无需处理")
                self.finished_signal.emit(True)
                return
            
            # 并行执行：创建任务队列和线程池
            task_queue = queue.Queue()
            for task in pending_tasks:
                task_queue.put(task)
            
            # 创建线程锁用于进度更新
            progress_lock = threading.Lock()
            
            # 定义工作线程函数
            def worker(api_config):
                nonlocal completed
                while True:
                    # 检查停止标志
                    if self.stop_flag:
                        break
                    
                    # 检查暂停标志
                    while self.is_paused():
                        if self.stop_flag:
                            break
                        time.sleep(0.1)  # 暂停检查间隔
                    
                    if self.stop_flag:
                        break
                    
                    try:
                        # 非阻塞获取任务，超时1秒
                        task = task_queue.get(timeout=1)
                    except queue.Empty:
                        break
                    
                    try:
                        # 再次检查暂停和停止标志
                        if self.stop_flag:
                            task_queue.task_done()
                            break
                        
                        while self.is_paused():
                            if self.stop_flag:
                                task_queue.task_done()
                                break
                            time.sleep(0.1)
                        
                        if self.stop_flag:
                            task_queue.task_done()
                            break
                        
                        success, msg = tts_task(task[0], task[1], task[2], api_config['url'], api_config['model'], task[3])
                        # 在日志中添加API名称信息
                        if success:
                            self.log_signal.emit(f"[{api_config['name']}] {msg}")
                        else:
                            self.log_signal.emit(f"[{api_config['name']}] {msg}")
                        
                        # 更新进度
                        with progress_lock:
                            completed += 1
                            self.progress_signal.emit(completed)
                    finally:
                        task_queue.task_done()
            
            # 创建并启动工作线程
            threads = []
            for api_config in available_apis:
                thread = threading.Thread(target=worker, args=(api_config,))
                thread.daemon = True  # 守护线程，主线程退出时自动退出
                threads.append(thread)
                thread.start()
            
            # 等待所有任务完成
            task_queue.join()
            
            # 等待所有线程完成
            for thread in threads:
                thread.join(timeout=1)  # 1秒超时，防止线程无限等待
            
            self.log_signal.emit(f"全部任务完成")
            self.log_signal.emit(f"任务ID: {task_id}")
            self.log_signal.emit(f"任务文件保存位置: {task_dir}")
            self.finished_signal.emit(True)
            
        except Exception as e:
            self.log_signal.emit(f"处理出错: {e}")
            self.finished_signal.emit(False)
        finally:
            if prevent_sleep:
                set_sleep_mode(False)
                self.log_signal.emit("防休眠已关闭")

CONFIG_FILE = "tts_config.json"

class TTSApp(QWidget):
    def __init__(self):
        super().__init__()
        self.lrc_files = []
        self.api_configs = []
        self.current_api_index = 0
        self.prevent_sleep = True
        self.use_multi_api = False
        self.load_config()
        self.init_ui()
        # 设置窗口为可接受拖拽
        self.setAcceptDrops(True)
    
    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                
                # 加载API配置
                if 'api_configs' in config:
                    self.api_configs = config['api_configs']
                    # 确保每个配置都有status字段
                    for api in self.api_configs:
                        if 'status' not in api:
                            api['status'] = 'unknown'
                
                # 加载当前API索引
                if 'current_api_index' in config:
                    idx = config['current_api_index']
                    if 0 <= idx < len(self.api_configs):
                        self.current_api_index = idx
                    else:
                        self.current_api_index = 0
                
                # 加载复选框状态
                if 'prevent_sleep' in config:
                    self.prevent_sleep = bool(config['prevent_sleep'])
                if 'use_multi_api' in config:
                    self.use_multi_api = bool(config['use_multi_api'])
                
                # 如果没有API配置，添加默认配置
                if not self.api_configs:
                    self.api_configs = [{
                        'name': '本地服务器',
                        'url': 'http://127.0.0.1:8000',
                        'model': '八重神子_ZH',
                        'status': 'unknown'
                    }]
                    self.current_api_index = 0
                    
            else:
                # 配置文件不存在，使用默认配置
                self.api_configs = [{
                    'name': '本地服务器',
                    'url': 'http://127.0.0.1:8000',
                    'model': '八重神子_ZH',
                    'status': 'unknown'
                }]
                self.current_api_index = 0
                
        except Exception as e:
            print(f"加载配置失败: {e}")
            # 使用默认配置
            self.api_configs = [{
                'name': '本地服务器',
                'url': 'http://127.0.0.1:8000',
                'model': '八重神子_ZH',
                'status': 'unknown'
            }]
            self.current_api_index = 0
    
    def save_config(self):
        try:
            config = {
                'api_configs': self.api_configs,
                'current_api_index': self.current_api_index,
                'prevent_sleep': self.prevent_sleep,
                'use_multi_api': self.use_multi_api
            }
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存配置失败: {e}")
    
    def on_prevent_sleep_changed(self, state):
        self.prevent_sleep = (state == Qt.CheckState.Checked.value)
        self.save_config()
    
    def on_multi_api_changed(self, state):
        self.use_multi_api = (state == Qt.CheckState.Checked.value)
        self.save_config()
    
    def init_ui(self):
        self.setWindowTitle("TTS字幕合成工具")
        self.resize(900, 700)
        
        layout = QVBoxLayout()
        
        # 选项卡
        self.tabs = QTabWidget()
        self.batch_tab = QWidget()
        self.settings_tab = QWidget()
        self.tabs.addTab(self.batch_tab, "批量合成")
        self.tabs.addTab(self.settings_tab, "设置")
        layout.addWidget(self.tabs)
        
        # 初始化各个选项卡
        self.init_batch_tab()
        self.init_settings_tab()
        
        self.setLayout(layout)
    
    def init_batch_tab(self):
        layout = QVBoxLayout()
        
        # API选择
        api_layout = QHBoxLayout()
        api_layout.addWidget(QLabel("当前API:"))
        self.api_combo = QComboBox()
        
        # API状态指示器
        self.api_status_label = QLabel("●")
        self.api_status_label.setFont(QFont("Arial", 14))
        self.api_status_label.setStyleSheet("color: gray")
        api_layout.addWidget(self.api_status_label)
        
        api_layout.addWidget(self.api_combo)
        self.api_combo.currentIndexChanged.connect(self.on_api_changed)
        self.update_api_combo()
        
        layout.addLayout(api_layout)
        
        # 文件列表
        layout.addWidget(QLabel("字幕文件列表:"))
        self.file_list = QListWidget()
        self.file_list.setFixedHeight(150)
        layout.addWidget(self.file_list)
        
        # 按钮
        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton("添加字幕")
        self.add_btn.clicked.connect(self.add_lrc_files)
        self.clear_btn = QPushButton("清除列表")
        self.clear_btn.clicked.connect(self.clear_lrc_files)
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.clear_btn)
        layout.addLayout(btn_layout)
        
        # 进度条
        layout.addWidget(QLabel("进度:"))
        self.progress = QProgressBar()
        self.progress.setFormat("任务: %v/%m (%p%)")
        layout.addWidget(self.progress)
        
        # 日志
        layout.addWidget(QLabel("日志:"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        
        # 控制按钮
        control_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始合成")
        self.start_btn.clicked.connect(self.start_processing)
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.clicked.connect(self.pause_processing)
        self.pause_btn.setEnabled(False)  # 初始状态禁用
        control_layout.addWidget(self.start_btn)
        control_layout.addWidget(self.pause_btn)
        layout.addLayout(control_layout)
        
        self.batch_tab.setLayout(layout)
    
    def init_settings_tab(self):
        layout = QVBoxLayout()
        
        # API管理组
        api_group = QGroupBox("API管理")
        api_group_layout = QVBoxLayout()
        
        # API表格
        self.api_table = QTableWidget()
        self.api_table.setColumnCount(4)
        self.api_table.setHorizontalHeaderLabels(["名称", "服务器地址", "模型名称", "状态"])
        self.api_table.horizontalHeader().setStretchLastSection(True)
        self.api_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.api_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        api_group_layout.addWidget(self.api_table)
        
        # API表格按钮
        table_btn_layout = QHBoxLayout()
        self.add_api_btn = QPushButton("添加API")
        self.add_api_btn.clicked.connect(self.add_api_config)
        self.edit_api_btn = QPushButton("编辑选中")
        self.edit_api_btn.clicked.connect(self.edit_api_config)
        self.delete_api_btn = QPushButton("删除选中")
        self.delete_api_btn.clicked.connect(self.delete_api_config)
        table_btn_layout.addWidget(self.add_api_btn)
        table_btn_layout.addWidget(self.edit_api_btn)
        table_btn_layout.addWidget(self.delete_api_btn)
        api_group_layout.addLayout(table_btn_layout)
        
        api_group.setLayout(api_group_layout)
        layout.addWidget(api_group)
        
        # 防休眠选项
        self.prevent_sleep_check = QCheckBox("防止电脑休眠")
        self.prevent_sleep_check.setChecked(self.prevent_sleep)
        self.prevent_sleep_check.stateChanged.connect(self.on_prevent_sleep_changed)
        layout.addWidget(self.prevent_sleep_check)
        
        # 多API并行执行选项
        self.multi_api_check = QCheckBox("启用多API并行执行（使用所有在线API）")
        self.multi_api_check.setChecked(self.use_multi_api)
        self.multi_api_check.stateChanged.connect(self.on_multi_api_changed)
        layout.addWidget(self.multi_api_check)
        
        # 刷新链接和清理缓存按钮
        refresh_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新链接")
        self.refresh_btn.clicked.connect(self.refresh_all_connections)
        self.clear_cache_btn = QPushButton("清理缓存")
        self.clear_cache_btn.clicked.connect(self.clear_cache)
        refresh_layout.addWidget(self.refresh_btn)
        refresh_layout.addWidget(self.clear_cache_btn)
        layout.addLayout(refresh_layout)
        
        layout.addStretch()
        self.settings_tab.setLayout(layout)
        
        # 更新API表格
        self.update_api_table()
    
    def update_api_combo(self):
        self.api_combo.clear()
        for config in self.api_configs:
            status_symbol = "●"
            if config['status'] == 'success':
                status_symbol = "✓"
            elif config['status'] == 'failed':
                status_symbol = "✗"
            self.api_combo.addItem(f"{config['name']} ({status_symbol})")
    
    def update_api_table(self):
        self.api_table.setRowCount(len(self.api_configs))
        for i, config in enumerate(self.api_configs):
            self.api_table.setItem(i, 0, QTableWidgetItem(config['name']))
            self.api_table.setItem(i, 1, QTableWidgetItem(config['url']))
            self.api_table.setItem(i, 2, QTableWidgetItem(config['model']))
            
            status_item = QTableWidgetItem()
            if config['status'] == 'success':
                status_item.setText("✓ 连接成功")
                status_item.setForeground(Qt.GlobalColor.green)
            elif config['status'] == 'failed':
                status_item.setText("✗ 连接失败")
                status_item.setForeground(Qt.GlobalColor.red)
            else:
                status_item.setText("● 未测试")
                status_item.setForeground(Qt.GlobalColor.gray)
            self.api_table.setItem(i, 3, status_item)
        
        # 调整列宽
        self.api_table.resizeColumnsToContents()
    
    def update_api_status_label(self):
        if not hasattr(self, 'api_status_label'):
            return
        if self.current_api_index < len(self.api_configs):
            config = self.api_configs[self.current_api_index]
            if config['status'] == 'success':
                self.api_status_label.setText("✓")
                self.api_status_label.setStyleSheet("color: green")
            elif config['status'] == 'failed':
                self.api_status_label.setText("✗")
                self.api_status_label.setStyleSheet("color: red")
            else:
                self.api_status_label.setText("●")
                self.api_status_label.setStyleSheet("color: gray")
    
    def add_lrc_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择字幕文件", "", "字幕文件 (*.lrc *.vtt *.srt)")
        for file in files:
            if file not in self.lrc_files:
                self.lrc_files.append(file)
                self.file_list.addItem(os.path.basename(file))
    
    def clear_lrc_files(self):
        self.lrc_files.clear()
        self.file_list.clear()
    
    def on_api_changed(self, index):
        if 0 <= index < len(self.api_configs):
            self.current_api_index = index
            self.update_api_status_label()
            self.save_config()
    
    def add_api_config(self):
        # 简单实现：添加一个默认的新API配置
        new_config = {
            'name': f'服务器{len(self.api_configs)+1}',
            'url': 'http://127.0.0.1:8000',
            'model': '八重神子_ZH',
            'status': 'unknown'
        }
        self.api_configs.append(new_config)
        self.update_api_table()
        self.update_api_combo()
        self.api_combo.setCurrentIndex(len(self.api_configs) - 1)
        self.save_config()
    
    def edit_api_config(self):
        selected_rows = self.api_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "警告", "请先选择一个API配置")
            return
        
        row = selected_rows[0].row()
        if 0 <= row < len(self.api_configs):
            config = self.api_configs[row]
            
            # 简单的编辑对话框
            dialog = QDialog()
            dialog.setWindowTitle("编辑API配置")
            dialog.resize(400, 200)
            
            layout = QVBoxLayout()
            
            # 名称
            name_layout = QHBoxLayout()
            name_layout.addWidget(QLabel("名称:"))
            name_input = QLineEdit(config['name'])
            name_layout.addWidget(name_input)
            layout.addLayout(name_layout)
            
            # 服务器地址
            url_layout = QHBoxLayout()
            url_layout.addWidget(QLabel("服务器地址:"))
            url_input = QLineEdit(config['url'])
            url_layout.addWidget(url_input)
            layout.addLayout(url_layout)
            
            # 模型名称
            model_layout = QHBoxLayout()
            model_layout.addWidget(QLabel("模型名称:"))
            model_input = QLineEdit(config['model'])
            model_layout.addWidget(model_input)
            layout.addLayout(model_layout)
            
            # 按钮
            btn_layout = QHBoxLayout()
            save_btn = QPushButton("保存")
            cancel_btn = QPushButton("取消")
            
            def save_config():
                config['name'] = name_input.text()
                config['url'] = url_input.text()
                config['model'] = model_input.text()
                config['status'] = 'unknown'
                self.update_api_table()
                self.update_api_combo()
                self.save_config()
                dialog.close()
            
            save_btn.clicked.connect(save_config)
            cancel_btn.clicked.connect(dialog.close)
            btn_layout.addWidget(save_btn)
            btn_layout.addWidget(cancel_btn)
            layout.addLayout(btn_layout)
            
            dialog.setLayout(layout)
            dialog.exec()
    
    def delete_api_config(self):
        selected_rows = self.api_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "警告", "请先选择一个API配置")
            return
        
        row = selected_rows[0].row()
        if 0 <= row < len(self.api_configs):
            reply = QMessageBox.question(
                self, "确认删除",
                f"确定要删除API配置 '{self.api_configs[row]['name']}' 吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                del self.api_configs[row]
                if self.current_api_index >= len(self.api_configs):
                    self.current_api_index = max(0, len(self.api_configs) - 1)
                self.update_api_table()
                self.update_api_combo()
                self.update_api_status_label()
                self.save_config()
    
    def test_api_connection(self, config):
        try:
            resp = requests.get(config['url'].rstrip('/'), timeout=5)
            if resp.status_code == 200:
                config['status'] = 'success'
                return True, "连接成功"
            else:
                config['status'] = 'failed'
                return False, f"连接失败: HTTP {resp.status_code}"
        except Exception as e:
            config['status'] = 'failed'
            return False, f"连接失败: {str(e)}"
    
    def refresh_all_connections(self):
        self.refresh_btn.setEnabled(False)
        
        for config in self.api_configs:
            success, msg = self.test_api_connection(config)
        
        self.update_api_table()
        self.update_api_combo()
        self.update_api_status_label()
        self.save_config()
        
        self.refresh_btn.setEnabled(True)
        QMessageBox.information(self, "刷新完成", "所有API连接状态已刷新")
    
    def clear_cache(self):
        """清理保存路径的缓存文件"""
        cache_dir = './batch_tts_output'
        
        # 确认对话框
        reply = QMessageBox.question(
            self, "确认清理",
            f"确定要清理缓存目录 '{cache_dir}' 中的所有文件吗？此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            total_files = 0
            total_dirs = 0
            
            # 遍历缓存目录
            if os.path.exists(cache_dir):
                for root, dirs, files in os.walk(cache_dir, topdown=False):
                    # 删除文件
                    for file in files:
                        file_path = os.path.join(root, file)
                        os.remove(file_path)
                        total_files += 1
                    # 删除目录
                    for dir in dirs:
                        dir_path = os.path.join(root, dir)
                        os.rmdir(dir_path)
                        total_dirs += 1
                    # 删除根目录
                os.rmdir(cache_dir)
            
            QMessageBox.information(
                self, "清理完成",
                f"成功清理缓存：\n删除了 {total_files} 个文件\n删除了 {total_dirs + 1} 个目录"
            )
        except Exception as e:
            QMessageBox.warning(
                self, "清理失败",
                f"清理缓存时出错：{str(e)}"
            )
    
    def start_processing(self):
        if not self.lrc_files:
            QMessageBox.warning(self, "警告", "请先添加字幕文件")
            return
        
        if self.current_api_index >= len(self.api_configs):
            QMessageBox.warning(self, "警告", "请选择一个有效的API配置")
            return
        
        current_config = self.api_configs[self.current_api_index]
        
        config = {
            'api_url': current_config['url'],
            'model_name': current_config['model'],
            'prevent_sleep': self.prevent_sleep_check.isChecked(),
            'lrc_files': self.lrc_files.copy(),
            'output_dir': 'batch_tts_output',
            'api_configs': self.api_configs.copy(),
            'use_multi_api': self.multi_api_check.isChecked(),
            'current_api_index': self.current_api_index
        }
        
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setMaximum(0)
        self.log.clear()
        
        self.worker = TTSWorker(config)
        self.worker.log_signal.connect(self.log.append)
        self.worker.progress_signal.connect(self.progress.setValue)
        self.worker.total_tasks_signal.connect(self.progress.setMaximum)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()
    
    def on_finished(self, success):
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        if success:
            QMessageBox.information(self, "完成", "合成完成")
        else:
            QMessageBox.warning(self, "警告", "合成过程中出现错误")
    
    def pause_processing(self):
        if hasattr(self, 'worker') and self.worker.isRunning():
            if self.pause_btn.text() == "暂停":
                self.worker.pause()
                self.pause_btn.setText("恢复")
            else:
                self.worker.resume()
                self.pause_btn.setText("暂停")
    
    def dragEnterEvent(self, event):
        # 检查拖拽的数据是否包含文件
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dragMoveEvent(self, event):
        # 允许拖拽移动
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event):
        # 处理拖拽释放事件
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            new_files = []
            
            for url in urls:
                file_path = url.toLocalFile()
                if os.path.isfile(file_path):
                    # 处理单个文件
                    file_ext = os.path.splitext(file_path)[1].lower()
                    if file_ext in ['.lrc', '.vtt', '.srt']:
                        new_files.append(file_path)
                elif os.path.isdir(file_path):
                    # 处理文件夹，递归查找字幕文件
                    for root, dirs, files in os.walk(file_path):
                        for file in files:
                            file_ext = os.path.splitext(file)[1].lower()
                            if file_ext in ['.lrc', '.vtt', '.srt']:
                                new_files.append(os.path.join(root, file))
            
            # 添加新文件到列表
            added_count = 0
            for file in new_files:
                if file not in self.lrc_files:
                    self.lrc_files.append(file)
                    self.file_list.addItem(os.path.basename(file))
                    added_count += 1
            
            if added_count > 0:
                # 检查log组件是否已经初始化
                if hasattr(self, 'log'):
                    self.log.append(f"成功添加 {added_count} 个字幕文件")
    
    def closeEvent(self, event):
        # 窗口关闭时保存配置和停止工作线程
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)  # 等待2秒让线程退出
        self.save_config()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = TTSApp()
    window.show()
    sys.exit(app.exec())