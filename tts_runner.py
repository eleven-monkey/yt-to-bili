
import os
import re
import argparse
import asyncio
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pydub import AudioSegment
import edge_tts
import random
import subprocess

# --- Helper Functions from worker_utils.py (duplicated to be standalone) ---

async def text_to_speech(text, output_file, voice="zh-CN-XiaoxiaoNeural", max_retries=5):
    """
    将文本转换为语音并保存为音频文件
    添加重试机制和延迟，处理edge-tts API的503错误
    """
    retry_count = 0
    base_delay = 1
    while retry_count <= max_retries:
        try:
            if retry_count > 0:
                delay = base_delay * (2 ** (retry_count - 1)) + (random.random() * 0.5)
                print(f"第{retry_count}次重试，等待{delay:.2f}秒后继续...", flush=True)
                await asyncio.sleep(delay)
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_file)
            print(f"[TTS-DEBUG] 成功保存音频: {output_file}", flush=True)
            return
        except Exception as e:
            error_msg = str(e).lower()
            retry_count += 1
            if "503" in error_msg or "timeout" in error_msg or "connection" in error_msg:
                if retry_count <= max_retries:
                    print(f"遇到API错误: {e}，准备第{retry_count}次重试...", flush=True)
                else:
                    print(f"达到最大重试次数({max_retries})，无法完成转换: {e}", flush=True)
                    raise
            else:
                print(f"遇到非重试类型的错误: {e}", flush=True)
                raise

def run_text_to_speech(text, output_file, voice="zh-CN-XiaoxiaoNeural", max_retries=5):
    """
    在多进程中运行text_to_speech的包装函数
    """
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
        if os.path.exists(temp_output_processed):
            os.remove(temp_output_processed)
        return i, None, f"音频速度调整失败 {i+1}: {e}"

# --- Main Logic ---

