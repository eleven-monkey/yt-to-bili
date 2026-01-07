# -*- coding: utf-8 -*-
import os
import re
import json
import time
import shutil
import subprocess
import asyncio
import glob
import random
from pathlib import Path
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from PIL import Image

import streamlit as st
import yt_dlp
import requests
import edge_tts
from bilibili_api import sync, video_uploader, Credential
from bilibili_api.video_uploader import VideoUploaderPage, VideoMeta
import pydub
import pickle

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
            config[key] = env_val
            
    # 兼容旧的/拼写错误的变量名 BILI_SESSIDATA -> BILI_SESSDATA
    if "BILI_SESSDATA" not in config and os.getenv("BILI_SESSIDATA"):
        config["BILI_SESSDATA"] = os.getenv("BILI_SESSIDATA")
            
    return config

env_config = load_env_config()

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
def translate_subtitles_from_vtt(vtt_file_path):
    """从VTT文件翻译字幕，生成带时间戳的文本文件（单步执行的完整逻辑）"""
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

            print(f"调试信息：分段 {batch_index} 翻译完成，返回内容长度: {len(translated_content)} 字符")
            print(f"翻译内容预览: {translated_content[:200]}...")
            return translated_content
        except Exception as e:
            print(f"调试信息：分段 {batch_index} 错误详情: {traceback.format_exc()}")
            return f"Error: {str(e)}"

    translated_results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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

# TTS 相关函数 - 移到模块级别以支持多进程
async def text_to_speech(text, output_file, voice="zh-CN-XiaoxiaoNeural", max_retries=5):
    """
    将文本转换为语音并保存为音频文件
    添加重试机制和延迟，处理edge-tts API的503错误
    """
    retry_count = 0
    base_delay = 1  # 基础延迟时间（秒）
    while retry_count <= max_retries:
        try:
            # 添加随机延迟，避免请求过于规律
            if retry_count > 0:
                delay = base_delay * (2 ** (retry_count - 1)) + (random.random() * 0.5)
                print(f"第{retry_count}次重试，等待{delay:.2f}秒后继续...")
                await asyncio.sleep(delay)
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_file)
            return  # 成功则退出循环
        except Exception as e:
            error_msg = str(e).lower()
            retry_count += 1
            # 检查是否是503错误或其他可重试的错误
            if "503" in error_msg or "timeout" in error_msg or "connection" in error_msg:
                if retry_count <= max_retries:
                    print(f"遇到API错误: {e}，准备第{retry_count}次重试...")
                else:
                    print(f"达到最大重试次数({max_retries})，无法完成转换: {e}")
                    raise  # 达到最大重试次数，抛出异常
            else:
                # 其他类型的错误直接抛出
                print(f"遇到非重试类型的错误: {e}")
                raise

def run_text_to_speech(text, output_file, voice="zh-CN-XiaoxiaoNeural", max_retries=5):
    """
    在多进程中运行text_to_speech的包装函数
    """
    # 创建新的事件循环并在其中运行异步函数
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(text_to_speech(text, output_file, voice, max_retries))
    finally:
        loop.close()

def process_segment(task):
    """
    处理单个文本段落的函数，用于多进程处理
    """
    i, timestamp, txt, temp_dir, voice = task
    try:
        cleaned_timestamp = re.sub(r'[^\w\d]+', '_', timestamp)
        file_name = f"{cleaned_timestamp}.mp3"
        output_file = os.path.join(temp_dir, file_name)

        print(f"进程正在处理段落 {i+1}: {timestamp} - {txt[:30]}...")
        run_text_to_speech(txt, output_file, voice)

        time_ms = parse_timestamp(f"({timestamp})")
        return i, output_file, time_ms, None
    except Exception as e:
        return i, None, None, f"处理段落 {i+1} 时出错: {str(e)}"

def adjust_audio_speed(task):
    """
    调整音频速度的函数，用于多进程处理
    """
    i, temp_output, target_duration, speed_factor = task
    temp_output_processed = temp_output + '.tmp.mp3'
    try:
        subprocess.run([
            'ffmpeg', '-y', '-i', temp_output,
            '-filter:a', f'atempo={speed_factor}',
            temp_output_processed
        ], check=True, capture_output=True)
        # Replace original file with processed one
        os.replace(temp_output_processed, temp_output)
        return i, temp_output, None  # 返回实际的文件路径
    except subprocess.CalledProcessError as e:
        # Clean up temporary file if it exists
        if os.path.exists(temp_output_processed):
            os.remove(temp_output_processed)
        return i, None, f"音频速度调整失败 {i+1}: {e}"

