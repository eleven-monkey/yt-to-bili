# -*- coding: utf-8 -*-
import os
import sys
import re
import json
import time
import shutil
import subprocess
import asyncio
import glob
import random
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from PIL import Image

import streamlit as st
import yt_dlp
import requests

def update_yt_dlp():
    """自动更新 yt-dlp 到最新版本"""
    try:
        print("正在检查并更新 yt-dlp...")
        # 使用 pip 更新 yt-dlp
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp'],
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            print("yt-dlp 更新成功")
            # 重新加载 yt_dlp 模块以获取更新后的版本
            import importlib
            importlib.reload(yt_dlp)
            print(f"当前 yt-dlp 版本: {yt_dlp.version.__version__}")
        else:
            print(f"yt-dlp 更新失败: {result.stderr}")
    except Exception as e:
        print(f"yt-dlp 更新过程出错: {e}")

# 启动时自动更新 yt-dlp（仅在进程首次启动时执行一次）
_yt_dlp_updated = globals().get('_yt_dlp_updated', False)
if not _yt_dlp_updated:
    update_yt_dlp()
    _yt_dlp_updated = True

def run_yt_dlp_subprocess(args, cookies_path=None):
    # Prefer calling yt-dlp directly to avoid python -m issues (like 'main.py error')
    cmd = [
        'yt-dlp',
        '--extractor-args', 'youtube:player_client=default,-web_safari',
        '--remote-components', 'ejs:github',
        '--no-playlist'
    ]
    if cookies_path:
        cmd.extend(['--cookies', cookies_path])
    
    cmd.extend(args)
    
    # Debug info
    print(f"Executing yt-dlp command: {' '.join(cmd)}")
    
    # Use shell=False for security, assuming args are clean
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
    
    if result.returncode != 0:
        # Fallback to python -m yt_dlp if binary is not found, but log it
        if "No such file or directory" in str(result.stderr) or result.returncode == 127:
             print("yt-dlp binary not found, falling back to python -m yt_dlp")
             import sys
             cmd[0] = sys.executable
             cmd.insert(1, '-m')
             cmd.insert(2, 'yt_dlp')
             print(f"Executing fallback command: {' '.join(cmd)}")
             result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')

        if result.returncode != 0:
            raise Exception(f"yt-dlp error: {result.stderr}")
    return result.stdout

import edge_tts
from bilibili_api import sync, video_uploader, Credential
from bilibili_api.video_uploader import VideoUploaderPage, VideoMeta
import pydub
import pickle
from worker_utils import process_segment, adjust_audio_speed

def load_env_config():
    """
    加载配置：优先使用系统环境变量(HuggingFace Secrets)，其次使用.env文件
    """
    config = {}
    
    # 1. 先加载 .env 文件 (如果有)
    env_file = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        config[key.strip()] = value.strip()
        except Exception as e:
            print(f"读取 .env 文件失败: {e}")

    # 2. 再加载系统环境变量 (覆盖 .env 中的同名配置)
    # 我们关心的特定环境变量列表
    target_keys = [
        "API_KEY", "API_URL", "MODEL_NAME", 
        "YT_COOKIES", 
        "BILI_SESSDATA", "BILI_BILI_JCT", "BILI_BUVID3", 
        "BILI_ACCESS_KEY_ID", "BILI_ACCESS_KEY_SECRET"
    ]
    
    for key in target_keys:
        env_val = os.getenv(key)
        if env_val:
            if key == "YT_COOKIES":
                env_val = env_val.replace("\\n", "\n")
            config[key] = env_val
            
    # 兼容旧的/拼写错误的变量名 BILI_SESSIDATA -> BILI_SESSDATA
    if "BILI_SESSDATA" not in config and os.getenv("BILI_SESSIDATA"):
        config["BILI_SESSDATA"] = os.getenv("BILI_SESSIDATA")
            
    return config

env_config = load_env_config()

# --- 状态管理与后台任务相关 ---
STATUS_FILE = "workflow_status.json"

class WorkflowManager:
    @staticmethod
    def get_status_file_path(temp_dir):
        return os.path.join(temp_dir, STATUS_FILE)

    @staticmethod
    def init_status(temp_dir):
        status = {
            "is_running": True,
            "stop_requested": False,
            "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "steps": {
                "下载字幕": {"status": "pending", "message": ""},
                "翻译标题": {"status": "pending", "message": ""},
                "翻译字幕": {"status": "pending", "message": ""},
                "转语音": {"status": "pending", "message": ""},
                "下载视频": {"status": "pending", "message": ""},
                "处理封面": {"status": "pending", "message": ""},
                "上传B站": {"status": "pending", "message": ""}
            },
            "results": {},
            "error": None,
            "logs": []
        }
        WorkflowManager.save_status(temp_dir, status)
        return status

    @staticmethod
    def request_stop(temp_dir):
        current_status = WorkflowManager.load_status(temp_dir)
        if current_status:
            current_status["stop_requested"] = True
            current_status["is_running"] = False  # 立即标记为停止
            current_status["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] 用户请求中止任务...")
            WorkflowManager.save_status(temp_dir, current_status)

    @staticmethod
    def load_status(temp_dir):
        file_path = WorkflowManager.get_status_file_path(temp_dir)
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    @staticmethod
    def save_status(temp_dir, status):
        file_path = WorkflowManager.get_status_file_path(temp_dir)
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存状态失败: {e}")

    @staticmethod
    def update_step(temp_dir, step_name, status_code, message=""):
        """
        status_code: pending, running, success, error
        """
        current_status = WorkflowManager.load_status(temp_dir)
        if current_status:
            if step_name in current_status["steps"]:
                current_status["steps"][step_name]["status"] = status_code
                current_status["steps"][step_name]["message"] = message
            current_status["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {step_name}: {status_code} - {message}")
            WorkflowManager.save_status(temp_dir, current_status)

    @staticmethod
    def mark_completed(temp_dir, results=None):
        current_status = WorkflowManager.load_status(temp_dir)
        if current_status:
            current_status["is_running"] = False
            if results:
                current_status["results"].update(results)
            WorkflowManager.save_status(temp_dir, current_status)

    @staticmethod
    def mark_error(temp_dir, error_msg):
        current_status = WorkflowManager.load_status(temp_dir)
        if current_status:
            current_status["is_running"] = False
            current_status["error"] = error_msg
            WorkflowManager.save_status(temp_dir, current_status)

