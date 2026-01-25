import asyncio
import edge_tts
import random
import re
import os
import subprocess

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
                print(f"第{retry_count}次重试，等待{delay:.2f}秒后继续...", flush=True)
                await asyncio.sleep(delay)
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_file)
            print(f"[TTS-DEBUG] 成功保存音频: {output_file}", flush=True)
            return  # 成功则退出循环
        except Exception as e:
            error_msg = str(e).lower()
            retry_count += 1
            # 检查是否是503错误或其他可重试的错误
            if "503" in error_msg or "timeout" in error_msg or "connection" in error_msg:
                if retry_count <= max_retries:
                    print(f"遇到API错误: {e}，准备第{retry_count}次重试...", flush=True)
                else:
                    print(f"达到最大重试次数({max_retries})，无法完成转换: {e}", flush=True)
                    raise  # 达到最大重试次数，抛出异常
            else:
                # 其他类型的错误直接抛出
                print(f"遇到非重试类型的错误: {e}", flush=True)
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

def process_segment(task):
    """
    处理单个文本段落的函数，用于多进程处理
    """
    i, timestamp, txt, temp_dir, voice = task
    try:
        cleaned_timestamp = re.sub(r'[^\w\d]+', '_', timestamp)
        file_name = f"{cleaned_timestamp}.mp3"
        output_file = os.path.join(temp_dir, file_name)

        print(f"进程正在处理段落 {i+1}: {timestamp} - {txt[:30]}... [PID: {os.getpid()}]", flush=True)
        run_text_to_speech(txt, output_file, voice)
        print(f"段落 {i+1} 处理完成", flush=True)

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
