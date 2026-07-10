# -*- coding: utf-8 -*-
"""语速优化模块：合并语速过快的相邻字幕行，降低 TTS 朗读速度。

移植自 translate_subtitles.py 的 merge_fast_speaking_lines 机制。
核心思想：逐行计算语速（字数/时间间隔），对超阈值的行向相邻较慢行合并，
通过 while 循环级联吸收，直到无快句或无慢邻居可合并。
"""

import re

# 行格式：(HH:MM:SS.mmm) 文本  或  (MM:SS) 文本
LINE_PATTERN = re.compile(r'^[\(（]\s*(\d{1,3}:\d{2}(:\d{2})?(\.\d{1,3})?)\s*[\)）]\s*(.+)$')

# 语速阈值（字/分钟），超过则与相邻较慢行合并
MAX_SPEAKING_WPM = 440


def _extract_ts(line):
    """从合规行 '(ts) text' 中提取 ts 字符串部分（不含括号）。"""
    m = LINE_PATTERN.match(line.strip())
    if m:
        return m.group(1)
    return None


def _extract_text_after_ts(line):
    """从合规行 '(ts) text' 中提取 text 部分。"""
    m = LINE_PATTERN.match(line.strip())
    if m:
        return m.group(4)
    return ""


def remove_timestamps(text):
    """从文本中移除时间戳，如 (HH:MM:SS.mmm) 或 (MM:SS)"""
    pattern = r'[\(（]\s*(\d{1,2}:)?\d{2}(:\d{2}\.\d{1,3})?\s*[\)）]'
    return re.sub(pattern, '', text)


def _ts_to_seconds(ts):
    """把时间戳字符串转成秒数。

    支持 HH:MM:SS.mmm / MM:SS.mmm / MM:SS 等任意段数。
    例：'1:23:45.678' -> 5025.678；'00:12.500' -> 12.5
    """
    parts = ts.split(':')
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def _merge_two_lines(line_a, line_b):
    """合并两行：保留 line_a 的时间戳，拼接两行文本。

    line_a 是时间戳较早的行（吸收者），line_b 是被吸收的行。
    返回合并后的单行，格式 '(ts) textA textB'。
    """
    ts_a = _extract_ts(line_a)
    text_a = _extract_text_after_ts(line_a)
    if not text_a:
        text_a = remove_timestamps(line_a).strip()
    text_b = _extract_text_after_ts(line_b)
    if not text_b:
        text_b = remove_timestamps(line_b).strip()
    merged_text = f"{text_a} {text_b}".strip()
    if ts_a:
        return f"({ts_a}) {merged_text}"
    return merged_text