def background_workflow_task(config):
    """
    后台运行的工作流主函数
    config: 包含所有必要参数的字典
    """
    temp_dir = config['temp_dir']
    workflow_url = config['workflow_url']
    auto_upload = config['auto_upload']
    
    # 初始化状态
    WorkflowManager.init_status(temp_dir)
    
    def check_interrupt():
        """检查是否有中断请求"""
        s = WorkflowManager.load_status(temp_dir)
        if s and s.get("stop_requested", False):
            raise Exception("用户手动中止任务")

    try:
        check_interrupt()
        subtitles_dir = os.path.join(temp_dir, "subtitles")
        os.makedirs(subtitles_dir, exist_ok=True)
        
        # --- 步骤1: 下载字幕 ---
        check_interrupt()
        WorkflowManager.update_step(temp_dir, "下载字幕", "running", "正在下载字幕...")
        
        cookies_file_path = None
        if config.get('yt_cookies', '').strip():
            cookies_file_path = os.path.join(temp_dir, "youtube_cookies.txt")
            with open(cookies_file_path, 'w', encoding='utf-8') as f:
                f.write(config['yt_cookies'].strip())
        
        # 重试机制
        def retry_op(func, max_retries=3):
            for attempt in range(max_retries):
                try:
                    check_interrupt()
                    return func()
                except Exception as e:
                    if str(e) == "用户手动中止任务": raise e
                    if attempt == max_retries - 1: raise e
                    # 重试前检查中断
                    check_interrupt()
                    time.sleep(2 ** attempt)

        def dl_sub():
            args = [
                '--write-auto-sub',
                '--skip-download',
                '--sub-langs', 'en',
                '--quiet',
                '-o', os.path.join(subtitles_dir, '%(title)s.%(ext)s'),
                workflow_url
            ]
            run_yt_dlp_subprocess(args, cookies_file_path)
            vtt_files = list(Path(subtitles_dir).glob("*.vtt"))
            if not vtt_files: raise Exception("未找到VTT文件")
            original_file = vtt_files[0]
            new_file = os.path.join(subtitles_dir, "word_level.vtt")
            # 如果存在则覆盖
            if os.path.exists(new_file): os.remove(new_file)
            os.rename(original_file, new_file)
            return new_file

        vtt_file_path = retry_op(dl_sub)
        WorkflowManager.update_step(temp_dir, "下载字幕", "success", f"已保存: {os.path.basename(vtt_file_path)}")
        
        # --- 步骤2: 翻译标题 ---
        check_interrupt()
        WorkflowManager.update_step(temp_dir, "翻译标题", "running", "正在分析视频信息...")
        
        def trans_title():
            args = ['--dump-json', '--skip-download', '--quiet', workflow_url]
            stdout = run_yt_dlp_subprocess(args, cookies_file_path)
            info_dict = json.loads(stdout)
            original_title = info_dict.get('title', '')
            
            if not original_title: raise Exception("无法获取标题")
            
            # 调用API翻译
            headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}
            
            # 标题翻译
            payload = {
                "model": config['model_name'],
                "messages": [
                    {"role": "system", "content": "你是爆款视频up主，将英文标题翻译成吸引眼球的爆款视频中文标题，直接输出翻译结果，不要解释。"},
                    {"role": "user", "content": original_title}
                ]
            }
            resp = requests.post(config['api_url'], json=payload, headers=headers, timeout=60)
            translated_title = resp.json()['choices'][0]['message']['content'].replace('**', '').strip()
            
            # 标签生成
            tags_payload = {
                "model": config['model_name'],
                "messages": [
                    {"role": "system", "content": "你是一个专业的B站运营助手"},
                    {"role": "user", "content": f"根据以下视频标题，生成5-8个B站视频标签（只输出标签，用逗号分隔）：\n标题：{translated_title}\n只输出标签，不要其他内容。"}
                ]
            }
            tags_resp = requests.post(config['api_url'], json=tags_payload, headers=headers, timeout=60)
            tags_str = tags_resp.json()['choices'][0]['message']['content']
            tags_list = [t.strip() for t in tags_str.replace('，', ',').split(',') if t.strip()][:10]
            
            # 保存上传配置
            upload_data = {'title_desc': f'(中配){translated_title}', 'tags': tags_list}
            with open(os.path.join(subtitles_dir, "upload_config.pkl"), 'wb') as f:
                pickle.dump(upload_data, f)
                
            return translated_title, tags_list
            
        translated_title, tags_list = retry_op(trans_title)
        WorkflowManager.update_step(temp_dir, "翻译标题", "success", f"标题: {translated_title}")
        
        # --- 步骤3: 翻译字幕 ---
        check_interrupt()
        WorkflowManager.update_step(temp_dir, "翻译字幕", "running", "AI正在翻译中(可能较慢)...")
        # 注意：这里调用全局函数，它会打印日志到stdout，但我们需要它正常运行
        # 我们可以暂时不捕获它的详细进度，或者修改原函数。为保持最小改动，直接调用。
        # 这里的全局变量 API_URL, API_KEY 等需要在调用前临时覆盖吗？
        # 全局函数 translate_subtitles_from_vtt 使用了全局变量 API_URL 等。
        # 在多线程环境下修改全局变量是危险的。
        # 但这里我们可以利用 python 的动态性，或者假设 config 中的值和全局的一致。
        # 实际上 app.py 里的 API_URL 是从 st.sidebar 获取的，在后台线程里无法访问 st.sidebar。
        # 必须确保全局变量被正确设置，或者重构 translate_subtitles_from_vtt 接受参数。
        # 为了安全，我们这里做一个简单的 trick：在 app.py 顶层，API_URL 等是全局变量。
        # 用户在 UI 修改后，这些全局变量并没有变（它们只是脚本顶层的初始值）。
        # st.sidebar 的值是在 st 运行时获取的。
        # 这是一个潜在 BUG：原代码中 translate_subtitles_from_vtt 直接用了 API_URL。
        # 在 Streamlit 中，每次 rerun 整个脚本从头跑，全局变量重置。
        # 当点击按钮时，API_URL 是当前局部变量（如果是通过 st.sidebar... 返回的）。
        # 原代码中：API_URL = st.sidebar.text_input(...)
        # 所以 API_URL 在脚本执行域中是存在的。
        # 但是，当线程运行时，如果主线程（Streamlit runner）结束或 rerun，这些模块级变量还在吗？
        # 在 Streamlit 中，模块级别的变量是跨 session 共享的（如果不是在函数内定义）。
        # 但 API_URL = st.sidebar... 是在脚本执行流中定义的。
        # 为了确保后台线程能拿到正确的配置，我们需要修改 translate_subtitles_from_vtt 
        # 或者临时设置全局变量（不推荐）。
        # 最稳妥的方法：修改 translate_subtitles_from_vtt 签名接受 api_key 等参数。
        # 但用户要求 "不要着急编码" 且 "mimic style"，我选择一种侵入性小的方法：
        # 将配置注入到全局（虽然有点脏，但在单实例容器中可行），或者最好稍微修改一下 translate_subtitles_from_vtt。
        # 让我们看看 translate_subtitles_from_vtt 定义。它确实使用了全局 API_URL。
        # 我们可以使用 unittest.mock.patch 或者简单的 global 赋值来确保线程内看到的变量是对的。
        # 但由于这是多线程，修改全局变量会影响其他用户（虽然 streamlit 通常单实例）。
        # 更好的方案：传递参数。
        # 我将修改 translate_subtitles_from_vtt 及其调用的函数，但这改动大。
        # 让我们用 global 变量注入的方式（在线程开始前，或者假设用户没有变）。
        # 实际上，我们可以重写 translate_subtitles_from_vtt 的部分逻辑在线程里，或者，
        # 鉴于 `translate_subtitles_from_vtt` 就在 `app.py` 里，
        # 我可以简单地将这些配置作为参数传递给 `translate_subtitles_from_vtt`，给它加默认参数=None，如果None则取全局。
        # 这样改动最小。
        
        # 稍后我会微调 translate_subtitles_from_vtt。现在先假设它能工作（如果它引用的全局变量被正确闭包捕获）。
        # 实际上 python 的闭包是迟绑定的。
        # 为了稳妥，我在 background_workflow_task 里定义一个 wrapper 或者 monkeypatch。
        # 让我们尝试一种 Pythonic 的方法：动态修改全局变量上下文？不，太黑魔法。
        # 我将修改 `translate_subtitles_from_vtt` 接受可选的 api_config 参数。
        
        txt_file_path = translate_subtitles_from_vtt(vtt_file_path, api_config={
            "API_URL": config['api_url'],
            "API_KEY": config['api_key'],
            "MODEL_NAME": config['model_name'],
            "MAX_WORKERS": config['max_workers'],
            "SEGMENT_SIZE": config['segment_size']
        })
        
        WorkflowManager.update_step(temp_dir, "翻译字幕", "success", f"已保存: {os.path.basename(txt_file_path)}")
        
        # --- 步骤4: 转语音 ---
        check_interrupt()
        WorkflowManager.update_step(temp_dir, "转语音", "running", "正在进行TTS转换...")
        
        output_mp3 = os.path.join(subtitles_dir, os.path.splitext(os.path.basename(vtt_file_path))[0] + "_translated.mp3")
        # 同理，process_tts_with_speed_adjustment 也需要 config
        mp3_file_path = process_tts_with_speed_adjustment(txt_file_path, output_mp3, subtitles_dir, tts_config={
            "TEMP_DIR": temp_dir,
            "SELECTED_VOICE": config['voice_choice']
        })
        
        if not mp3_file_path: raise Exception("TTS转换失败")
        WorkflowManager.update_step(temp_dir, "转语音", "success", f"已生成: {os.path.basename(mp3_file_path)}")
        
        # --- 步骤5: 下载视频 ---
        check_interrupt()
        WorkflowManager.update_step(temp_dir, "下载视频", "running", "下载并合并视频...")
        
        def dl_video():
            dl_base = os.path.join(temp_dir, "subtitles", "downloaded_video")
            args = [
                '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                '-o', f'{dl_base}.%(ext)s',
                '--quiet',
                workflow_url
            ]
            run_yt_dlp_subprocess(args, cookies_file_path)
            
            dl_files = glob.glob(f"{dl_base}.*")
            if not dl_files: raise Exception("视频文件未找到")
            
            raw_video = dl_files[0]
            final_vid = os.path.splitext(mp3_file_path)[0] + ".mp4"
            
            subprocess.run(['ffmpeg', '-y', '-i', raw_video, '-i', mp3_file_path,
                           '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v:0', '-map', '1:a:0',
                           final_vid], check=True, capture_output=True)
            
            if os.path.exists(raw_video): os.remove(raw_video)
            return final_vid
            
        final_video_path = retry_op(dl_video)
        WorkflowManager.update_step(temp_dir, "下载视频", "success", f"最终视频: {os.path.basename(final_video_path)}")
        
        # --- 步骤6: 处理封面 ---
        check_interrupt()
        WorkflowManager.update_step(temp_dir, "处理封面", "running", "优化封面图片...")
        
        def proc_cover():
            args = [
                '--skip-download',
                '--write-thumbnail',
                '--quiet',
                '-o', os.path.join(temp_dir, "subtitles", 'cover.%(ext)s'),
                workflow_url
            ]
            run_yt_dlp_subprocess(args, cookies_file_path)
            
            # 寻找封面文件
            cover_candidates = list(Path(os.path.join(temp_dir, "subtitles")).glob("cover.*"))
            # 排除已经是jpeg的防止重复处理
            src_cover = None
            for c in cover_candidates:
                if c.suffix.lower() in ['.webp', '.jpg', '.png']:
                    src_cover = c
                    break
            
            if not src_cover: raise Exception("未找到封面文件")
            
            out_cover = os.path.join(temp_dir, "subtitles", "cover.jpeg")
            qual = 90
            with Image.open(src_cover) as img:
                if img.mode != 'RGB': img = img.convert('RGB')
                img.save(out_cover, 'jpeg', quality=qual)
                
                # 压缩到合适大小
                while os.path.getsize(out_cover) / 1024 > 50 and qual > 10:
                    qual -= 5
                    img.save(out_cover, 'jpeg', quality=qual)
            return out_cover

        cover_path = retry_op(proc_cover)
        WorkflowManager.update_step(temp_dir, "处理封面", "success", "封面处理完成")
        
        results = {
            "vtt": vtt_file_path,
            "txt": txt_file_path,
            "mp3": mp3_file_path,
            "video": final_video_path,
            "cover": cover_path
        }
        
        # --- 步骤7: 上传B站 ---
        if auto_upload:
            check_interrupt()
            WorkflowManager.update_step(temp_dir, "上传B站", "running", "正在上传到B站...")
            
            print("=" * 50, flush=True)
            print("开始B站上传流程", flush=True)
            print("=" * 50, flush=True)
            
            credential = Credential(sessdata=config['bili_sess'], bili_jct="bcd4ba0d9ab8a7b95485798ed8097d26")
            vu_meta = VideoMeta(
                tid=130, title=translated_title, tags=tags_list,
                desc=translated_title, cover=cover_path, no_reprint=True
            )
            
            upload_completed = False
            upload_error = None
            upload_result = None
            
            async def upload_task():
                nonlocal upload_completed, upload_error, upload_result
                try:
                    page = VideoUploaderPage(path=final_video_path, title=translated_title, description=translated_title)
                    uploader = video_uploader.VideoUploader([page], vu_meta, credential, line=video_uploader.Lines.QN)
                    
                    # 进度状态
                    total_chunks = 0
                    uploaded_chunks = 0
                    last_percent = 0
                    
                    @uploader.on("__ALL__")
                    async def on_all_event(event_data):
                        nonlocal total_chunks, uploaded_chunks, last_percent
                        
                        # 检查中断
                        check_interrupt()
                        
                        # 处理 tuple 类型的 event_data
                        if isinstance(event_data, tuple):
                            if len(event_data) > 0:
                                event_data = event_data[0]
                            else:
                                event_data = {}
                                
                        # 打印事件名称和关键数据
                        event_name = event_data.get("name", "UNKNOWN")
                        data = event_data.get("data", {})
                        
                        if event_name == "PREUPLOAD":
                            print(f"[上传] 事件 PREUPLOAD - 获取上传信息中...", flush=True)
                        elif event_name == "PREUPLOAD_FAILED":
                            print(f"[上传] 事件 PREUPLOAD_FAILED - 获取上传信息失败: {data}", flush=True)
                        elif event_name == "PRE_CHUNK":
                            total_chunks = data.get("total_chunk_count", 1)
                            chunk_num = data.get("chunk_number", 1)
                            print(f"[上传] 事件 PRE_CHUNK - 开始上传分块 {chunk_num}/{total_chunks}", flush=True)
                        elif event_name == "AFTER_CHUNK":
                            uploaded_chunks += 1
                            chunk_num = data.get("chunk_number", 0)
                            percent = int((uploaded_chunks / total_chunks) * 100) if total_chunks > 0 else 0
                            if percent - last_percent >= 5:
                                print(f"[上传] 事件 AFTER_CHUNK - 分块 {chunk_num} 上传完成, 进度 {percent}%", flush=True)
                                WorkflowManager.update_step(temp_dir, "上传B站", "running", f"上传中... {percent}%")
                                last_percent = percent
                        elif event_name == "CHUNK_FAILED":
                            print(f"[上传] 事件 CHUNK_FAILED - 分块上传失败: {data}", flush=True)
                        elif event_name == "PRE_PAGE_SUBMIT":
                            print(f"[上传] 事件 PRE_PAGE_SUBMIT - 准备提交分P", flush=True)
                        elif event_name == "AFTER_PAGE_SUBMIT":
                            print(f"[上传] 事件 AFTER_PAGE_SUBMIT - 分P提交完成", flush=True)
                        elif event_name == "PAGE_SUBMIT_FAILED":
                            print(f"[上传] 事件 PAGE_SUBMIT_FAILED - 分P提交失败: {data}", flush=True)
                        elif event_name == "PRE_COVER":
                            print(f"[上传] 事件 PRE_COVER - 准备上传封面", flush=True)
                        elif event_name == "AFTER_COVER":
                            cover_url = data.get("url", "")
                            print(f"[上传] 事件 AFTER_COVER - 封面上传完成: {cover_url}", flush=True)
                        elif event_name == "COVER_FAILED":
                            print(f"[上传] 事件 COVER_FAILED - 封面上传失败: {data}", flush=True)
                        elif event_name == "PRE_SUBMIT":
                            print(f"[上传] 事件 PRE_SUBMIT - 准备最终提交", flush=True)
                        elif event_name == "SUBMIT_FAILED":
                            print(f"[上传] 事件 SUBMIT_FAILED - 最终提交失败: {data}", flush=True)
                        elif event_name == "AFTER_SUBMIT":
                            print(f"[上传] 事件 AFTER_SUBMIT - 最终提交完成", flush=True)
                            upload_result = data
                        elif event_name == "COMPLETED":
                            print(f"[上传] 事件 COMPLETED - 上传全部完成", flush=True)
                        elif event_name == "ABORTED":
                            print(f"[上传] 事件 ABORTED - 上传被中止", flush=True)
                        elif event_name == "FAILED":
                            print(f"[上传] 事件 FAILED - 上传失败: {data}", flush=True)
                        else:
                            print(f"[上传] 事件 {event_name}: {data}", flush=True)
                    
                    print(f"[上传] 开始调用 uploader.start()...", flush=True)
                    
                    # 设置超时（10分钟）
                    await asyncio.wait_for(uploader.start(), timeout=600)
                    upload_completed = True
                    print(f"[上传] uploader.start() 返回，上传完成", flush=True)
                    
                except asyncio.TimeoutError:
                    upload_error = "上传超时（10分钟），可能是网络问题或B站服务器繁忙"
                    print(f"[上传] 超时错误: {upload_error}", flush=True)
                except asyncio.CancelledError:
                    upload_error = "上传被用户取消"
                    print(f"[上传] 取消错误: {upload_error}", flush=True)
                except Exception as e:
                    upload_error = str(e)
                    print(f"[上传] 异常错误: {upload_error}", flush=True)
                    import traceback
                    traceback.print_exc()
            
            # 在新事件循环运行
            print(f"[上传] 创建新的事件循环", flush=True)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(upload_task())
                print(f"[上传] 事件循环执行完毕", flush=True)
            except Exception as e:
                print(f"[上传] 事件循环异常: {e}", flush=True)
                import traceback
                traceback.print_exc()
            finally:
                loop.close()
                print(f"[上传] 事件循环已关闭", flush=True)
            
            # 检查上传结果
            if upload_error:
                WorkflowManager.update_step(temp_dir, "上传B站", "error", upload_error)
                raise Exception(upload_error)
            elif not upload_completed:
                WorkflowManager.update_step(temp_dir, "上传B站", "error", "上传意外中断（未完成）")
                raise Exception("上传意外中断（未完成）")
            
            if upload_result:
                bvid = upload_result.get("bvid", "")
                aid = upload_result.get("aid", "")
                print(f"[上传] 上传成功! bvid={bvid}, aid={aid}", flush=True)
            
            WorkflowManager.update_step(temp_dir, "上传B站", "success", "上传成功！")
            print(f"[上传] B站上传步骤完成", flush=True)
        else:
            WorkflowManager.update_step(temp_dir, "上传B站", "success", "跳过上传")

        WorkflowManager.mark_completed(temp_dir, results)

    except Exception as e:
        import traceback
        err_msg = f"{str(e)}"
        if str(e) != "用户手动中止任务":
            err_msg += f"\n{traceback.format_exc()}"
            print(f"后台任务出错: {err_msg}")
        WorkflowManager.mark_error(temp_dir, str(e))