def process_tts_with_speed_adjustment(txt_file_path, output_mp3_path, subtitles_dir):
    """处理TTS转换并进行音频速度调整以避免重叠"""
    print("="*50, flush=True)
    print("开始TTS转换流程", flush=True)
    print("="*50, flush=True)

    with open(txt_file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"txt_file_path: {txt_file_path}", flush=True)
    print(f"文件是否存在: {os.path.exists(txt_file_path)}", flush=True)
    print(f"content长度: {len(content)} 字符", flush=True)

    # 使用笔记本中的正确正则表达式
    pattern = r'[\\(（](\d{1,2})?:?(\d{1,3}):(\d{1,2})(?:\.(\d{1,3}))?[\\)）](.+?)(?=[\\(（](?:\d{1,2})?:?(\d{1,3}):(\d{1,2})(?:\.(\d{1,3}))?[\\)）]|$)'
    matches = list(re.finditer(pattern, content, re.DOTALL))
    print(f"匹配到的segments数量: {len(matches)}", flush=True)

    segments = []
    for match in matches:
        timestamp_string = match.group(0)
        content_text = match.group(5).strip()
        if content_text:
            # 提取时间戳部分
            timestamp_match = re.match(r'[\\(（](.+?)[\\)）]', timestamp_string)
            if timestamp_match:
                timestamp = timestamp_match.group(1)
                segments.append((timestamp, content_text))

    print(f"解析出的segments数量: {len(segments)}", flush=True)
    if segments:
        print(f"前3个segments:", flush=True)
        for i, (ts, txt) in enumerate(segments[:3]):
            print(f"  {i+1}: ({ts}) {txt[:50]}...", flush=True)

    temp_dir = os.path.dirname(output_mp3_path) if os.path.dirname(output_mp3_path) else TEMP_DIR

    tasks = []
    for i, (timestamp, txt) in enumerate(segments):
        cleaned_timestamp = re.sub(r'[^\w\d]+', '_', timestamp)
        file_name = f"{cleaned_timestamp}.mp3"
        output_file = os.path.join(temp_dir, file_name)
        tasks.append((i, timestamp, txt, temp_dir, SELECTED_VOICE))

    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(process_segment, task) for task in tasks]

        audio_files = [None] * len(tasks)

        for future in as_completed(futures):
            index, output_file, time_ms, error = future.result()
            if error:
                print(f"警告: {error}")
            if output_file and os.path.exists(output_file):
                audio_files[index] = (output_file, time_ms)

        audio_files = [af for af in audio_files if af is not None]

    print(f"调试信息：audio_files 数量: {len(audio_files)}")
    if audio_files:
        print(f"调试信息：audio_files[0] 结构: {audio_files[0]}")

    audio_files.sort(key=lambda x: x[1])

    if audio_files:
        # 导入必要的库
        from pydub import AudioSegment
        import numpy as np
        from multiprocessing import shared_memory

        # 音频速度调整以避免重叠 (在混音之前进行)
        print("开始音频速度调整，segments数量:", len(segments))
        print("segments示例:", segments[:2] if segments else '空')

        processed_audio_segments = []
        for i, (audio_file_path, time_ms) in enumerate(audio_files):
            audio = AudioSegment.from_file(audio_file_path)
            processed_audio_segments.append((audio_file_path, time_ms, audio))

        # 计算需要调整速度的音频片段
        speed_adjust_tasks_list = []
        print(f"开始计算速度调整任务，片段总数: {len(processed_audio_segments)}")

        for i, (audio_file_path, time_ms, audio) in enumerate(processed_audio_segments[:-1]):
            current_len = len(audio)
            end_time = time_ms + current_len

            # 计算下一个片段的开始时间
            if i + 1 < len(processed_audio_segments):
                next_start = processed_audio_segments[i+1][1]
                if end_time > next_start + 100:  # 如果重叠超过100ms
                    target = next_start - time_ms - 50  # 留50ms缓冲
                    if target > 100:  # 目标时长至少100ms
                        factor = min(current_len / target, 2.0)  # 最多加速2倍
                        print(f"片段{i}: 当前时间={time_ms}ms, 下一个时间={next_start}ms, 目标时长={target}ms, 实际时长={current_len}ms")
                        print(f"  需要加速: 因子={factor:.2f}")
                        if factor > 1.0:  # 只有需要加速时才调整
                            # 创建临时文件用于速度调整
                            temp_speed_file = audio_file_path.replace('.mp3', '_speed.mp3')
                            audio.export(temp_speed_file, format="mp3")
                            speed_adjust_tasks_list.append((i, temp_speed_file, target, factor))

        print(f"需要调整速度的音频片段数量: {len(speed_adjust_tasks_list)}")

        # 执行速度调整
        if speed_adjust_tasks_list:
            print(f"开始处理 {len(speed_adjust_tasks_list)} 个音频速度调整任务...")

            with ProcessPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(adjust_audio_speed, task) for task in speed_adjust_tasks_list]

                for future in as_completed(futures):
                    try:
                        result = future.result()
                        if result and len(result) >= 3:
                            i, adjusted_file_path, error = result
                            if error:
                                print(f"速度调整失败 {i}: {error}")
                                continue
                            if adjusted_file_path and os.path.exists(adjusted_file_path):
                                # 验证调整后的文件确实存在
                                print(f"速度调整成功 {i}: {adjusted_file_path}")
                    except Exception as e:
                        print(f"音频速度调整任务失败: {e}")

        # 现在进行最终混音 - 使用调整后的音频文件
        print(f"开始混音 {len(processed_audio_segments)} 个音频片段")

        # 导入必要的库
        from pydub import AudioSegment
        import numpy as np
        from multiprocessing import shared_memory

        SR = 24000
        N_CH = 1
        WIDTH = 2

        def to_int16_samples(audio_segment):
            audio = audio_segment.set_frame_rate(SR).set_channels(N_CH).set_sample_width(WIDTH)
            return np.frombuffer(audio_segment.raw_data, dtype=np.int16)

        # 为混音准备音频数据 - 检查是否有调整后的文件
        final_audio_segments = []
        for audio_file_path, time_ms, original_audio in processed_audio_segments:
            # 检查是否有对应的调整后文件
            adjusted_file = audio_file_path.replace('.mp3', '_speed.mp3')
            if os.path.exists(adjusted_file):
                # 使用调整后的音频文件
                try:
                    adjusted_audio = AudioSegment.from_file(adjusted_file)
                    final_audio_segments.append((adjusted_file, time_ms, adjusted_audio))
                    print(f"使用调整后的音频: {os.path.basename(adjusted_file)}, 时长={len(adjusted_audio)}ms")
                except Exception as e:
                    print(f"加载调整后的音频失败 {adjusted_file}: {e}, 使用原始音频")
                    final_audio_segments.append((audio_file_path, time_ms, original_audio))
            else:
                # 使用原始音频
                final_audio_segments.append((audio_file_path, time_ms, original_audio))
                print(f"使用原始音频: {os.path.basename(audio_file_path)}, 时长={len(original_audio)}ms")

        print(f"最终音频段数: {len(final_audio_segments)}")

        # 计算总时长
        last_path, last_ms, last_audio = final_audio_segments[-1]
        print(f"最后片段: {last_path}, 时间={last_ms}ms, 时长={len(last_audio)}ms")
        total_ms = last_ms + len(last_audio) + 1000
        total_samples = int(total_ms * SR / 1000)

        # 创建共享内存缓冲区
        shm = shared_memory.SharedMemory(create=True, size=total_samples * N_CH * 4)
        buf = np.ndarray((total_samples * N_CH,), dtype=np.float32, buffer=shm.buf)
        buf[:] = 0.0

        # 混合所有音频段
        for audio_file_path, start_ms, audio_segment in final_audio_segments:
            samples = to_int16_samples(audio_segment).astype(np.float32)
            start_sample = int(start_ms * SR / 1000)
            end_sample = start_sample + len(samples)
            if end_sample > len(buf):
                end_sample = len(buf)  # 防止越界
            buf[start_sample:end_sample] += samples
            print(f"混音片段: {os.path.basename(audio_file_path)}, 起始={start_sample}, 结束={end_sample}")

        np.clip(buf, -32768, 32767, out=buf)
        out_bytes = buf.astype(np.int16).tobytes()
        shm.close()
        shm.unlink()

        final_audio = AudioSegment(data=out_bytes, sample_width=WIDTH, frame_rate=SR, channels=N_CH)
        final_audio.export(output_mp3_path, format="mp3")
        print(f"最终音频已保存: {output_mp3_path}")

        # 清理临时文件
        for fp, _ in audio_files:
            if os.path.exists(fp):
                os.remove(fp)

        # 清理调整后的临时文件
        for audio_file_path, _, _ in processed_audio_segments:
            speed_file = audio_file_path.replace('.mp3', '_speed.mp3')
            if os.path.exists(speed_file):
                os.remove(speed_file)
                print(f"清理临时文件: {os.path.basename(speed_file)}")

        return output_mp3_path

    return None