def merge_fast_speaking_lines(lines, max_wpm=MAX_SPEAKING_WPM, log_callback=None):
    """合并语速超过 max_wpm 的行，与相邻语速较慢者合并以降速。

    策略：
    - 逐行计算语速 = 字数 / (下一行时间戳 - 本行时间戳) * 60
    - 找到第一行超阈值的行 i，检查其左右邻居的语速
    - 选邻居中语速较慢（且比自己慢）的方向合并：
        - 合并左邻居：i 吸收进 i-1，保留 i-1 的时间戳
        - 合并右邻居：i 吸收 i+1，保留 i 的时间戳
    - 合并后重新计算所有行语速（因为时长结构变了），重复
    - 若某行超阈值但两邻居都不比自己慢，标记跳过，继续找下一个

    无时间戳的行无法计算语速，自然跳过。
    每次合并让总行数减 1，因此循环必然终止。

    返回 (合并后的新行列表, 合并标记列表)，均不修改原列表。
    合并标记列表与结果行一一对应，True 表示该行由合并产生。
    """
    lines = list(lines)
    is_merged = [False] * len(lines)
    merge_count = 0
    skipped = set()

    while True:
        n = len(lines)
        # 解析每行的时间戳（秒）和字数
        ts_secs = []
        chars = []
        for line in lines:
            ts = _extract_ts(line)
            ts_sec = None
            if ts:
                try:
                    ts_sec = _ts_to_seconds(ts)
                except (ValueError, IndexError):
                    ts_sec = None
            ts_secs.append(ts_sec)
            text_only = remove_timestamps(line)
            chars.append(len(re.sub(r'\s', '', text_only)))

        # 计算每行语速（最后一行无下一行，不计）
        wpms = [None] * n
        for i in range(n - 1):
            if ts_secs[i] is not None and ts_secs[i + 1] is not None:
                dur = ts_secs[i + 1] - ts_secs[i]
                if dur > 0 and chars[i] > 0:
                    wpms[i] = chars[i] / dur * 60

        # 找第一个可处理的超阈值行
        target = None
        merge_dir = None
        for i in range(n):
            if i in skipped or wpms[i] is None or wpms[i] <= max_wpm:
                continue
            left_wpm = wpms[i - 1] if i > 0 else None
            right_wpm = wpms[i + 1] if i < n - 1 else None
            # 在比自己慢的邻居中选最慢的
            best = None
            if left_wpm is not None and left_wpm < wpms[i]:
                best = ('left', left_wpm)
            if right_wpm is not None and right_wpm < wpms[i]:
                if best is None or right_wpm < best[1]:
                    best = ('right', right_wpm)
            if best is not None:
                target = i
                merge_dir = best[0]
                break
            else:
                # 两邻居都不比自己慢，合并无法降速，跳过
                skipped.add(i)

        if target is None:
            break

        # 执行合并，行号变化后重新评估所有行
        skipped.clear()
        if merge_dir == 'left':
            lines[target - 1] = _merge_two_lines(lines[target - 1], lines[target])
            del lines[target]
            del is_merged[target]
            is_merged[target - 1] = True
        else:
            lines[target] = _merge_two_lines(lines[target], lines[target + 1])
            del lines[target + 1]
            del is_merged[target + 1]
            is_merged[target] = True
        merge_count += 1

    if merge_count > 0:
        msg = f"[语速优化] 合并了 {merge_count} 个语速过快的行（阈值 {max_wpm} 字/分）"
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
    return lines, is_merged


def optimize_speaking_rate_in_file(file_path, max_wpm=MAX_SPEAKING_WPM, log_callback=None):
    """读取翻译后的 txt 字幕文件，合并语速过快的行，写回原文件。

    文件格式为每行 '(timestamp) 文本'，空行会被忽略。
    合并后以换行符重新拼接写回，段落间不再保留空行。
    log_callback 用于将合并信息输出到界面（如 Streamlit），为 None 时仅 print。
    返回合并的行数（0 表示无需优化）。
    """
    def _log(msg):
        try:
            print(msg)
        except UnicodeEncodeError:
            # Windows GBK 控制台可能无法编码某些字符，降级输出
            print(msg.encode('ascii', errors='replace').decode('ascii'))
        if log_callback:
            log_callback(msg)

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        _log(f"[语速优化] 读取文件失败: {e}")
        return 0

    # 分割行，保留非空行
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    if len(lines) < 2:
        # 少于2行无法计算语速，无需优化
        return 0

    original_count = len(lines)
    _log(f"[语速优化] 开始检测语速过快的句子（阈值 {max_wpm} 字/分）...")
    merged_lines, is_merged = merge_fast_speaking_lines(lines, max_wpm, log_callback=log_callback)
    merge_count = sum(is_merged)

    if merge_count > 0:
        # 写回文件（保持每行一条，用换行符分隔）
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(merged_lines))
        # 先输出合并后的行预览，让用户看到合并效果
        for i, line in enumerate(merged_lines):
            if is_merged[i]:
                ts = _extract_ts(line)
                text = _extract_text_after_ts(line)
                preview = text[:40] + ('...' if len(text) > 40 else '')
                _log(f"  > 合并行 ({ts}) {preview}")
        # 最后输出总结（作为最后一条消息，便于界面显示）
        _log(f"[语速优化] 完成：合并 {merge_count} 行，字幕行数 {original_count} -> {len(merged_lines)}")
    else:
        _log("[语速优化] 未发现语速过快的句子，无需合并")

    return merge_count