# --- 批量工作流相关 ---
BATCH_STATUS_FILE = "batch_status.json"

class BatchWorkflowManager:
    @staticmethod
    def get_batch_dir(base_dir):
        return os.path.join(base_dir, "batch_workspace")

    @staticmethod
    def get_status_file_path(base_dir):
        batch_dir = BatchWorkflowManager.get_batch_dir(base_dir)
        return os.path.join(batch_dir, BATCH_STATUS_FILE)

    @staticmethod
    def init_status(base_dir, urls):
        batch_dir = BatchWorkflowManager.get_batch_dir(base_dir)
        os.makedirs(batch_dir, exist_ok=True)
        video_results = []
        for i, url in enumerate(urls):
            video_results.append({
                "index": i,
                "url": url,
                "status": "pending",
                "message": "等待中"
            })
        status = {
            "is_running": True,
            "stop_requested": False,
            "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "start_timestamp": datetime.now().isoformat(),
            "total_videos": len(urls),
            "current_index": -1,
            "video_results": video_results,
            "error": None,
            "elapsed_time": "",
            "logs": []
        }
        BatchWorkflowManager.save_status(base_dir, status)
        return status

    @staticmethod
    def request_stop(base_dir):
        current = BatchWorkflowManager.load_status(base_dir)
        if current:
            current["stop_requested"] = True
            current["is_running"] = False
            current["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] 用户请求中止批量任务...")
            BatchWorkflowManager.save_status(base_dir, current)

    @staticmethod
    def load_status(base_dir):
        file_path = BatchWorkflowManager.get_status_file_path(base_dir)
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    @staticmethod
    def save_status(base_dir, status):
        file_path = BatchWorkflowManager.get_status_file_path(base_dir)
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存批量状态失败: {e}")

    @staticmethod
    def update_video(base_dir, index, status_code, message=""):
        current = BatchWorkflowManager.load_status(base_dir)
        if current and 0 <= index < len(current["video_results"]):
            current["video_results"][index]["status"] = status_code
            current["video_results"][index]["message"] = message
            current["current_index"] = index
            current["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] 视频 {index+1}: {status_code} - {message}")
            BatchWorkflowManager.save_status(base_dir, current)

    @staticmethod
    def _calc_elapsed(current):
        """根据 start_timestamp 计算已用时间的可读字符串"""
        try:
            start = datetime.fromisoformat(current.get("start_timestamp", ""))
            delta = datetime.now() - start
            total_secs = int(delta.total_seconds())
            hours, remainder = divmod(total_secs, 3600)
            mins, secs = divmod(remainder, 60)
            if hours > 0:
                return f"{hours}小时{mins}分{secs}秒"
            elif mins > 0:
                return f"{mins}分{secs}秒"
            else:
                return f"{secs}秒"
        except Exception:
            return ""

    @staticmethod
    def mark_completed(base_dir):
        current = BatchWorkflowManager.load_status(base_dir)
        if current:
            current["is_running"] = False
            elapsed = BatchWorkflowManager._calc_elapsed(current)
            current["elapsed_time"] = elapsed
            current["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] 批量任务全部完成，总用时: {elapsed}")
            BatchWorkflowManager.save_status(base_dir, current)

    @staticmethod
    def mark_error(base_dir, error_msg):
        current = BatchWorkflowManager.load_status(base_dir)
        if current:
            current["is_running"] = False
            current["error"] = error_msg
            current["elapsed_time"] = BatchWorkflowManager._calc_elapsed(current)
            BatchWorkflowManager.save_status(base_dir, current)


def background_batch_workflow_task(batch_config):
    """
    后台运行的批量工作流主函数
    batch_config: 包含 urls 列表和所有必要参数的字典
    """
    base_dir = batch_config['base_dir']
    items = batch_config['items']  # [{"url": ..., "voice": ...}, ...]
    auto_upload = batch_config['auto_upload']
    default_voice = batch_config.get('voice_choice', 'zh-CN-YunjianNeural')

    # 提取所有 URL 用于初始化状态
    urls = [item['url'] for item in items]
    BatchWorkflowManager.init_status(base_dir, urls)
    batch_dir = BatchWorkflowManager.get_batch_dir(base_dir)

    try:
        for i, item in enumerate(items):
            url = item['url']
            voice = item.get('voice', default_voice)

            # 检查中止
            bs = BatchWorkflowManager.load_status(base_dir)
            if bs and bs.get("stop_requested", False):
                BatchWorkflowManager.update_video(base_dir, i, "error", "用户中止")
                break

            BatchWorkflowManager.update_video(base_dir, i, "running", f"正在处理... (配音: {voice})")

            # 为每个视频创建独立临时目录
            video_temp_dir = os.path.join(batch_dir, f"video_{i}")
            os.makedirs(video_temp_dir, exist_ok=True)

            # 构建单视频配置（复用 background_workflow_task）
            single_config = {
                "temp_dir": video_temp_dir,
                "workflow_url": url.strip(),
                "auto_upload": auto_upload,
                "api_url": batch_config['api_url'],
                "api_key": batch_config['api_key'],
                "model_name": batch_config['model_name'],
                "bili_sess": batch_config['bili_sess'],
                "bili_ak": batch_config.get('bili_ak', ''),
                "bili_sk": batch_config.get('bili_sk', ''),
                "yt_cookies": batch_config.get('yt_cookies', ''),
                "voice_choice": voice,
                "max_workers": batch_config['max_workers'],
                "segment_size": batch_config['segment_size']
            }

            try:
                background_workflow_task(single_config)
                # 检查单视频的执行结果
                single_status = WorkflowManager.load_status(video_temp_dir)
                if single_status and single_status.get("error"):
                    BatchWorkflowManager.update_video(base_dir, i, "error", single_status["error"])
                else:
                    BatchWorkflowManager.update_video(base_dir, i, "success", "处理完成")
            except Exception as e:
                BatchWorkflowManager.update_video(base_dir, i, "error", str(e))
                print(f"批量任务 - 视频 {i+1} 出错: {e}")
                # 继续处理下一个视频，不中断整个批量任务
                continue

        BatchWorkflowManager.mark_completed(base_dir)

    except Exception as e:
        import traceback
        print(f"批量任务出错: {e}\n{traceback.format_exc()}")
        BatchWorkflowManager.mark_error(base_dir, str(e))