def parse_timestamp(timestamp):
    match = re.match(r'[\(（](?:(\d{1,2}):)?(\d{1,3}):(\d{1,2})(?:\.(\d{1,3}))?[\)）]', timestamp)
    if match:
        hours, minutes, seconds, milliseconds = match.groups()
        total_ms = 0
        if hours:
            total_ms += int(hours) * 3600 * 1000
        total_ms += int(minutes) * 60 * 1000
        total_ms += int(seconds) * 1000
        if milliseconds:
            total_ms += int(milliseconds.ljust(3, '0'))
        return total_ms
    return 0

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

tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "0️🚀 一键工作流",
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
        <h1 style="text-align:center; margin-bottom:1rem;">🚀 一键工作流</h1>
        <p style="text-align:center; opacity:0.9;">全自动完成从YouTube到B站的视频搬运</p>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("---")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        workflow_url = st.text_input("YouTube视频URL", placeholder="https://www.youtube.com/watch?v=...", key="workflow_url")
    with col2:
        auto_upload = st.checkbox("自动上传到B站", value=True, help="勾选后完成所有步骤会自动上传，否则只处理到封面")
    
    st.markdown("---")
    
    progress_container = st.container()
    
    if st.button("🚀 开始一键工作流", type="primary", use_container_width=True):
        if not workflow_url:
            st.error("请输入YouTube视频URL")
        else:
            # 清空temp目录
            clear_temp_directory()

            status_container = st.container()
            
            steps_status = {
                "下载字幕": {"status": "pending", "message": ""},
                "翻译标题": {"status": "pending", "message": ""},
                "翻译字幕": {"status": "pending", "message": ""},
                "转语音": {"status": "pending", "message": ""},
                "下载视频": {"status": "pending", "message": ""},
                "处理封面": {"status": "pending", "message": ""},
                "上传B站": {"status": "pending", "message": ""}
            }
            
            def update_step_status(step_name, status, message=""):
                steps_status[step_name]["status"] = status
                steps_status[step_name]["message"] = message
                
                status_dict = {
                    "pending": "⏳",
                    "running": "🔄",
                    "success": "✅",
                    "error": "❌"
                }
                
                step_class = {
                    "pending": "step-card",
                    "running": "step-card step-running",
                    "success": "step-card step-success",
                    "error": "step-card step-error"
                }
                
                return status_dict[status], step_class[status]
            
            def retry_with_backoff(func, max_retries=3, step_name="操作"):
                for attempt in range(max_retries):
                    try:
                        return func()
                    except Exception as e:
                        if attempt < max_retries - 1:
                            delay = 2 ** attempt
                            current_attempt = attempt + 1
                            retry_msg = f"{step_name}失败，{delay}秒后重试 ({current_attempt}/{max_retries}): {str(e)}"
                            st.warning(retry_msg)
                            time.sleep(delay)
                        else:
                            raise e
            
            try:
                subtitles_dir = os.path.join(TEMP_DIR, "subtitles")
                os.makedirs(subtitles_dir, exist_ok=True)
                
                with status_container:
                    st.markdown("## 📋 工作流进度")
                    
                    icon1, class1 = update_step_status("下载字幕", "running")
                    st.markdown(f"""
                    <div class="{class1}">
                        <strong>{icon1} 步骤1: 下载字幕</strong><br/>
                        <span id="msg1"></span>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    def step1_download_subtitles():
                        cookies_file_path = None
                        if YT_COOKIES.strip():
                            cookies_file_path = os.path.join(TEMP_DIR, "youtube_cookies.txt")
                            with open(cookies_file_path, 'w', encoding='utf-8') as f:
                                f.write(YT_COOKIES.strip())
                        
                        ydl_opts = {
                            'writeautomaticsub': True,
                            'skip_download': True,
                            'subtitleslangs': ['en'],
                            'quiet': True,
                            'outtmpl': os.path.join(subtitles_dir, '%(title)s.%(ext)s')
                        }
                        
                        if cookies_file_path:
                            ydl_opts['cookiefile'] = cookies_file_path
                        
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([workflow_url])
                        
                        vtt_files = list(Path(subtitles_dir).glob("*.vtt"))
                        if vtt_files:
                            original_file = vtt_files[0]
                            new_file = os.path.join(subtitles_dir, "word_level.vtt")
                            os.rename(original_file, new_file)
                            return new_file
                        return None
                    
                    vtt_file_path = retry_with_backoff(step1_download_subtitles, max_retries=3, step_name="下载字幕")
                    
                    icon1, class1 = update_step_status("下载字幕", "success", f"成功: {vtt_file_path}")
                    st.markdown(f"""
                    <div class="{class1}">
                        <strong>{icon1} 步骤1: 下载字幕</strong><br/>
                        {vtt_file_path}
                    </div>
                    """, unsafe_allow_html=True)
                    
                    icon2, class2 = update_step_status("翻译标题", "running")
                    st.markdown(f"""
                    <div class="{class2}">
                        <strong>{icon2} 步骤2: 翻译标题</strong><br/>
                        <span id="msg2"></span>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    def step2_translate_title():
                        ydl_info_opts = {
                            'skip_download': True,
                            'quiet': True,
                        }
                        
                        cookies_file_path = None
                        if YT_COOKIES.strip():
                            cookies_file_path = os.path.join(TEMP_DIR, "youtube_cookies.txt")
                        
                        if cookies_file_path and os.path.exists(cookies_file_path):
                            ydl_info_opts['cookiefile'] = cookies_file_path
                        
                        with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
                            info_dict = ydl.extract_info(workflow_url, download=False)
                            original_title = info_dict.get('title', '')
                        
                        if not original_title:
                            raise Exception("无法获取视频标题")
                        
                        SYSTEM_PROMPT = """你是爆款视频up主，将英文标题翻译成吸引眼球的爆款视频中文标题，直接输出翻译结果，不要解释。"""
                        
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
                        # 限制tags数量不超过10个
                        tags_list = tags_list[:10]

                        upload_config_file = os.path.join(subtitles_dir, "upload_config.pkl")
                        upload_data = {
                            'title_desc': f'(中配){translated_title}',
                            'tags': tags_list
                        }
                        
                        with open(upload_config_file, 'wb') as f:
                            pickle.dump(upload_data, f)
                        
                        return translated_title, tags_list
                    
                    translated_title, tags_list = retry_with_backoff(step2_translate_title, max_retries=3, step_name="翻译标题")
                    
                    icon2, class2 = update_step_status("翻译标题", "success", f"标题: {translated_title}")
                    st.markdown(f"""
                    <div class="{class2}">
                        <strong>{icon2} 步骤2: 翻译标题</strong><br/>
                        {translated_title}<br/>
                        标签: {', '.join(tags_list)}
                    </div>
                    """, unsafe_allow_html=True)
                    
                    icon3, class3 = update_step_status("翻译字幕", "running")
                    st.markdown(f"""
                    <div class="{class3}">
                        <strong>{icon3} 步骤3: 翻译字幕</strong><br/>
                        <span id="msg3"></span>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    def step3_translate_subtitles():
                        # 直接调用单步执行的翻译字幕逻辑
                        return translate_subtitles_from_vtt(vtt_file_path)
                    
                    txt_file_path = retry_with_backoff(step3_translate_subtitles, max_retries=3, step_name="翻译字幕")
                    
                    icon3, class3 = update_step_status("翻译字幕", "success", f"保存到: {txt_file_path}")
                    st.markdown(f"""
                    <div class="{class3}">
                        <strong>{icon3} 步骤3: 翻译字幕</strong><br/>
                        {txt_file_path}
                    </div>
                    """, unsafe_allow_html=True)
                    
                    icon4, class4 = update_step_status("转语音", "running")
                    st.markdown(f"""
                    <div class="{class4}">
                        <strong>{icon4} 步骤4: 转语音</strong><br/>
                        <span id="msg4"></span>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    def step4_tts():
                        output_mp3 = os.path.join(subtitles_dir, os.path.splitext(os.path.basename(vtt_file_path))[0] + "_translated.mp3")
                        result = process_tts_with_speed_adjustment(txt_file_path, output_mp3, subtitles_dir)
                        return result
                    
                    mp3_file_path = retry_with_backoff(step4_tts, max_retries=3, step_name="转语音")
                    
                    icon4, class4 = update_step_status("转语音", "success", f"保存到: {mp3_file_path}")
                    st.markdown(f"""
                    <div class="{class4}">
                        <strong>{icon4} 步骤4: 转语音</strong><br/>
                        {mp3_file_path}
                    </div>
                    """, unsafe_allow_html=True)
                    
                    icon5, class5 = update_step_status("下载视频", "running")
                    st.markdown(f"""
                    <div class="{class5}">
                        <strong>{icon5} 步骤5: 下载视频</strong><br/>
                        <span id="msg5"></span>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    def step5_download_video():
                        downloaded_video_base_name = os.path.join(TEMP_DIR, "subtitles", "downloaded_video")
                        
                        ydl_opts_video_only = {
                            'format': 'best',
                            'outtmpl': f'{downloaded_video_base_name}.%(ext)s',
                            'noplaylist': True,
                        }
                        
                        cookies_file_path = None
                        if YT_COOKIES.strip():
                            cookies_file_path = os.path.join(TEMP_DIR, "youtube_cookies.txt")
                        
                        if cookies_file_path:
                            ydl_opts_video_only['cookiefile'] = cookies_file_path
                        
                        with yt_dlp.YoutubeDL(ydl_opts_video_only) as ydl:
                            ydl.extract_info(workflow_url, download=True)
                        
                        downloaded_files = glob.glob(f"{downloaded_video_base_name}.*")
                        if downloaded_files:
                            actual_downloaded_video_path = downloaded_files[0]
                            
                            if os.path.exists(mp3_file_path):
                                final_video_path = os.path.splitext(mp3_file_path)[0] + ".mp4"
                                subprocess.run(['ffmpeg', '-y', '-i', actual_downloaded_video_path, '-i', mp3_file_path,
                                                    '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v:0', '-map', '1:a:0',
                                                    final_video_path], check=True, capture_output=True, text=True)
                                
                                if os.path.exists(actual_downloaded_video_path):
                                    os.remove(actual_downloaded_video_path)
                                
                                return final_video_path
                        
                        raise FileNotFoundError("视频下载失败")
                    
                    final_video_path = retry_with_backoff(step5_download_video, max_retries=3, step_name="下载视频")
                    
                    icon5, class5 = update_step_status("下载视频", "success", f"保存到: {final_video_path}")
                    st.markdown(f"""
                    <div class="{class5}">
                        <strong>{icon5} 步骤5: 下载视频</strong><br/>
                        {final_video_path}
                    </div>
                    """, unsafe_allow_html=True)
                    
                    icon6, class6 = update_step_status("处理封面", "running")
                    st.markdown(f"""
                    <div class="{class6}">
                        <strong>{icon6} 步骤6: 处理封面</strong><br/>
                        <span id="msg6"></span>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    def step6_process_cover():
                        ydl_opts_thumbnail = {
                            'skip_download': True,
                            'writethumbnail': True,
                            'outtmpl': os.path.join(TEMP_DIR, "subtitles", 'cover.%(ext)s'),
                            'noplaylist': True,
                        }
                        
                        cookies_file_path = None
                        if YT_COOKIES.strip():
                            cookies_file_path = os.path.join(TEMP_DIR, "youtube_cookies.txt")
                        
                        if cookies_file_path:
                            ydl_opts_thumbnail['cookiefile'] = cookies_file_path
                        
                        with yt_dlp.YoutubeDL(ydl_opts_thumbnail) as ydl:
                            ydl.extract_info(workflow_url, download=True)
                        
                        input_path = os.path.join(TEMP_DIR, "subtitles", "cover.webp")
                        output_path = os.path.join(TEMP_DIR, "subtitles", "cover.jpeg")
                        
                        if not os.path.exists(input_path):
                            input_files = list(Path(os.path.join(TEMP_DIR, "subtitles")).glob("cover.*"))
                            if input_files:
                                input_path = input_files[0]
                        
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
                        
                        return output_path
                    
                    cover_file_path = retry_with_backoff(step6_process_cover, max_retries=3, step_name="处理封面")
                    
                    icon6, class6 = update_step_status("处理封面", "success", f"保存到: {cover_file_path}")
                    st.markdown(f"""
                    <div class="{class6}">
                        <strong>{icon6} 步骤6: 处理封面</strong><br/>
                        {cover_file_path}
                    </div>
                    """, unsafe_allow_html=True)
                    
                    st.success("🎉 工作流执行完成！所有文件已准备好")
                    
                    st.markdown("---")
                    st.markdown("## 📁 生成的文件")
                    st.markdown(f"""
                    - 字幕: {os.path.join(TEMP_DIR, 'subtitles', 'word_level.vtt')}
                    - 翻译文本: {txt_file_path}
                    - 配音: {mp3_file_path}
                    - 最终视频: {final_video_path}
                    - 封面: {cover_file_path}
                    """)
                    
                    if auto_upload:
                        icon7, class7 = update_step_status("上传B站", "running")
                        st.markdown(f"""
                        <div class="{class7}">
                            <strong>{icon7} 步骤7: 上传B站</strong><br/>
                            <span id="msg7"></span>
                        </div>
                        """, unsafe_allow_html=True)
                        
                        def step7_upload():
                            credential = Credential(
                                sessdata=BILI_SESSDATA,
                                bili_jct="bcd4ba0d9ab8a7b95485798ed8097d26"
                            )
                            
                            vu_meta = VideoMeta(
                                tid=130,
                                title=translated_title,
                                tags=tags_list,
                                desc=translated_title,
                                cover=cover_file_path,
                                no_reprint=True
                            )
                            
                            async def main_upload():
                                page = VideoUploaderPage(
                                    path=final_video_path,
                                    title=translated_title,
                                    description=translated_title,
                                )
                                
                                uploader = video_uploader.VideoUploader([page], vu_meta, credential, line=video_uploader.Lines.QN)
                                
                                @uploader.on("__ALL__")
                                async def ev(data):
                                    pass
                                
                                await uploader.start()
                            
                            asyncio.run(main_upload())
                            return True
                        
                        retry_with_backoff(step7_upload, max_retries=3, step_name="上传B站")
                        
                        icon7, class7 = update_step_status("上传B站", "success")
                        st.markdown(f"""
                        <div class="{class7}">
                            <strong>{icon7} 步骤7: 上传B站</strong><br/>
                            上传成功！
                        </div>
                        """, unsafe_allow_html=True)
                        
                        st.success("🎉 上传成功！视频已发布到B站！")
                    else:
                        st.info("💡 如需上传B站，请在左侧勾选'自动上传到B站'后重新运行工作流")
            
            except Exception as e:
                import traceback
                st.error(f"❌ 工作流执行失败: {str(e)}")
                st.markdown(f"""
                <div class="step-card step-error">
                    <strong>错误详情:</strong><br/>
                    {traceback.format_exc()}
                </div>
                """, unsafe_allow_html=True)

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
                    
                    ydl_opts = {
                        'writeautomaticsub': True,
                        'skip_download': True,
                        'subtitleslangs': ['en'],
                        'quiet': False,
                        'outtmpl': os.path.join(subtitles_dir, '%(title)s.%(ext)s')
                    }
                    
                    if cookies_file_path:
                        ydl_opts['cookiefile'] = cookies_file_path
                    
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([youtube_url])
                    
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
                    
                    ydl_info_opts = {
                        'skip_download': True,
                        'quiet': True,
                    }
                    if cookies_file_path:
                        ydl_info_opts['cookiefile'] = cookies_file_path
                    
                    with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
                        info_dict = ydl.extract_info(youtube_url, download=False)
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
                        
                        ydl_opts_video_only = {
                            'format': 'best',
                            'outtmpl': f'{downloaded_video_base_name}.%(ext)s',
                            'noplaylist': True,
                        }
                        
                        if cookies_file_path:
                            ydl_opts_video_only['cookiefile'] = cookies_file_path
                        
                        with yt_dlp.YoutubeDL(ydl_opts_video_only) as ydl:
                            ydl.extract_info(youtube_url, download=True)
                        
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
                        
                        ydl_opts_thumbnail = {
                            'skip_download': True,
                            'writethumbnail': True,
                            'outtmpl': os.path.join(temp_dir, "subtitles", 'cover.%(ext)s'),
                            'noplaylist': True,
                        }
                        
                        if cookies_file_path:
                            ydl_opts_thumbnail['cookiefile'] = cookies_file_path
                        
                        with yt_dlp.YoutubeDL(ydl_opts_thumbnail) as ydl:
                            ydl.extract_info(youtube_url, download=True)
                        
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
