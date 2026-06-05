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

def translate_chunk(subtitle_text: str, llm_instance, terminology: dict, log_callback=None):
    """
    核心翻译函数，包含时间戳格式自动修复
    """
    # 1. 基础指令（彻底消除歧义，强调逐行1对1，禁止总结）
    base_instruction = """请将以下字幕文本准确翻译为中文。

【风格要求】
1. 译文必须通顺、易懂，高度符合中文日常口语表达习惯，多用常用词汇，避免生硬直译和机翻味。
2. 句子结构要自然流畅，符合字幕的阅读节奏。

【格式与结构约束】（极度重要）
1. 逐行严格对应：原文有多少行，译文就必须有多少行。必须保持原有的换行顺序，绝对禁止合并段落、禁止总结、禁止意译、禁止遗漏任何一行。
2. 时间戳强制保留：译文的【每一行】都必须以原文对应的时间戳开头，格式严格为 (HH:MM:SS.mmm)。绝对不可遗漏、修改或丢弃任何时间戳。
3. 标点极度注意：绝对禁止将毫秒前的小数点 "." 写成冒号 ":"（例如：严禁输出 (00:00:02:720)，必须输出 (00:00:02.720)）。
4. 直接输出翻译后的字幕内容，绝对不要包含“助手：”、“翻译结果：”等任何前缀或额外解释。"""

    # 2. 根据 terminology 变量动态构建 Prompt
    if terminology:
        term_lines = [f"{k} 翻译成 {v}" for k, v in terminology.items()]
        term_text = "\n".join(term_lines)
        prompt = f"""参考下面的专有名词翻译：
{term_text}

{base_instruction}

{subtitle_text}"""
    else:
        prompt = f"""{base_instruction}

{subtitle_text}"""

    # 3. 使用内置聊天接口
    try:
        response = llm_instance.create_chat_completion(
            messages=[
                {"role": "system", "content": "你是一个专业的字幕翻译专家。你的译文通顺自然、符合中文日常表达习惯，且能严格遵守时间与格式约束。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=4096,
            temperature=0.3,
            top_p=0.95,
            repeat_penalty=1.2,
            stop=["<|im_end|>", "user", "system", "assistant"]
        )
        result = response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        if log_callback:
            log_callback(f"⚠️ 聊天接口调用异常 ({e})，尝试使用基础补全模式...")
        else:
            print(f"⚠️ 聊天接口调用异常 ({e})，尝试使用基础补全模式...")
        prompt_template = f"SYSTEM: 你是一个专业的字幕翻译专家。\nUSER: {prompt}\nASSISTANT:\n"
        response = llm_instance(
            prompt=prompt_template, max_tokens=4096, temperature=0.3, top_p=0.95,
            repeat_penalty=1.2, stop=['USER:', 'SYSTEM:', 'ASSISTANT:'], echo=False
        )
        result = response["choices"][0]["text"].strip()

    # ================= 4. 强力代码兜底清洗与修复 =================

    # 修复 A: 自动修正时间戳中错误的冒号 (00:00:02:720) -> (00:00:02.720)
    result = re.sub(r'\((\d{2}:\d{2}:\d{2}):(\d{3})\)', r'(\1.\2)', result)

    # 修复 B: 切除可能出现的“助手：”、“翻译结果：”等废话前缀 (支持多行匹配)
    result = re.sub(r'^[\s]*(助手[：:]|翻译结果[：:]|翻译如下[：:]|AI[：:]|Assistant[：:])[ \t]*', '', result, flags=re.MULTILINE)

    # 修复 C: 清除可能产生的连续多余空行，保持字幕紧凑
    result = re.sub(r'\n{3,}', '\n\n', result)

    # 修复 D: 去除特定的标记和无用词
    for word in ["<|channel>", "thought", "<channel|>"]:
        result = result.replace(word, "")

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

    # 1. 读取原文本
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    if not lines:
        msg = "❌ 字幕文件为空或无有效内容！"
        if log_callback: log_callback(msg)
        else: print(msg)
        return

    # 2. 分片
    chunks = []
    for i in range(0, len(lines), chunk_size):
        chunks.append(lines[i:i+chunk_size])

    msg_init = f"✅ 共读取到 {len(lines)} 行有效字幕，已分为 {len(chunks)} 片（每片最多 {chunk_size} 行）。"
    if terminology:
        msg_init += f"\n📚 已启用 Terminology 模式，加载了 {len(terminology)} 个专属术语。\n"
    else:
        msg_init += f"\n📖 使用 Default Translation 模式。\n"

    if log_callback: log_callback(msg_init)
    else: print(msg_init)

    # 3. 加载模型
    msg_load = f"正在加载本地模型 {model_path}，请稍候（首次加载可能需要较长时间）..."
    if log_callback: log_callback(msg_load)
    else: print(msg_load)

    # 实例化 Llama 模型
    llm = Llama(
        model_path=model_path,
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        verbose=False,
        chat_format="chatml"
    )

    msg_loaded = "模型加载完成，开始逐片翻译...\n"
    if log_callback: log_callback(msg_loaded)
    else: print(msg_loaded)

    translated_chunks = []

    try:
        # 逐片翻译
        for idx, chunk in enumerate(chunks):
            chunk_text = "\n".join(chunk)
            msg_progress = f"⏳ 正在翻译第 {idx + 1}/{len(chunks)} 片..."
            if log_callback: log_callback(msg_progress)
            else: print(msg_progress)

            max_retries = 3
            translated_text = ""

            for attempt in range(max_retries):
                try:
                    translated_text = translate_chunk(chunk_text, llm, terminology, log_callback)

                    # 验证行数是否匹配
                    trans_lines = [l for l in translated_text.split('\n') if l.strip()]
                    if len(trans_lines) < len(chunk) * 0.8:
                        warn_msg = f"⚠️ 警告：译文行数 ({len(trans_lines)}) 明显少于原文 ({len(chunk)})，可能发生截断，正在重试 ({attempt+1}/{max_retries})..."
                        if log_callback: log_callback(warn_msg)
                        else: print(warn_msg)
                        
                        if attempt == max_retries - 1:
                            err_msg = "❌ 重试失败，将保留当前部分翻译结果继续。"
                            if log_callback: log_callback(err_msg)
                            else: print(err_msg)
                        continue
                    break

                except Exception as e:
                    err_msg = f"❌ 发生异常: {e}，正在重试 ({attempt+1}/{max_retries})...\n{traceback.format_exc()}"
                    if log_callback: log_callback(err_msg)
                    else: print(err_msg)
                    if attempt == max_retries - 1:
                        raise e

            translated_chunks.append(translated_text)

            # 实时打印/记录当前片段的翻译结果
            msg_done = f"✅ 第 {idx + 1}/{len(chunks)} 片翻译完成！结果如下：\n" + "-" * 40 + "\n" + translated_text + "\n" + "-" * 40
            if log_callback: log_callback(msg_done)
            else:
                print(msg_done)
                print()

        # 4. 合成并写入文件
        final_result = "\n".join(translated_chunks)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(final_result)

        msg_end = f"\n🎉 全部翻译完成！最终文件已保存至: {output_path}"
        if log_callback: log_callback(msg_end)
        else: print(msg_end)

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