def clear_temp_directory():
    """清空temp目录下的所有内容"""
    import shutil
    try:
        if os.path.exists(TEMP_DIR):
            for filename in os.listdir(TEMP_DIR):
                file_path = os.path.join(TEMP_DIR, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    print(f'清空temp目录时出错 {file_path}: {e}')
            print("temp目录已清空")
        else:
            os.makedirs(TEMP_DIR, exist_ok=True)
            print("temp目录已创建")
    except Exception as e:
        print(f"清空temp目录失败: {e}")

# 翻译字幕相关函数
def translate_subtitles_from_vtt(vtt_file_path, api_config=None):
    """从VTT文件翻译字幕，生成带时间戳的文本文件（单步执行的完整逻辑）"""
    # 获取配置，如果未提供则使用全局变量
    cfg_api_url = api_config.get("API_URL", API_URL) if api_config else API_URL
    cfg_api_key = api_config.get("API_KEY", API_KEY) if api_config else API_KEY
    cfg_model = api_config.get("MODEL_NAME", MODEL_NAME) if api_config else MODEL_NAME
    cfg_max_workers = api_config.get("MAX_WORKERS", MAX_WORKERS) if api_config else MAX_WORKERS
    cfg_seg_size = api_config.get("SEGMENT_SIZE", SEGMENT_SIZE) if api_config else SEGMENT_SIZE

    def vtt_to_sentences(vtt_text):
        """将带逐词时间戳的VTT转换为按句分段的文本"""
        # 正则：cue 头（起止时间）
        CUE_HEADER_RE = re.compile(
            r'^(\d{2}:\d{2}:\d{2}\.\d{3})\s*--> (\d{2}:\d{2}:\d{2}\.\d{3})'
        )

        # 正则：逐词时间戳 <HH:MM:SS.mmm>
        TS_TAG_RE = re.compile(r'<(\d{2}:\d{2}:\d{2}\.\d{3})>')

        # 正则：清理 <c> 或 <c.xxx> 样式标签
        C_TAG_RE = re.compile(r'</?c(?:\.[^>]*)?>', re.IGNORECASE)

        SENTENCE_END = ".!?"

        lines = vtt_text.splitlines()
        sentences = []
        current_words = []
        current_sentence_start_time = None

        effective_time = None
        cue_start_time = None

        def flush_sentence():
            nonlocal current_words, current_sentence_start_time
            if not current_words:
                return
            text = " ".join(current_words)
            text = re.sub(r"\s+([,.;!?])", r"\1", text)
            text = re.sub(r"\(\s+", "(", text)
            text = re.sub(r"\s+\)", ")", text)
            start_ts = current_sentence_start_time or cue_start_time or effective_time or "00:00:00.000"
            sentences.append(f"({start_ts}) {text}")
            current_words = []
            current_sentence_start_time = None

        for line in lines:
            line = line.strip("\ufeff\r\n")

            # cue 头
            m = CUE_HEADER_RE.match(line)
            if m:
                cue_start_time = m.group(1)
                effective_time = cue_start_time
                continue

            # 只处理含逐词时间戳的行
            if not TS_TAG_RE.search(line):
                continue

            # 清理 <c> 标签，并把 <timestamp> 变成 [[TS:...]] 哨兵
            s = C_TAG_RE.sub("", line)
            s = TS_TAG_RE.sub(lambda mm: f" [[TS:{mm.group(1)}]] ", s)

            # 扫描 token
            for token in s.split():
                if token.startswith("[[TS:") and token.endswith("]]"):
                    effective_time = token[5:-2]
                    continue

                word = token.strip()
                if not word:
                    continue

                # 记录首词时间
                if current_sentence_start_time is None:
                    current_sentence_start_time = effective_time or cue_start_time

                current_words.append(word)

                # 句子结束判定（句号、问号、叹号）
                if word.strip().endswith(tuple(SENTENCE_END)):
                    flush_sentence()

        # 文件结束，收尾
        flush_sentence()
        return sentences

    vtt_content = Path(vtt_file_path).read_text(encoding="utf-8", errors="ignore")
    sentences = vtt_to_sentences(vtt_content)

    print(f"调试信息：解析出 {len(sentences)} 个句子")
    if sentences:
        print(f"前3个句子示例：")
        for i, s in enumerate(sentences[:3]):
            print(f"  {i+1}: {s[:100]}...")

    output_txt_file = os.path.splitext(vtt_file_path)[0] + ".txt"
    with open(output_txt_file, 'w', encoding='utf-8') as f:
        for seg in sentences:
            f.write(seg + "\n\n")

    paragraphs = [line.strip() for line in open(output_txt_file, 'r', encoding='utf-8') if line.strip()]

    print(f"调试信息：读取到 {len(paragraphs)} 个段落")

    batched_paragraphs = []
    current_batch = []
    current_char_count = 0

    for i, paragraph in enumerate(paragraphs):
        paragraph_char_count = len(paragraph)
        if (len(current_batch) >= cfg_seg_size) or (current_char_count + paragraph_char_count > 2000 and current_batch):
            batched_paragraphs.append("\n".join(current_batch))
            print(f"调试信息：分段 {len(batched_paragraphs)} 包含 {len(current_batch)} 个段落，共 {current_char_count} 字符")
            current_batch = [paragraph]
            current_char_count = paragraph_char_count
        else:
            current_batch.append(paragraph)
            current_char_count += paragraph_char_count

    if current_batch:
        batched_paragraphs.append("\n".join(current_batch))
        print(f"调试信息：最后一个分段 {len(batched_paragraphs)} 包含 {len(current_batch)} 个段落，共 {current_char_count} 字符")

    print(f"调试信息：总共 {len(batched_paragraphs)} 个翻译分段")

    def translate_batch(batch, batch_index):
        try:
            print(f"调试信息：开始翻译分段 {batch_index}，内容长度: {len(batch)} 字符")
            print(f"分段内容预览: {batch[:200]}...")

            url = cfg_api_url
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg_api_key}"
            }
            payload = {
                "model": cfg_model,
                "messages": [
                    {"role": "system", "content": "# Role: 专业翻译官\n\n## Profile\n- author: LangGPT优化中心\n- version: 2.1\n- language: 中英双语\n- description: 专注于文本精准转换的AI翻译专家，擅长处理技术文档和日常对话场景\n\n## Background\n用户在跨国协作、技术文档处理、社交媒体互动等场景中，需要将外文内容准确转化为中文，同时保持特殊格式元素完整\n\n## Skills\n1. 多语言文本解析与重构能力\n2. 时间戳识别与格式保留技术\n3. 语义通顺度校验算法\n4. 格式控制与冗余内容过滤\n\n## Goals\n1. 实现原文语义的精准转换\n2. 保持时间戳等特殊格式元素\n3. 确保输出结果自然流畅\n4. 排除非翻译内容添加\n\n## Constraints\n1. 禁止添加解释性文字\n2. 禁用注释或说明性符号\n3. 保留原始时间戳格式（如(12:34））\n4. 不处理非文本元素（如图片/表格）\n5. 禁止使用工具调用（tool_calls）功能，禁止调用外部翻译api进行翻译\n\n## Workflow\n1. 接收输入内容，检测语言类型\n2. 识别并标记特殊格式元素\n3. 执行语义转换：\n   - 日常用语：采用口语化表达\n   - 技术术语：使用标准化译法\n5. 输出纯翻译结果\n\n## OutputFormat\n仅返回符合以下要求的翻译文本：\n1. 中文书面语表达\n2. 保留原始段落结构\n3. 时间戳保持(MM:SS)或(HH:MM:SS)格式\n4. 无任何附加符号或说明\n4. 尽量只要中文，不要中英文夹杂。"},
                    {"role": "user", "content": batch}
                ],
                "stream": False,
                "max_tokens": 4000
            }
            print(f"调试信息：分段 {batch_index} 发送API请求到 {url}")
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            print(f"调试信息：分段 {batch_index} API响应状态码: {response.status_code}")
            response.raise_for_status()
            result = response.json()
            translated_content = result['choices'][0]['message']['content']

            # 清理不需要朗读的字符 / Clean characters not to be read
            translated_content = translated_content.replace('&gt;', '').replace('>>', '').replace('& trash;', '').replace('[音乐]', '').replace('[笑声]', '')

            print(f"调试信息：分段 {batch_index} 翻译完成，返回内容长度: {len(translated_content)} 字符")
            print(f"翻译内容预览: {translated_content[:200]}...")
            return translated_content
        except Exception as e:
            print(f"调试信息：分段 {batch_index} 错误详情: {traceback.format_exc()}")
            return f"Error: {str(e)}"

    translated_results = {}
    with ThreadPoolExecutor(max_workers=cfg_max_workers) as executor:
        futures = {executor.submit(translate_batch, batch, i): i for i, batch in enumerate(batched_paragraphs)}
        for future in as_completed(futures):
            index = futures[future]
            result = future.result()
            if not result.startswith("Error:"):
                translated_results[index] = result

    translated_paragraphs = []
    failed_count = 0
    for i in range(len(batched_paragraphs)):
        if i in translated_results:
            translated_paragraphs.append(translated_results[i])
        else:
            failed_count += 1
            translated_paragraphs.append(f"翻译失败的分段 {i+1}")

    if failed_count > 0:
        print(f"警告：{failed_count} 个分段翻译失败")

    final_output_file = os.path.splitext(vtt_file_path)[0] + "_translated.txt"
    with open(final_output_file, 'w', encoding='utf-8') as f:
        for para in translated_paragraphs:
            f.write(para + "\n\n")

    print(f"翻译完成，保存到: {final_output_file}")
    return final_output_file

# TTS 相关函数已移至 worker_utils.py

