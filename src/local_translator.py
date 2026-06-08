# -*- coding: utf-8 -*-
import os
import re
import traceback

# Optional imports, check them dynamically
HAS_DEPENDENCIES = False
DEP_ERROR = ""
try:
    from huggingface_hub import hf_hub_download
    from llama_cpp import Llama
    HAS_DEPENDENCIES = True
except ImportError as e:
    DEP_ERROR = str(e)

def check_dependencies():
    """
    检查本地模型所需的依赖项是否安装
    """
    if not HAS_DEPENDENCIES:
        return False, (
            f"缺少依赖项: {DEP_ERROR}。\n"
            "要使用本地模型翻译功能，请先安装 huggingface_hub 和 llama-cpp-python。\n"
            "CPU 环境安装命令:\n"
            "pip install huggingface_hub llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu\n"
            "GPU (CUDA) 环境安装命令:\n"
            "pip install huggingface_hub\n"
            "pip install llama_cpp_python-0.3.25-py3-none-linux_x86_64.whl (Linux/Colab) 或根据您的系统平台编译安装。"
        )
    return True, ""

def download_model(repo_id="tencent/Hy-MT2-1.8B-GGUF", filename="Hy-MT2-1.8B-Q4_K_M.gguf", log_callback=None):
    """
    从 Hugging Face Hub 下载 GGUF 模型
    """
    is_ok, err = check_dependencies()
    if not is_ok:
        raise ImportError(err)

    if log_callback:
        log_callback(f"正在从 Hugging Face Hub ({repo_id}/{filename}) 下载模型，这可能需要一些时间...")
    else:
        print(f"正在下载模型 {repo_id}/{filename} ...")

    model_path = hf_hub_download(repo_id=repo_id, filename=filename)
    
    if log_callback:
        log_callback(f"模型下载成功，本地路径: {model_path}")
    else:
        print(f"模型下载成功，本地路径: {model_path}")
        
    return model_path

def translate_chunk(subtitle_text: str, llm_instance, terminology: dict, use_chat_completion: bool, log_callback=None):
    """
    核心翻译函数：采用极简强硬指令，杜绝模型“聊天”和幻觉
    """
    # 1. 构建术语文本
    term_text = ""
    if terminology:
        term_lines = [f"- {k} 必须翻译为 {v}" for k, v in terminology.items()]
        term_text = "\n".join(term_lines) + "\n"

    if use_chat_completion:
        # Construct messages for chat completion
        system_content = "你是一个严格的字幕翻译引擎。你的唯一任务是翻译，绝对不要输出任何对话、问候、确认（如“好的”、“我明白了”）或解释性文字。\n\n【绝对规则】\n1. 逐行对应，强制保持行数一致：原文几行，译文就几行。禁止合并、总结或遗漏。\n2. 时间戳：每一行必须以精确的 (HH:MM:SS.mmm) 格式开头。严禁修改时间戳的数字位数或标点（严禁将 . 写成 :）。\n3. 风格：中文口语化，通顺易懂。\n" + term_text
        user_content = f"【待翻译文本】\n\n{subtitle_text}\n\n【翻译结果】："

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content}
        ]

        try:
            response = llm_instance.create_chat_completion(
                messages=messages,
                max_tokens=4096,
                temperature=0.2,       # 进一步降低温度，减少胡言乱语的概率
                top_p=0.9,
                repeat_penalty=1.15,
                stop=["<|im_end|>", "<|im_start|>"], # Stop tokens for chat format
            )
            result = response["choices"][0]["message"]["content"].strip()
        except Exception as e:
            msg = f"⚠️ 翻译异常 (Chat Completion): {e}"
            if log_callback:
                log_callback(msg)
            else:
                print(msg)
            return ""
    else: # Use direct prompt completion for Hy-MT models
        # 2. 极简、强硬的指令 Prompt (Instruct 风格，杜绝对话感)
        prompt = f"""你是一个严格的字幕翻译引擎。你的唯一任务是翻译，绝对不要输出任何对话、问候、确认（如“好的”、“我明白了”）或解释性文字。

【绝对规则】
1. 逐行对应，强制保持行数一致：原文几行，译文就几行。禁止合并、总结或遗漏。
2. 时间戳：每一行必须以精确的 (HH:MM:SS.mmm) 格式开头。严禁修改时间戳的数字位数或标点（严禁将 . 写成 :）。
3. 风格：中文口语化，通顺易懂。
4. {term_text}
【待翻译文本】

{subtitle_text}

【翻译结果】（直接从第一行时间戳开始输出，不要有任何前缀）：
"""
        try:
            response = llm_instance(
                prompt=prompt,
                max_tokens=4096,
                temperature=0.2,       # 进一步降低温度，减少胡言乱语的概率
                top_p=0.9,
                repeat_penalty=1.15,
                stop=["<|im_end|>", "<|im_start|>", "USER:", "Assistant:"],
                echo=False
            )
            result = response["choices"][0]["text"].strip()
        except Exception as e:
            msg = f"⚠️ 翻译异常 (Direct Completion): {e}"
            if log_callback:
                log_callback(msg)
            else:
                print(msg)
            return ""

    # ================= 4. 强力代码兜底清洗 (针对你遇到的混乱情况) =================

    # 修复 A: 清除所有 ChatML 泄漏标记和模型自我确认的废话
    result = re.sub(r'<\|im_end\|>|<\|im_start\|>', '', result)
    result = re.sub(r'^[\s]*(我已了解|我已完全理解|好的|明白|Assistant:|翻译结果:)[^\n]*\n*', '', result, flags=re.MULTILINE | re.IGNORECASE)

    # 修复 B: 自动修正时间戳中错误的冒号和位数错乱 (例如 000:10:43.120 -> 00:10:43.120)
    result = re.sub(r'\((\d+):(\d+):(\d+)[:.](\d+)\)',
                    lambda m: f"({int(m.group(1)):02d}:{int(m.group(2)):02d}:{int(m.group(3)):02d}.{m.group(4)})",
                    result)

    # 修复 C: 过滤掉所有【不以标准时间戳开头】的无效行（彻底消灭模型的胡言乱语行）
    valid_lines = []
    timestamp_pattern = re.compile(r'^\(\d{2}:\d{2}:\d{2}\.\d{3}\)')
    for line in result.split('\n'):
        line = line.strip()
        if not line:
            continue
        if timestamp_pattern.match(line):
            valid_lines.append(line)

    result = "\n".join(valid_lines)

    return result