def process_tts(txt_file_path, output_mp3_path, voice, max_workers, temp_dir_root):
    print("="*50, flush=True)
    print("开始TTS转换流程 (独立进程)", flush=True)
    print("="*50, flush=True)

    with open(txt_file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"txt_file_path: {txt_file_path}", flush=True)
    print(f"content长度: {len(content)} 字符", flush=True)

    pattern = r'[\\(（](\d{1,2})?:?(\d{1,3}):(\d{1,2})(?:\.(\d{1,3}))?[\\)）](.+?)(?=[\\(（](?:\d{1,2})?:?(\d{1,3}):(\d{1,2})(?:\.(\d{1,3}))?[\\)）]|$)'
    matches = list(re.finditer(pattern, content, re.DOTALL))
    print(f"匹配到的segments数量: {len(matches)}", flush=True)

    segments = []
    for match in matches:
        timestamp_string = match.group(0)
        content_text = match.group(5).strip()
        if content_text:
            timestamp_match = re.match(r'[\\(（](.+?)[\\)）]', timestamp_string)
            if timestamp_match:
                timestamp = timestamp_match.group(1)
                segments.append((timestamp, content_text))

    print(f"解析出的segments数量: {len(segments)}", flush=True)
    
    # Use dirname of output file as temp dir if not specified, or create a specific one
    temp_dir = os.path.join(temp_dir_root, "tts_temp")
    os.makedirs(temp_dir, exist_ok=True)

    tasks = []
    for i, (timestamp, txt) in enumerate(segments):
        tasks.append((i, timestamp, txt, temp_dir, voice))

    print(f"开始使用 {max_workers} 个进程处理TTS...", flush=True)
    
    audio_files = [None] * len(tasks)
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_segment, task) for task in tasks]

        for future in as_completed(futures):
            index, output_file, time_ms, error = future.result()
            if error:
                print(f"警告: {error}", flush=True)
            if output_file and os.path.exists(output_file):
                audio_files[index] = (output_file, time_ms)

    audio_files = [af for af in audio_files if af is not None]
    
    print(f"生成了 {len(audio_files)} 个音频文件", flush=True)
    audio_files.sort(key=lambda x: x[1])

    if audio_files:
        # 音频速度调整以避免重叠
        print("开始音频速度调整...", flush=True)

        processed_audio_segments = []
        for i, (audio_file_path, time_ms) in enumerate(audio_files):
            audio = AudioSegment.from_file(audio_file_path)
            processed_audio_segments.append((audio_file_path, time_ms, audio))

        speed_adjust_tasks_list = []
        
        for i, (audio_file_path, time_ms, audio) in enumerate(processed_audio_segments[:-1]):
            current_len = len(audio)
            end_time = time_ms + current_len

            if i + 1 < len(processed_audio_segments):
                next_start = processed_audio_segments[i+1][1]
                if end_time > next_start + 100:
                    target = next_start - time_ms - 50
                    if target > 100:
                        factor = min(current_len / target, 2.0)
                        print(f"片段{i}: 需要加速 因子={factor:.2f}", flush=True)
                        if factor > 1.0:
                            temp_speed_file = audio_file_path.replace('.mp3', '_speed.mp3')
                            audio.export(temp_speed_file, format="mp3")
                            speed_adjust_tasks_list.append((i, temp_speed_file, target, factor))

        if speed_adjust_tasks_list:
            print(f"开始处理 {len(speed_adjust_tasks_list)} 个音频速度调整任务...", flush=True)
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(adjust_audio_speed, task) for task in speed_adjust_tasks_list]
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        if result and len(result) >= 3:
                            i, adjusted_file_path, error = result
                            if error:
                                print(f"速度调整失败 {i}: {error}", flush=True)
                    except Exception as e:
                        print(f"音频速度调整任务失败: {e}", flush=True)

        # Final Mix
        print(f"开始混音 {len(processed_audio_segments)} 个音频片段", flush=True)
        final_audio_segments = []
        for audio_file_path, time_ms, original_audio in processed_audio_segments:
            adjusted_file = audio_file_path.replace('.mp3', '_speed.mp3')
            if os.path.exists(adjusted_file):
                try:
                    adjusted_audio = AudioSegment.from_file(adjusted_file)
                    final_audio_segments.append((adjusted_file, time_ms, adjusted_audio))
                except Exception as e:
                    final_audio_segments.append((audio_file_path, time_ms, original_audio))
            else:
                final_audio_segments.append((audio_file_path, time_ms, original_audio))

        combined_audio = AudioSegment.empty()
        current_pos = 0
        
        for i, (audio_file_path, start_ms, audio_segment) in enumerate(final_audio_segments):
            if start_ms > current_pos:
                silence_gap = start_ms - current_pos
                combined_audio += AudioSegment.silent(duration=silence_gap)
                current_pos += silence_gap
            combined_audio += audio_segment
            current_pos += len(audio_segment)
            
            if i % 10 == 0:
                print(f"已处理 {i+1}/{len(final_audio_segments)} 段", flush=True)

        combined_audio.export(output_mp3_path, format="mp3")
        print(f"最终音频已保存: {output_mp3_path}", flush=True)

        # Cleanup
        for fp, _ in audio_files:
            if os.path.exists(fp):
                try: os.remove(fp)
                except: pass
        
        for item in final_audio_segments:
            path = item[0]
            if path.endswith('_speed.mp3') and os.path.exists(path):
                 try: os.remove(path)
                 except: pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TTS Runner")
    parser.add_argument("--input", required=True, help="Input TXT file path")
    parser.add_argument("--output", required=True, help="Output MP3 file path")
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="TTS Voice")
    parser.add_argument("--workers", type=int, default=4, help="Max workers")
    parser.add_argument("--temp", default=".", help="Temp directory root")

    args = parser.parse_args()
    
    try:
        process_tts(args.input, args.output, args.voice, args.workers, args.temp)
    except Exception as e:
        print(f"TTS Process Error: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