def process_tts_with_speed_adjustment(txt_file_path, output_mp3_path, subtitles_dir, tts_config=None):
    """处理TTS转换并进行音频速度调整 (调用外部脚本以避免主进程卡顿)"""
    cfg_temp_dir = tts_config.get("TEMP_DIR", TEMP_DIR) if tts_config else TEMP_DIR
    cfg_voice = tts_config.get("SELECTED_VOICE", SELECTED_VOICE) if tts_config else SELECTED_VOICE

    print("="*50, flush=True)
    print("开始TTS转换流程 (Subprocess Mode)", flush=True)
    print("="*50, flush=True)

    tts_runner_script = os.path.join(os.getcwd(), "tts_runner.py")
    
    cmd = [
        sys.executable,
        tts_runner_script,
        "--input", txt_file_path,
        "--output", output_mp3_path,
        "--voice", cfg_voice,
        "--workers", "4",
        "--temp", cfg_temp_dir
    ]
    
    print(f"Executing TTS command: {' '.join(cmd)}", flush=True)
    
    try:
        # 使用 subprocess.Popen 实时捕获输出
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )
        
        # 实时打印子进程输出
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                print(f"[TTS-Process] {line.strip()}", flush=True)
                
        returncode = process.poll()
        
        if returncode == 0 and os.path.exists(output_mp3_path):
            print(f"TTS子进程执行成功，输出文件: {output_mp3_path}", flush=True)
            return output_mp3_path
        else:
            print(f"TTS子进程执行失败，返回码: {returncode}", flush=True)
            return None
            
    except Exception as e:
        print(f"执行TTS子进程时出错: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None

# parse_timestamp 已移至 worker_utils.py

st.set_page_config(
    page_title="YouTube转B站搬运工具",
    page_icon="🎥",
    layout="wide"
)

st.title("YouTube转B站搬运一条龙")
st.markdown("---")

st.sidebar.header("⚙️ 配置")

API_URL = st.sidebar.text_input("API URL", value=env_config.get("API_URL", "https://api.siliconflow.cn/v1/chat/completions"), help="翻译API的URL", key="api_url")
API_KEY = st.sidebar.text_input("API Key", type="password", value=env_config.get("API_KEY", ""), help="翻译API的Key（将在运行时从环境变量读取）", key="api_key")
MODEL_NAME = st.sidebar.text_input("模型名称", value=env_config.get("MODEL_NAME", "THUDM/GLM-4-9B-0414"), help="翻译使用的模型名称", key="model_name")

BILI_SESSDATA = st.sidebar.text_area("B站Cookie", value=env_config.get("BILI_SESSDATA", ""), help="B站的sessdata（用于上传）", height=100, key="bili_sessdata")
BILI_ACCESS_KEY_ID = st.sidebar.text_input("B站Access Key ID", value=env_config.get("BILI_ACCESS_KEY_ID", ""), help="B站的access_key_id", key="bili_access_key_id")
BILI_ACCESS_KEY_SECRET = st.sidebar.text_input("B站Access Key Secret", type="password", value=env_config.get("BILI_ACCESS_KEY_SECRET", ""), help="B站的access_key_secret", key="bili_access_key_secret")

YT_COOKIES = st.sidebar.text_area("YouTube Cookies (可选)", value=env_config.get("YT_COOKIES", ""), help="YouTube cookies（用于访问需要登录的视频）", height=100, key="yt_cookies")

VOICE_CHOICES = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunjianNeural", "zh-CN-YunxiNeural"]
# 中文别名映射（用于批量工作流的配音标签）
VOICE_ALIAS_MAP = {
    "女声-晓晓": "zh-CN-XiaoxiaoNeural",
    "男声-云健": "zh-CN-YunjianNeural",
    "男声-云希": "zh-CN-YunxiNeural",
}
VOICE_ALIAS_REVERSE = {v: k for k, v in VOICE_ALIAS_MAP.items()}
SELECTED_VOICE = st.sidebar.selectbox("TTS语音角色", options=VOICE_CHOICES, index=1, key="selected_voice")

MAX_WORKERS = st.sidebar.slider("翻译并发数", min_value=1, max_value=20, value=10, help="同时翻译的段落数量")
SEGMENT_SIZE = st.sidebar.slider("翻译分段大小", min_value=1, max_value=20, value=11, help="每次翻译包含的段落数量")

st.markdown("---")

TEMP_DIR = os.path.join(os.getcwd(), "temp_storage")
if not os.path.exists(TEMP_DIR):
    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
    except Exception as e:
        # 如果当前目录不可写，再退回到系统临时目录
        TEMP_DIR = os.path.join(tempfile.gettempdir(), "yt_video_trans_temp")
        os.makedirs(TEMP_DIR, exist_ok=True)

temp_dir = None

tab0, tab8, tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "0️🚀 一键工作流",
        "📦 批量工作流",
        "1️⬇️ 下载字幕", 
        "2️⚙️ 翻译字幕", 
        "3️🗣️ 转语音", 
        "4️🎬️ 下载视频", 
        "5️🖼️ 处理封面", 
        "6️✂️ 视频剪辑", 
        "7️📤️ 上传B站"
    ])