def translate_subtitle_file(input_path: str, output_path: str, model_path: str, chunk_size: int = 10, terminology: dict = None, n_ctx: int = 8192, n_gpu_layers: int = -1, log_callback=None):
    """
    读取文件、分片、逐片翻译并合成保存
    """
    is_ok, err = check_dependencies()
    if not is_ok:
        raise ImportError(err)

    if terminology is None:
        terminology = {}

    with open(input_path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    if not lines:
        msg = "❌ 字幕文件为空或无有效内容！"
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
        return

    use_chat_completion = "Hy-MT" not in model_path

    # Initial chunks (can be modified if subdivision occurs)
    chunks = []
    for i in range(0, len(lines), chunk_size):
        chunks.append(lines[i:i+chunk_size])

    msg_init = f"✅ 共读取到 {len(lines)} 行有效字幕，已分为 {len(chunks)} 片（每片最多 {chunk_size} 行）。"
    if terminology:
        msg_init += f"\n📚 已启用 Terminology 模式，加载了 {len(terminology)} 个专属术语。\n"
    else:
        msg_init += f"\n📖 使用 Default Translation 模式。\n"

    if log_callback:
        log_callback(msg_init)
    else:
        print(msg_init)

    # 加载模型
    msg_load = f"正在加载本地模型 {model_path}，请稍候（首次加载可能需要较长时间）..."
    if log_callback:
        log_callback(msg_load)
    else:
        print(msg_load)

    # 实例化 Llama 模型
    llm = Llama(
        model_path=model_path,
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        verbose=False,
        
    )

    msg_loaded = "模型加载完成，开始逐片翻译...\n"
    if log_callback:
        log_callback(msg_loaded)
    else:
        print(msg_loaded)

    translated_chunks = []
    processed_chunk_index = 0 # This will track our progress through the (potentially dynamic) chunks list

    try:
        while processed_chunk_index < len(chunks):
            current_chunk = chunks[processed_chunk_index]
            chunk_text = "\n".join(current_chunk)

            msg_progress = f"⏳ 正在翻译第 {processed_chunk_index + 1}/{len(chunks)} 片 (原始行数: {len(current_chunk)})... "
            if log_callback:
                log_callback(msg_progress)
            else:
                print(msg_progress)

            translated_text_for_this_chunk = ""
            success = False
            max_retries = 3 # Define max retries for a single chunk
            mid_point = 0

            for attempt in range(max_retries):
                try:
                    translated_text_for_this_chunk = translate_chunk(chunk_text, llm, terminology, use_chat_completion, log_callback)
                    trans_lines = [l for l in translated_text_for_this_chunk.split('\n') if l.strip()]

                    if len(trans_lines) == len(current_chunk):
                        success = True
                        msg_success = f"✅ 第 {processed_chunk_index + 1}/{len(chunks)} 片翻译成功！"
                        if log_callback:
                            log_callback(msg_success)
                        else:
                            print(msg_success)
                        break # Successfully translated this chunk

                    else:
                        warn_msg = f"\n⚠️ 警告：有效译文行数 ({len(trans_lines)}) 与原文 ({len(current_chunk)}) 不一致，模型可能产生了幻觉或遗漏，正在重试 ({attempt+1}/{max_retries})..."
                        if log_callback:
                            log_callback(warn_msg)
                        else:
                            print(warn_msg)
                        
                        dbg_msg = (
                            "--- 原始提交给模型的内容 ---\n" + chunk_text + "\n" +
                            "--- 模型返回的翻译结果 ---\n" + translated_text_for_this_chunk + "\n" +
                            f"--- 失败分析：原始行数 {len(current_chunk)}，模型返回有效行数 {len(trans_lines)} ---"
                        )
                        if log_callback:
                            log_callback(dbg_msg)
                        else:
                            print(dbg_msg)

                        if attempt == max_retries - 1: # Last attempt failed for this chunk
                            fail_msg = f"❌ 第 {processed_chunk_index + 1}/{len(chunks)} 片经过 {max_retries} 次重试后仍然失败。尝试拆分..."
                            if log_callback:
                                log_callback(fail_msg)
                            else:
                                print(fail_msg)

                            mid_point = len(current_chunk) // 2
                            if mid_point == 0 or len(current_chunk) == 1: # Cannot subdivide further
                                fail_warn = f"❌ 警告：无法进一步拆分只有1行的失败片段。将保留当前结果。"
                                if log_callback:
                                    log_callback(fail_warn)
                                else:
                                    print(fail_warn)
                                # Append the best (last) failed attempt's result and move on
                                translated_chunks.append(translated_text_for_this_chunk)
                                processed_chunk_index += 1
                                success = True # Treat as 'processed' to move to next logical chunk
                                break # Exit retry loop
                            else:
                                first_half = current_chunk[:mid_point]
                                second_half = current_chunk[mid_point:]

                                # Replace the current failing chunk with two smaller chunks at its position
                                chunks[processed_chunk_index:processed_chunk_index+1] = [first_half, second_half]
                                split_msg = f"  已将片段拆分为两部分 (大小: {len(first_half)}, {len(second_half)})，将重新处理。"
                                if log_callback:
                                    log_callback(split_msg)
                                else:
                                    print(split_msg)
                                success = False # The overall 'current_chunk' failed, will re-attempt smaller ones
                                break # Exit retry loop, and the while loop will pick up the new chunks

                except Exception as e:
                    err_msg = f"❌ 发生异常: {e}，正在重试 ({attempt+1}/{max_retries})...\n{traceback.format_exc()}"
                    if log_callback:
                        log_callback(err_msg)
                    else:
                        print(err_msg)
                    if attempt == max_retries - 1:
                        raise e

            if success and len(trans_lines) == len(current_chunk): # Successfully translated and line count matches
                 translated_chunks.append(translated_text_for_this_chunk)
                 processed_chunk_index += 1
            elif success and (mid_point == 0 or len(current_chunk) == 1):
                 pass

        final_result = "\n".join(translated_chunks)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(final_result)

        msg_end = f"\n🎉 全部翻译完成！最终文件已保存至: {output_path}"
        if log_callback:
            log_callback(msg_end)
        else:
            print(msg_end)

    finally:
        # 清理并关闭 Llama 实例以释放内存/显存
        if hasattr(llm, 'close'):
            try:
                llm.close()
            except Exception:
                pass
        del llm

def translate_title_and_tags_local(original_title: str, model_path: str, n_ctx: int = 4096, n_gpu_layers: int = -1, log_callback=None):
    """
    使用本地 Llama 模型翻译视频标题并生成视频标签
    """
    is_ok, err = check_dependencies()
    if not is_ok:
        raise ImportError(err)

    if log_callback:
        log_callback("正在加载本地模型以翻译标题和生成标签...")
    else:
        print("正在加载本地模型以翻译标题和生成标签...")
    
    llm = Llama(
        model_path=model_path,
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        verbose=False,
        chat_format="chatml"
    )
    
    try:
        # 1. 翻译标题
        if log_callback:
            log_callback(f"正在本地翻译标题: '{original_title}' ...")
        else:
            print(f"正在本地翻译标题: '{original_title}' ...")
        
        system_prompt = "你是一个专业的中英文翻译官。"
        user_prompt = f"请将以下英文视频标题准确地翻译为中文，直接输出翻译好的中文标题，绝对不要包含任何前缀、解释、标点引号或额外英文文字：\n{original_title}"
        
        try:
            response = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=512,
                temperature=0.3,
                top_p=0.95,
                repeat_penalty=1.2,
                stop=["<|im_end|>", "user", "system", "assistant"]
            )
            translated_title = response["choices"][0]["message"]["content"].replace('**', '').replace('"', '').replace('“', '').replace('”', '').strip()
        except Exception as e:
            if log_callback:
                log_callback(f"⚠️ 聊天接口调用异常 ({e})，尝试使用基础补全模式...")
            prompt_template = f"<|im_start|>system\n{system_prompt}<|im_end|>\n<|im_start|>user\n{user_prompt}<|im_end|>\n<|im_start|>assistant\n"
            response = llm(
                prompt=prompt_template, max_tokens=512, temperature=0.3, top_p=0.95,
                repeat_penalty=1.2, stop=["<|im_end|>", 'USER:', 'SYSTEM:', 'ASSISTANT:'], echo=False
            )
            translated_title = response["choices"][0]["text"].replace('**', '').replace('"', '').replace('“', '').replace('”', '').strip()
            
        if log_callback:
            log_callback(f"本地翻译标题结果: '{translated_title}'")
        else:
            print(f"本地翻译标题结果: '{translated_title}'")

        # 2. 生成标签
        if log_callback:
            log_callback("正在本地生成视频标签...")
        else:
            print("正在本地生成视频标签...")
            
        system_prompt_tags = "你是一个专业的视频运营助手。"
        tags_prompt = f"请根据以下中文视频标题，提取或生成5到8个适合的视频分类标签，并用英文逗号分隔输出（只输出标签，绝对不要包含任何前缀、序号或多余解释）：\n标题：{translated_title}"
        
        try:
            response_tags = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt_tags},
                    {"role": "user", "content": tags_prompt}
                ],
                max_tokens=512,
                temperature=0.3,
                top_p=0.95,
                repeat_penalty=1.2,
                stop=["<|im_end|>", "user", "system", "assistant"]
            )
            tags_str = response_tags["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if log_callback:
                log_callback(f"⚠️ 聊天接口调用异常 ({e})，尝试使用基础补全模式...")
            prompt_template = f"<|im_start|>system\n{system_prompt_tags}<|im_end|>\n<|im_start|>user\n{tags_prompt}<|im_end|>\n<|im_start|>assistant\n"
            response_tags = llm(
                prompt=prompt_template, max_tokens=512, temperature=0.3, top_p=0.95,
                repeat_penalty=1.2, stop=["<|im_end|>", 'USER:', 'SYSTEM:', 'ASSISTANT:'], echo=False
            )
            tags_str = response_tags["choices"][0]["text"].strip()
            
        tags_list = [t.strip() for t in tags_str.replace('，', ',').split(',') if t.strip()]
        
        # 兜底防御性逻辑：针对无法进行任务指令微调的纯翻译模型进行关键字提取
        if not tags_list or len(tags_list) < 2 or any(len(t) > 15 for t in tags_list) or any("根据" in t for t in tags_list):
            if log_callback:
                log_callback("⚠️ 检测到本地模型无法合理生成标签，自动从中文标题提取关键词作为视频标签...")
            else:
                print("⚠️ 检测到本地模型无法合理生成标签，自动从中文标题提取关键词作为视频标签...")
            
            import re
            words = re.findall(r'[\u4e00-\u9fa5]{2,}', translated_title)
            if words:
                tags_list = list(set(words))[:6]
            else:
                tags_list = ["科技", "人工智能", "视频"]
                
        if log_callback:
            log_callback(f"本地生成标签结果: {tags_list}")
        else:
            print(f"本地生成标签结果: {tags_list}")
        
        return translated_title, tags_list

    finally:
        if hasattr(llm, 'close'):
            try:
                llm.close()
            except Exception:
                pass
        del llm