with tab0:
    st.markdown("""
    <style>
    .workflow-container {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 2rem;
        border-radius: 10px;
        color: white;
    }
    .step-card {
        background: rgba(255,255,255,0.1);
        border: 1px solid rgba(255,255,255,0.2);
        border-radius: 8px;
        padding: 1rem;
        margin: 0.5rem 0;
    }
    .step-success {
        background: rgba(40,167,69,0.3);
        border-color: #28a745;
    }
    .step-error {
        background: rgba(220,53,69,0.3);
        border-color: #dc3545;
    }
    .step-running {
        background: rgba(255,193,7,0.3);
        border-color: #ffc107;
    }
    </style>
    <div class="workflow-container">
        <h1 style="text-align:center; margin-bottom:1rem;">🚀 一键工作流 (后台版)</h1>
        <p style="text-align:center; opacity:0.9;">任务在后台运行，您可以放心刷新或关闭页面</p>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("---")
    
    # 检查当前状态
    current_status = WorkflowManager.load_status(TEMP_DIR)
    is_running = current_status and current_status.get("is_running", False)
    
    if is_running:
        st.info(f"🔄 任务正在后台运行中... (开始时间: {current_status.get('start_time')})")

        if st.button("🛑 中止任务", type="secondary", key="stop_workflow_btn"):
             WorkflowManager.request_stop(TEMP_DIR)
             st.rerun()
        
        # 显示进度
        steps = current_status.get("steps", {})
        
        for step_name, step_info in steps.items():
            status = step_info.get("status", "pending")
            msg = step_info.get("message", "")
            
            icon = "⏳"
            css_class = "step-card"
            if status == "running":
                icon = "🔄"
                css_class = "step-card step-running"
            elif status == "success":
                icon = "✅"
                css_class = "step-card step-success"
            elif status == "error":
                icon = "❌"
                css_class = "step-card step-error"
            
            st.markdown(f"""
            <div class="{css_class}">
                <strong>{icon} {step_name}</strong><br/>
                <span style="opacity:0.8; font-size:0.9em">{msg}</span>
            </div>
            """, unsafe_allow_html=True)
        
        # 显示日志
        with st.expander("查看详细日志", expanded=True):
            logs = current_status.get("logs", [])
            for log in logs[-10:]:  # 只显示最后10条
                st.text(log)
        
        # 自动刷新逻辑
        time.sleep(2)
        try:
            st.rerun()
        except AttributeError:
            st.experimental_rerun()
            
    else:
        # --- 空闲状态，显示输入表单 ---
        
        # 如果有上一次的结果，先显示结果
        if current_status:
            if current_status.get("error"):
                st.error(f"❌ 上次任务失败: {current_status.get('error')}")
            elif not current_status.get("is_running") and current_status.get("results"):
                st.success("🎉 上次任务执行成功！")
                results = current_status.get("results", {})
                st.markdown("### 📁 生成的文件")
                st.markdown(f"""
                - 字幕: `{results.get('vtt', 'N/A')}`
                - 翻译: `{results.get('txt', 'N/A')}`
                - 配音: `{results.get('mp3', 'N/A')}`
                - 视频: `{results.get('video', 'N/A')}`
                - 封面: `{results.get('cover', 'N/A')}`
                """)
                st.markdown("---")

        col1, col2 = st.columns([2, 1])
        with col1:
            workflow_url = st.text_input("YouTube视频URL", placeholder="https://www.youtube.com/watch?v=...", key="workflow_url_bg")
        with col2:
            auto_upload = st.checkbox("自动上传到B站", value=True, help="勾选后完成所有步骤会自动上传", key="auto_upload_bg")
        
        if st.button("🚀 启动后台任务", type="primary", use_container_width=True):
            if not workflow_url:
                st.error("请输入YouTube视频URL")
            else:
                # 收集配置
                task_config = {
                    "temp_dir": TEMP_DIR,
                    "workflow_url": workflow_url,
                    "auto_upload": auto_upload,
                    "api_url": API_URL,
                    "api_key": API_KEY,
                    "model_name": MODEL_NAME,
                    "bili_sess": BILI_SESSDATA,
                    "bili_ak": BILI_ACCESS_KEY_ID, # 虽然代码里暂时没用AK/SK上传，但保留配置
                    "bili_sk": BILI_ACCESS_KEY_SECRET,
                    "yt_cookies": YT_COOKIES,
                    "voice_choice": SELECTED_VOICE,
                    "max_workers": MAX_WORKERS,
                    "segment_size": SEGMENT_SIZE
                }
                
                # 启动线程
                thread = threading.Thread(target=background_workflow_task, args=(task_config,))
                thread.daemon = True # 设置为守护线程
                thread.start()
                
                st.success("任务已在后台启动！页面即将刷新...")
                time.sleep(1)
                try:
                    st.rerun()
                except AttributeError:
                    st.experimental_rerun()

with tab8:
    st.markdown("""
    <style>
    .batch-container {
        background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        padding: 2rem;
        border-radius: 10px;
        color: white;
    }
    .video-card {
        background: rgba(255,255,255,0.1);
        border: 1px solid rgba(255,255,255,0.2);
        border-radius: 8px;
        padding: 1rem;
        margin: 0.5rem 0;
    }
    .video-success {
        background: rgba(40,167,69,0.3);
        border-color: #28a745;
    }
    .video-error {
        background: rgba(220,53,69,0.3);
        border-color: #dc3545;
    }
    .video-running {
        background: rgba(255,193,7,0.3);
        border-color: #ffc107;
    }
    </style>
    <div class="batch-container">
        <h1 style="text-align:center; margin-bottom:1rem;">📦 批量工作流</h1>
        <p style="text-align:center; opacity:0.9;">输入多个YouTube链接，自动逐个完成翻译、配音、下载和上传</p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # 检查批量任务状态
    batch_status = BatchWorkflowManager.load_status(TEMP_DIR)
    batch_is_running = batch_status and batch_status.get("is_running", False)

    if batch_is_running:
        st.info(f"🔄 批量任务正在后台运行中... (开始时间: {batch_status.get('start_time')})")

        if st.button("🛑 中止批量任务", type="secondary", key="stop_batch_btn"):
            BatchWorkflowManager.request_stop(TEMP_DIR)
            st.rerun()

        # 总体进度
        total = batch_status.get("total_videos", 0)
        current_idx = batch_status.get("current_index", -1)
        done_count = sum(1 for v in batch_status.get("video_results", []) if v["status"] in ("success", "error"))
        st.progress(done_count / total if total > 0 else 0, text=f"总进度: {done_count}/{total}")

        # 每个视频的状态卡片
        for vr in batch_status.get("video_results", []):
            vs = vr.get("status", "pending")
            vm = vr.get("message", "")
            vurl = vr.get("url", "")
            vidx = vr.get("index", 0)

            icon = "⏳"
            css = "video-card"
            if vs == "running":
                icon = "🔄"
                css = "video-card video-running"
            elif vs == "success":
                icon = "✅"
                css = "video-card video-success"
            elif vs == "error":
                icon = "❌"
                css = "video-card video-error"

            st.markdown(f"""
            <div class="{css}">
                <strong>{icon} 视频 {vidx+1}</strong> &nbsp; <code style="color:#ddd;">{vurl[:60]}{'...' if len(vurl)>60 else ''}</code><br/>
                <span style="opacity:0.8; font-size:0.9em">{vm}</span>
            </div>
            """, unsafe_allow_html=True)

        # 日志
        with st.expander("查看详细日志", expanded=False):
            logs = batch_status.get("logs", [])
            for log in logs[-15:]:
                st.text(log)

        # 如果当前有正在处理的视频，显示其子步骤
        if 0 <= current_idx < total:
            current_vr = batch_status["video_results"][current_idx]
            if current_vr["status"] == "running":
                batch_dir = BatchWorkflowManager.get_batch_dir(TEMP_DIR)
                video_temp = os.path.join(batch_dir, f"video_{current_idx}")
                single_st = WorkflowManager.load_status(video_temp)
                if single_st:
                    with st.expander(f"📋 视频 {current_idx+1} 详细步骤", expanded=True):
                        for sn, si in single_st.get("steps", {}).items():
                            s_status = si.get("status", "pending")
                            s_msg = si.get("message", "")
                            s_icon = {"pending": "⏳", "running": "🔄", "success": "✅", "error": "❌"}.get(s_status, "⏳")
                            st.text(f"  {s_icon} {sn}: {s_msg}")

        # 自动刷新
        time.sleep(3)
        try:
            st.rerun()
        except AttributeError:
            st.experimental_rerun()

    else:
        # --- 空闲状态 ---

        # 显示上一次批量任务结果
        if batch_status:
            if batch_status.get("error"):
                st.error(f"❌ 上次批量任务失败: {batch_status.get('error')}")
            elif not batch_status.get("is_running"):
                st.success("🎉 上次批量任务已完成！")
                results_list = batch_status.get("video_results", [])
                success_count = sum(1 for v in results_list if v["status"] == "success")
                error_count = sum(1 for v in results_list if v["status"] == "error")
                elapsed = batch_status.get('elapsed_time', '')
                elapsed_text = f" &nbsp; ⏱️ 用时 {elapsed}" if elapsed else ""
                st.markdown(f"**结果**: ✅ 成功 {success_count} 个 &nbsp; ❌ 失败 {error_count} 个{elapsed_text}")
                for vr in results_list:
                    icon = "✅" if vr["status"] == "success" else "❌"
                    st.text(f"  {icon} {vr['url'][:80]} — {vr['message']}")
                st.markdown("---")

        # 初始化 session_state
        if "batch_urls_val" not in st.session_state:
            st.session_state["batch_urls_val"] = ""

        batch_urls = st.text_area(
            "YouTube视频URL（每行一个，可用 | 指定配音角色）",
            placeholder="https://www.youtube.com/watch?v=xxx | 女声-晓晓\nhttps://www.youtube.com/watch?v=yyy | 男声-云健\nhttps://www.youtube.com/watch?v=zzz",
            height=200,
            key="batch_urls_input",
            help="格式: URL | 配音角色（可选）。不指定角色则使用侧边栏默认角色。"
        )

        # 添加配音标签按钮
        tag_col1, tag_col2 = st.columns([1, 3])
        with tag_col1:
            if st.button("🏷️ 添加配音标签", key="add_voice_tag_btn", help="为所有未标记角色的URL添加当前侧边栏选中的配音角色标签"):
                current_alias = VOICE_ALIAS_REVERSE.get(SELECTED_VOICE, SELECTED_VOICE)
                lines = batch_urls.strip().splitlines() if batch_urls.strip() else []
                new_lines = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if "|" in line:
                        # 已有标签，不覆盖
                        new_lines.append(line)
                    else:
                        new_lines.append(f"{line} | {current_alias}")
                st.session_state["batch_urls_input"] = "\n".join(new_lines)
                st.rerun()
        with tag_col2:
            alias_list = "、".join(VOICE_ALIAS_MAP.keys())
            st.caption(f"可用角色: {alias_list}")

        bcol1, bcol2 = st.columns([2, 1])
        with bcol2:
            batch_auto_upload = st.checkbox("自动上传到B站", value=True, help="每个视频完成后自动上传", key="batch_auto_upload")

        if st.button("🚀 启动批量任务", type="primary", use_container_width=True, key="start_batch_btn"):
            # 解析URL和配音角色
            raw_lines = [u.strip() for u in batch_urls.strip().splitlines() if u.strip()]
            if not raw_lines:
                st.error("请输入至少一个YouTube视频URL")
            else:
                parsed_items = []
                for line in raw_lines:
                    if "|" in line:
                        parts = line.split("|", 1)
                        url_part = parts[0].strip()
                        voice_tag = parts[1].strip()
                        # 将中文别名转换为实际语音ID
                        voice_id = VOICE_ALIAS_MAP.get(voice_tag, voice_tag)
                        parsed_items.append({"url": url_part, "voice": voice_id})
                    else:
                        parsed_items.append({"url": line.strip(), "voice": SELECTED_VOICE})

                st.info(f"即将处理 {len(parsed_items)} 个视频")

                batch_task_config = {
                    "base_dir": TEMP_DIR,
                    "items": parsed_items,
                    "auto_upload": batch_auto_upload,
                    "api_url": API_URL,
                    "api_key": API_KEY,
                    "model_name": MODEL_NAME,
                    "bili_sess": BILI_SESSDATA,
                    "bili_ak": BILI_ACCESS_KEY_ID,
                    "bili_sk": BILI_ACCESS_KEY_SECRET,
                    "yt_cookies": YT_COOKIES,
                    "voice_choice": SELECTED_VOICE,
                    "max_workers": MAX_WORKERS,
                    "segment_size": SEGMENT_SIZE
                }

                thread = threading.Thread(target=background_batch_workflow_task, args=(batch_task_config,))
                thread.daemon = True
                thread.start()

                st.success("批量任务已在后台启动！页面即将刷新...")
                time.sleep(1)
                try:
                    st.rerun()
                except AttributeError:
                    st.experimental_rerun()


with tab1:
    st.header("1️⬇️ 下载YouTube字幕")
    youtube_url = st.text_input("YouTube视频URL", placeholder="https://www.youtube.com/watch?v=...", key="youtube_url_tab1")
    
    if st.button("下载字幕", type="primary", key="download_subtitles_btn"):
        if not youtube_url:
            st.error("请输入YouTube视频URL")
        else:
            # 清空temp目录
            clear_temp_directory()

            with st.spinner("正在下载字幕..."):
                temp_dir = TEMP_DIR
                try:
                    subtitles_dir = os.path.join(temp_dir, "subtitles")
                    os.makedirs(subtitles_dir, exist_ok=True)
                    
                    cookies_file_path = None
                    if YT_COOKIES.strip():
                        cookies_file_path = os.path.join(temp_dir, "youtube_cookies.txt")
                        with open(cookies_file_path, 'w', encoding='utf-8') as f:
                            f.write(YT_COOKIES.strip())
                    
                    args = [
                        '--write-auto-sub',
                        '--skip-download',
                        '--sub-langs', 'en',
                        '--quiet',
                        '-o', os.path.join(subtitles_dir, '%(title)s.%(ext)s'),
                        youtube_url
                    ]
                    run_yt_dlp_subprocess(args, cookies_file_path)
                    
                    vtt_files = list(Path(subtitles_dir).glob("*.vtt"))
                    if vtt_files:
                        original_file = vtt_files[0]
                        new_file = os.path.join(subtitles_dir, "word_level.vtt")
                        os.rename(original_file, new_file)
                        st.success(f"字幕下载成功！")
                        st.info(f"文件位置: {new_file}")
                    else:
                        st.error("未找到VTT字幕文件")
                        
                    st.markdown("---")
                    st.info("正在获取并翻译视频标题...")
                    
                    args = ['--dump-json', '--skip-download', '--quiet', youtube_url]
                    stdout = run_yt_dlp_subprocess(args, cookies_file_path)
                    info_dict = json.loads(stdout)
                    original_title = info_dict.get('title', '')
                    
                    if original_title:
                        st.text(f"原始标题: {original_title}")
                        
                        SYSTEM_PROMPT = """你是爆款视频up主，将英文标题翻译成吸引眼球的爆款视频中文标题，直接输出翻译结果，不要解释。"""
                        
                        import requests
                        payload = {
                            "model": MODEL_NAME,
                            "messages": [
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": original_title}
                            ]
                        }
                        headers = {
                            "Authorization": f"Bearer {API_KEY}",
                            "Content-Type": "application/json"
                        }
                        
                        response = requests.post(API_URL, json=payload, headers=headers, timeout=60)
                        response_data = response.json()
                        
                        translated_title_with_markdown = response_data['choices'][0]['message']['content']
                        translated_title = translated_title_with_markdown.replace('**', '').strip()
                        
                        st.text(f"翻译标题: {translated_title}")
                        
                        TAGS_PROMPT = f"""根据以下视频标题，生成5-8个B站视频标签（只输出标签，用逗号分隔）：
标题：{translated_title}
示例标签：科技,人工智能,AI,机器学习,未来
只输出标签，不要其他内容。"""
                        
                        tags_payload = {
                            "model": MODEL_NAME,
                            "messages": [
                                {"role": "system", "content": "你是一个专业的B站运营助手"},
                                {"role": "user", "content": TAGS_PROMPT}
                            ]
                        }
                        
                        tags_response = requests.post(API_URL, json=tags_payload, headers=headers, timeout=60)
                        tags_data = tags_response.json()
                        
                        tags_content = tags_data['choices'][0]['message']['content']
                        tags_list = [t.strip() for t in tags_content.replace('，', ',').split(',') if t.strip()]
                        tags_str = ','.join(tags_list)
                        
                        st.text(f"生成标签: {tags_str}")
                        
                        upload_config_file = os.path.join(subtitles_dir, "upload_config.pkl")
                        import pickle
                        upload_data = {
                            'title_desc': f'(中配){translated_title}',
                            'tags': tags_list
                        }
                        
                        with open(upload_config_file, 'wb') as f:
                            pickle.dump(upload_data, f)
                        
                        st.success("标题翻译和标签生成完成！")
                        st.info(f"配置已保存到: {upload_config_file}")
                    else:
                        st.warning("无法获取视频标题")
                        
                except Exception as e:
                    st.error(f"下载失败: {str(e)}")
    
    vtt_file = os.path.join(TEMP_DIR, "subtitles", "word_level.vtt")
    
    with tab2:
        st.header("2️⚙️ 翻译字幕")
        vtt_file_path = st.text_input("VTT字幕文件路径", value=vtt_file, key="vtt_file_path")
        
        if st.button("开始翻译", type="primary", key="start_translate_btn"):
            if not os.path.exists(vtt_file_path):
                st.error(f"文件不存在: {vtt_file_path}")
            else:
                with st.spinner("正在翻译字幕..."):
                    try:
                        def vtt_to_sentences(vtt_text):
                            """将带逐词时间戳的VTT转换为按句分段的文本"""
                            # 正则：cue 头（起止时间）
                            CUE_HEADER_RE = re.compile(
                                r'^(\d{2}:\d{2}:\d{2}\.\d{3})\s*--> (\d{2}:\d{2}:\d{2}\.\d{3})'
                            )
                            
                            # 正则：逐词时间戳 <HH:MM:SS.mmm>
                            TS_TAG_RE = re.compile(r'<(\d{2}:\d{2}:\d{2}\.\d{3})>')
                            
                            # 正则：清理 <c> 或 <c.xxx> 样式标签
                            C_TAG_RE = re.compile(r'</?c(?:\.[^>]*)?>', re.IGNORECASE)
                            
                            SENTENCE_END = ".!?"
                            
                            lines = vtt_text.splitlines()
                            sentences = []
                            current_words = []
                            current_sentence_start_time = None
                            
                            effective_time = None
                            cue_start_time = None
                            
                            def flush_sentence():
                                nonlocal current_words, current_sentence_start_time
                                if not current_words:
                                    return
                                text = " ".join(current_words)
                                text = re.sub(r"\s+([,.;!?])", r"\1", text)
                                text = re.sub(r"\(\s+", "(", text)
                                text = re.sub(r"\s+\)", ")", text)
                                start_ts = current_sentence_start_time or cue_start_time or effective_time or "00:00:00.000"
                                sentences.append(f"({start_ts}) {text}")
                                current_words = []
                                current_sentence_start_time = None
                            
                            for line in lines:
                                line = line.strip("\ufeff\r\n")
                                
                                # cue 头
                                m = CUE_HEADER_RE.match(line)
                                if m:
                                    cue_start_time = m.group(1)
                                    effective_time = cue_start_time
                                    continue
                                
                                # 只处理含逐词时间戳的行
                                if not TS_TAG_RE.search(line):
                                    continue
                                
                                # 清理 <c> 标签，并把 <timestamp> 变成 [[TS:...]] 哨兵
                                s = C_TAG_RE.sub("", line)
                                s = TS_TAG_RE.sub(lambda mm: f" [[TS:{mm.group(1)}]] ", s)
                                
                                # 扫描 token
                                for token in s.split():
                                    if token.startswith("[[TS:") and token.endswith("]]"):
                                        effective_time = token[5:-2]
                                        continue
                                    
                                    word = token.strip()
                                    if not word:
                                        continue
                                    
                                    # 记录首词时间
                                    if current_sentence_start_time is None:
                                        current_sentence_start_time = effective_time or cue_start_time
                                    
                                    current_words.append(word)
                                    
                                    # 句子结束判定（句号、问号、叹号）
                                    if word.strip().endswith(tuple(SENTENCE_END)):
                                        flush_sentence()
                            
                            # 文件结束，收尾
                            flush_sentence()
                            return sentences
                        
                        vtt_content = Path(vtt_file_path).read_text(encoding="utf-8", errors="ignore")
                        sentences = vtt_to_sentences(vtt_content)
                        
                        print(f"调试信息：解析出 {len(sentences)} 个句子")
                        if sentences:
                            print(f"前3个句子示例：")
                            for i, s in enumerate(sentences[:3]):
                                print(f"  {i+1}: {s[:100]}...")
                        
                        output_txt_file = os.path.splitext(vtt_file_path)[0] + ".txt"
                        with open(output_txt_file, 'w', encoding='utf-8') as f:
                            for seg in sentences:
                                f.write(seg + "\n\n")
                        
                        paragraphs = [line.strip() for line in open(output_txt_file, 'r', encoding='utf-8') if line.strip()]
                        
                        print(f"调试信息：读取到 {len(paragraphs)} 个段落")
                        
                        batched_paragraphs = []
                        current_batch = []
                        current_char_count = 0
                        
                        for i, paragraph in enumerate(paragraphs):
                            paragraph_char_count = len(paragraph)
                            if (len(current_batch) >= SEGMENT_SIZE) or (current_char_count + paragraph_char_count > 2000 and current_batch):
                                batched_paragraphs.append("\n".join(current_batch))
                                print(f"调试信息：分段 {len(batched_paragraphs)} 包含 {len(current_batch)} 个段落，共 {current_char_count} 字符")
                                current_batch = [paragraph]
                                current_char_count = paragraph_char_count
                            else:
                                current_batch.append(paragraph)
                                current_char_count += paragraph_char_count
                        
                        if current_batch:
                            batched_paragraphs.append("\n".join(current_batch))
                            print(f"调试信息：最后一个分段 {len(batched_paragraphs)} 包含 {len(current_batch)} 个段落，共 {current_char_count} 字符")
                        
                        print(f"调试信息：总共 {len(batched_paragraphs)} 个翻译分段")
                        
                        def translate_batch(batch, batch_index):
                            try:
                                print(f"调试信息：开始翻译分段 {batch_index}，内容长度: {len(batch)} 字符")
                                print(f"分段内容预览: {batch[:200]}...")
                                
                                url = API_URL
                                headers = {
                                    "Content-Type": "application/json",
                                    "Authorization": f"Bearer {API_KEY}"
                                }
                                payload = {
                                    "model": MODEL_NAME,
                                    "messages": [
                                        {"role": "system", "content": "# Role: 专业翻译官\n\n## Profile\n- author: LangGPT优化中心\n- version: 2.1\n- language: 中英双语\n- description: 专注于文本精准转换的AI翻译专家，擅长处理技术文档和日常对话场景\n\n## Background\n用户在跨国协作、技术文档处理、社交媒体互动等场景中，需要将外文内容准确转化为中文，同时保持特殊格式元素完整\n\n## Skills\n1. 多语言文本解析与重构能力\n2. 时间戳识别与格式保留技术\n3. 语义通顺度校验算法\n4. 格式控制与冗余内容过滤\n\n## Goals\n1. 实现原文语义的精准转换\n2. 保持时间戳等特殊格式元素\n3. 确保输出结果自然流畅\n4. 排除非翻译内容添加\n\n## Constraints\n1. 禁止添加解释性文字\n2. 禁用注释或说明性符号\n3. 保留原始时间戳格式（如(12:34））\n4. 不处理非文本元素（如图片/表格）\n5. 禁止使用工具调用（tool_calls）功能，禁止调用外部翻译api进行翻译\n\n## Workflow\n1. 接收输入内容，检测语言类型\n2. 识别并标记特殊格式元素\n3. 执行语义转换：\n   - 日常用语：采用口语化表达\n   - 技术术语：使用标准化译法\n5. 输出纯翻译结果\n\n## OutputFormat\n仅返回符合以下要求的翻译文本：\n1. 中文书面语表达\n2. 保留原始段落结构\n3. 时间戳保持(MM:SS)或(HH:MM:SS)格式\n4. 无任何附加符号或说明\n4. 尽量只要中文，不要中英文夹杂。"},
                                        {"role": "user", "content": batch}
                                    ],
                                    "stream": False,
                                    "max_tokens": 4000
                                }
                                print(f"调试信息：分段 {batch_index} 发送API请求到 {url}")
                                response = requests.post(url, json=payload, headers=headers, timeout=60)
                                print(f"调试信息：分段 {batch_index} API响应状态码: {response.status_code}")
                                response.raise_for_status()
                                result = response.json()
                                translated_content = result['choices'][0]['message']['content']
                                print(f"调试信息：分段 {batch_index} 翻译结果长度: {len(translated_content)} 字符")
                                print(f"翻译结果预览: {translated_content[:200]}...")
                                return translated_content
                            except Exception as e:
                                print(f"调试信息：分段 {batch_index} 翻译失败: {str(e)}")
                                import traceback
                                print(f"调试信息：分段 {batch_index} 错误详情: {traceback.format_exc()}")
                                return f"Error: {str(e)}"
                        
                        translated_results = {}
                        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                            futures = {executor.submit(translate_batch, batch, i): i for i, batch in enumerate(batched_paragraphs)}
                            
                            progress_bar = st.progress(0)
                            completed = 0
                            for future in as_completed(futures):
                                index = futures[future]
                                result = future.result()
                                if not result.startswith("Error:"):
                                    translated_results[index] = result
                                completed += 1
                                progress_bar.progress(completed / len(batched_paragraphs))
                        
                        translated_paragraphs = []
                        failed_count = 0
                        
                        for i in range(len(batched_paragraphs)):
                            if i in translated_results:
                                translated_paragraphs.append(translated_results[i])
                            else:
                                failed_count += 1
                        
                        output_translated_file = os.path.splitext(vtt_file_path)[0] + "_translated.txt"
                        with open(output_translated_file, 'w', encoding='utf-8') as f:
                            for seg in translated_paragraphs:
                                cleaned = seg.replace('&gt;', '').replace('>>', '').replace('&trash;', '').replace('> ', '').replace('&nbsp;', '').replace('_', '').replace('＞', '').replace('[音乐]', '')
                                f.write(cleaned + "\n\n")
                        
                        st.success(f"翻译完成！成功: {len(translated_paragraphs)} 段落，失败: {failed_count}")
                        st.info(f"输出文件: {output_translated_file}")
                        
                    except Exception as e:
                        st.error(f"翻译失败: {str(e)}")
    
    txt_file = os.path.join(TEMP_DIR, "subtitles", os.path.splitext(os.path.basename(vtt_file))[0] + "_translated.txt")
    mp3_file = os.path.join(TEMP_DIR, "subtitles", os.path.splitext(os.path.basename(vtt_file))[0] + "_translated.mp3")
    
    with tab3:
        st.header("3️🗣️ TTS字幕转语音")
        txt_file_path = st.text_input("翻译后的TXT文件路径", value=txt_file, key="txt_file_path")
        
        if st.button("开始转换语音", type="primary", key="start_tts_btn"):
            if not os.path.exists(txt_file_path):
                st.error(f"文件不存在: {txt_file_path}")
            else:
                with st.spinner("正在转换语音..."):
                    try:
                        output_mp3 = os.path.splitext(txt_file_path)[0] + ".mp3"
                        subtitles_dir = os.path.dirname(txt_file_path)

                        result = process_tts_with_speed_adjustment(txt_file_path, output_mp3, subtitles_dir)

                        if result:
                            st.success(f"语音转换完成！")
                            st.info(f"输出文件: {output_mp3}")
                        else:
                            st.error("没有成功生成音频文件")
                    except Exception as e:
                        st.error(f"转换失败: {str(e)}")
    
    mp3_file = os.path.join(TEMP_DIR, "subtitles", os.path.splitext(os.path.basename(vtt_file))[0] + "_translated.mp3")
    
    with tab4:
        st.header("4️🎬️ 下载视频")
        
        youtube_url = st.text_input("YouTube视频URL", placeholder="https://www.youtube.com/watch?v=...", key="video_url")
        
        cookies_file_path = None
        if YT_COOKIES.strip():
            temp_dir = TEMP_DIR
            cookies_file_path = os.path.join(temp_dir, "youtube_cookies.txt")
            with open(cookies_file_path, 'w', encoding='utf-8') as f:
                f.write(YT_COOKIES.strip())
        
        if st.button("下载视频", type="primary", key="download_video_btn"):
            if not youtube_url:
                st.error("请输入YouTube视频URL")
            else:
                with st.spinner("正在下载视频..."):
                    try:
                        temp_dir = TEMP_DIR
                        downloaded_video_base_name = os.path.join(temp_dir, "subtitles", "downloaded_video")
                        new_audio_path = mp3_file
                        
                        args = [
                            '-f', 'best',
                            '-o', f'{downloaded_video_base_name}.%(ext)s',
                            '--no-playlist',
                            '--quiet',
                            youtube_url
                        ]
                        run_yt_dlp_subprocess(args, cookies_file_path)
                        
                        downloaded_files = glob.glob(f"{downloaded_video_base_name}.*")
                        if downloaded_files:
                            actual_downloaded_video_path = downloaded_files[0]
                        else:
                            raise FileNotFoundError(f"yt-dlp did not download a file")
                        
                        if os.path.exists(new_audio_path):
                            final_video_path = os.path.splitext(mp3_file)[0] + ".mp4"
                            try:
                                subprocess.run(['ffmpeg', '-y', '-i', actual_downloaded_video_path, '-i', new_audio_path,
                                                    '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v:0', '-map', '1:a:0',
                                                    final_video_path], check=True, capture_output=True, text=True)
                                
                                if os.path.exists(actual_downloaded_video_path):
                                    os.remove(actual_downloaded_video_path)
                                
                                st.success(f"视频下载完成！")
                                st.info(f"输出文件: {final_video_path}")
                            except subprocess.CalledProcessError as ffmpeg_error:
                                st.warning("⚠️ 视频已下载成功，但音视频合并时出现FFmpeg错误")
                                st.info(f"已下载视频位置: {actual_downloaded_video_path}")
                                st.info("提示: 你可以手动使用FFmpeg或视频编辑软件将音频合并到视频中")
                                
                                if os.path.exists(actual_downloaded_video_path):
                                    import shutil
                                    manual_video_path = os.path.splitext(mp3_file)[0] + "_video_only.mp4"
                                    shutil.copy2(actual_downloaded_video_path, manual_video_path)
                                    st.success(f"已复制视频文件到: {manual_video_path}")
                                    
                        else:
                            st.error(f"音频文件不存在: {new_audio_path}")
                            st.info(f"已下载视频位置: {actual_downloaded_video_path}")
                    except Exception as e:
                        error_str = str(e)
                        if "Non-relative patterns" in error_str:
                            st.warning("⚠️ 视频已下载成功，但M3U8修复时出现兼容性问题")
                            st.info("这通常不影响视频的正常使用")
                            
                        downloaded_files = glob.glob(f"{downloaded_video_base_name}.*")
                        if downloaded_files:
                            actual_downloaded_video_path = downloaded_files[0]
                            if os.path.exists(actual_downloaded_video_path):
                                manual_video_path = os.path.splitext(mp3_file)[0] + "_video_only.mp4"
                                import shutil
                                shutil.copy2(actual_downloaded_video_path, manual_video_path)
                                st.success(f"已保存视频文件到: {manual_video_path}")
                        else:
                            st.error(f"下载失败: {str(e)}")
    
    final_video = os.path.splitext(mp3_file)[0] + ".mp4"
    
    with tab5:
        st.header("5️🖼️ 处理封面")
        
        youtube_url = st.text_input("YouTube视频URL", placeholder="https://www.youtube.com/watch?v=...", key="cover_url")
        
        cookies_file_path = None
        if YT_COOKIES.strip():
            temp_dir = TEMP_DIR
            cookies_file_path = os.path.join(temp_dir, "youtube_cookies.txt")
            with open(cookies_file_path, 'w', encoding='utf-8') as f:
                f.write(YT_COOKIES.strip())
        
        if st.button("下载封面", type="primary", key="download_cover_btn"):
            if not youtube_url:
                st.error("请输入YouTube视频URL")
            else:
                with st.spinner("正在下载封面..."):
                    try:
                        temp_dir = TEMP_DIR
                        
                        args = [
                            '--skip-download',
                            '--write-thumbnail',
                            '--no-playlist',
                            '--quiet',
                            '-o', os.path.join(temp_dir, "subtitles", 'cover.%(ext)s'),
                            youtube_url
                        ]
                        run_yt_dlp_subprocess(args, cookies_file_path)
                        
                        input_path = os.path.join(temp_dir, "subtitles", "cover.webp")
                        output_path = os.path.join(temp_dir, "subtitles", "cover.jpeg")
                        
                        if not os.path.exists(input_path):
                            st.error(f"文件不存在: {input_path}")
                        else:
                            quality = 90
                            with Image.open(input_path) as img:
                                if img.mode != 'RGB':
                                    img = img.convert('RGB')
                                img.save(output_path, 'jpeg', quality=quality)

                            current_size_kb = os.path.getsize(output_path) / 1024
                            while current_size_kb > 50 and quality > 4:
                                quality -= 5
                                img.save(output_path, 'jpeg', quality=quality)
                                current_size_kb = os.path.getsize(output_path) / 1024
                                print(f"当前大小: {current_size_kb:.2f} KB, 质量: {quality}")
                            
                            st.success(f"封面处理完成！")
                            st.info(f"输出文件: {output_path}")
                    except Exception as e:
                        st.error(f"封面处理失败: {str(e)}")
    
    cover_file = os.path.join(TEMP_DIR, "subtitles", "cover.jpeg")
    
    with tab6:
        st.header("6️✂️ 视频剪辑")
        
        video_file = st.text_input("视频文件路径", value=final_video, key="video_file_path_tab6")
        
        trim_enabled = st.checkbox("启用剪辑（删除违规片段）", value=False, key="trim_enabled")
        trim_start = st.text_input("剪辑开始时间", value="6:45", help="格式: MM:SS", key="trim_start")
        trim_end = st.text_input("剪辑结束时间", value="6:55", help="格式: MM:SS", key="trim_end")
        
        if trim_enabled and st.button("执行剪辑", type="primary", key="execute_trim_btn"):
            if not os.path.exists(video_file):
                st.error(f"视频文件不存在: {video_file}")
            else:
                with st.spinner("正在剪辑视频..."):
                    try:
                        output_part1 = os.path.join(os.path.dirname(video_file), "final_video_part1.mp4")
                        output_part2 = os.path.join(os.path.dirname(video_file), "final_video_part2.mp4")
                        output_video_trimmed = os.path.join(os.path.dirname(video_file), "final_video_trimmed.mp4")
                        temp_concat_file = os.path.join(os.path.dirname(video_file), "concat_list.txt")
                        
                        subprocess.run(['ffmpeg', '-y', '-i', video_file, '-to', trim_start,
                                                '-c', 'copy', output_part1], check=True)
                        subprocess.run(['ffmpeg', '-y', '-i', video_file, '-ss', trim_end,
                                                '-c', 'copy', output_part2], check=True)
                        
                        if os.path.exists(output_part1) and os.path.getsize(output_part1) > 0:
                            with open(temp_concat_file, 'w') as f:
                                f.write(f"file '{output_part1}'\n")
                        if os.path.exists(output_part2) and os.path.getsize(output_part2) > 0:
                            with open(temp_concat_file, 'a') as f:
                                f.write(f"file '{output_part2}'\n")
                        
                        subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', temp_concat_file,
                                                '-c', 'copy', output_video_trimmed], check=True)
                        
                        if os.path.exists(output_video_trimmed) and os.path.getsize(output_video_trimmed) > 0:
                            os.replace(output_video_trimmed, video_file)
                            st.success(f"视频剪辑完成！")
                            st.info(f"删除了从 {trim_start} 到 {trim_end} 的片段")
                        else:
                            st.error("剪辑失败")
                    except Exception as e:
                        st.error(f"剪辑失败: {str(e)}")
        else:
            st.info("剪辑未启用，跳过")
    
    trimmed_video = os.path.splitext(mp3_file)[0] + ".mp4"
    
    upload_config_file = os.path.join(TEMP_DIR, "subtitles", "upload_config.pkl")
    loaded_title_desc = None
    loaded_tags_list = None
    
    if os.path.exists(upload_config_file):
        try:
            import pickle
            with open(upload_config_file, 'rb') as f:
                loaded_data = pickle.load(f)
            loaded_title_desc = loaded_data.get('title_desc')
            loaded_tags_list = loaded_data.get('tags')
        except Exception:
            pass
    
    with tab7:
        st.header("7️📤️ 上传B站")
        
        video_file = st.text_input("视频文件路径", value=trimmed_video, key="video_file_path_tab7")
        cover_file_path_input = st.text_input("封面文件路径", value=cover_file, key="cover_file_path")
        
        default_title = loaded_title_desc if loaded_title_desc else f"(中配)请先下载字幕获取标题"
        title = st.text_input("视频标题", value=default_title, help="留空则使用翻译后的标题", key="title")
        
        default_tags = ','.join(loaded_tags_list) if loaded_tags_list else "科技"
        tags = st.text_input("视频标签", value=default_tags, key="tags_tab7")
        
        if loaded_title_desc:
            st.success("已从下载字幕步骤获取标题和标签")
        else:
            st.warning("未找到标题和标签配置，请先下载字幕")
        
        bilibili_enabled = st.checkbox("上传到B站", value=False, key="bilibili_enabled")
        
        if bilibili_enabled and st.button("开始上传", type="primary", key="start_upload_btn"):
            if not os.path.exists(video_file):
                st.error(f"视频文件不存在: {video_file}")
            elif not os.path.exists(cover_file_path_input):
                st.error(f"封面文件不存在: {cover_file_path_input}")
            else:
                with st.spinner("正在上传到B站..."):
                    try:
                        credential = Credential(
                            sessdata=BILI_SESSDATA,
                            bili_jct="bcd4ba0d9ab8a7b95485798ed8097d26"
                        )
                        
                        vu_meta = VideoMeta(
                            tid=130,
                            title=title or "(中配)AI幻觉造出科学发现？！#ai幻觉",
                            tags=tags.split(',') if tags else ['科技'],
                            desc=title or "(中配)AI幻觉造出科学发现？！#ai幻觉",
                            cover=cover_file_path_input,
                            no_reprint=True
                        )
                        
                        async def main_upload():
                            page = VideoUploaderPage(
                                path=video_file,
                                title=title or "(中配)AI幻觉造出科学发现？！#ai幻觉",
                                description=title or "(中配)AI幻觉造出科学发现？！#ai幻觉",
                            )
                            
                            uploader = video_uploader.VideoUploader([page], vu_meta, credential, line=video_uploader.Lines.QN)
                            
                            @uploader.on("__ALL__")
                            async def ev(data):
                                pass
                            
                            await uploader.start()
                            
                        asyncio.run(main_upload())
                        
                        st.success("上传完成！")
                    except Exception as e:
                        import traceback
                        st.error(f"上传失败: {str(e)}")
                        st.markdown("### 调试信息")
                        st.text(f"错误类型: {type(e).__name__}")
                        st.text(f"完整错误: {repr(e)}")
                        st.text(f"Traceback:\n{traceback.format_exc()}")
                        
                        st.markdown("### 配置检查")
                        st.text(f"BILI_SESSDATA: {'已设置' if BILI_SESSDATA else '未设置'} (长度: {len(BILI_SESSDATA)})")
                        st.text(f"BILI_ACCESS_KEY_ID: {'已设置' if BILI_ACCESS_KEY_ID else '未设置'}")
                        st.text(f"BILI_ACCESS_KEY_SECRET: {'已设置' if BILI_ACCESS_KEY_SECRET else '未设置'}")
                        st.text(f"视频文件: {video_file}")
                        st.text(f"封面文件: {cover_file_path_input}")
                        st.text(f"视频文件大小: {os.path.getsize(video_file) / 1024 / 1024:.2f} MB" if os.path.exists(video_file) else "视频文件不存在")
                        st.text(f"封面文件大小: {os.path.getsize(cover_file_path_input) / 1024:.2f} KB" if os.path.exists(cover_file_path_input) else "封面文件不存在")

st.markdown("---")
st.info("💡 注意事项：")
st.markdown("""
1. API Key等敏感信息建议通过HuggingFace Spaces的Secrets管理，不要直接在代码中硬编码
2. 处理大型视频时，TTS转换和视频处理可能需要较长时间，请耐心等待
3. B站上传功能需要有效的sessdata和access_key_id
4. 视频剪辑功能会永久修改视频文件，请谨慎使用
5. 建议先在小视频上测试流程，确认无误后再处理大视频
""")
