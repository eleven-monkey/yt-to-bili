---
title: Yt Video Trans
emoji: 🚀
colorFrom: red
colorTo: red
sdk: docker
app_port: 7860
tags:
- streamlit
pinned: false
short_description: 汉化油管视频
license: mit
---

# YouTube视频转B站搬运工具

全自动将YouTube视频转换为B站格式并上传的工具。

## 功能特性

- 🚀 一键工作流：从YouTube下载到B站上传全自动化
- 🗣️ TTS语音合成：支持多进程加速的文本转语音
- ✂️ 视频剪辑：支持视频裁剪和音频替换
- 📤 B站上传：自动生成标签并上传视频
- 🌐 多语言支持：中英文翻译和语音合成
- ⚡ 高性能：多进程并行处理，显著提升速度

## 环境变量配置

### 必需配置

- `API_KEY`: OpenAI API密钥（用于翻译功能）
- `API_URL`: API端点URL，默认为 `https://api.openai.com/v1/chat/completions`
- `MODEL_NAME`: 使用的AI模型，默认为 `gpt-3.5-turbo`

### 可选配置

- `YT_COOKIES`: YouTube cookies（用于访问需要登录的视频）
- `BILIBILI_SESSDATA`: B站SESSDATA（用于B站上传）
- `BILIBILI_BILI_JCT`: B站bili_jct（用于B站上传）
- `BILIBILI_BUVID3`: B站buvid3（用于B站上传）

## Hugging Face Spaces 部署

### 部署准备步骤

#### **步骤1: 创建requirements.txt**
确保包含所有必要的依赖：

```txt
streamlit>=1.28.0
yt-dlp>=2023.12.30
edge-tts>=6.1.0
pydub>=0.25.1
bilibili-api-python>=16.2.0
Pillow>=10.0.0
numpy>=1.24.0
requests>=2.31.0
python-multipart>=0.0.6
```

#### **步骤2: 创建packages.txt**
```txt
ffmpeg
```

#### **步骤3: 创建新的Space**
1. 访问 [huggingface.co/spaces](https://huggingface.co/spaces)
2. 点击 "Create new Space"
3. 填写信息：
   - **Space name**: `yt-to-bilibili`
   - **License**: 选择合适的许可证
   - **SDK**: 选择 **Streamlit**
   - **Visibility**: **Public**
4. 点击 "Create Space"

#### **步骤4: 上传项目文件**
使用 "Add file" 按钮上传：
- `app.py`
- `requirements.txt`
- `packages.txt`
- `README.md`

#### **步骤5: 配置环境变量**
在Spaces的Settings > Secrets中设置：

```
Name: API_KEY, Value: your-openai-api-key
Name: API_URL, Value: https://api.openai.com/v1/chat/completions
Name: MODEL_NAME, Value: gpt-3.5-turbo
Name: YT_COOKIES, Value: your-youtube-cookies (可选)
```

#### **步骤6: 设置Space配置**
创建 `runtime.txt`：
```txt
python-3.10
```

### 本地运行

如需在本地运行：

1. 安装依赖
```bash
pip install -r requirements.txt
```

2. 配置环境变量
```bash
cp .env.example .env
# 编辑 .env 文件
```

3. 运行应用
```bash
streamlit run app.py
```

## 使用方法

### 快速开始

1. 在左侧边栏配置必要的API密钥
2. 输入YouTube视频URL
3. 选择需要的功能并执行

### 一键工作流

最简单的使用方式：

1. 在"一键工作流"标签页输入YouTube视频URL
2. 勾选"自动上传到B站"（默认已勾选）
3. 点击"🚀 开始一键工作流"
4. 等待自动化完成所有步骤

### 单独功能使用

#### 下载字幕
- 进入"⬇️ 下载字幕"标签页
- 输入YouTube URL
- 点击下载获取字幕文件

#### 翻译字幕
- 进入"⚙️ 翻译字幕"标签页
- 上传或选择字幕文件
- 选择目标语言并开始翻译

#### TTS转语音
- 进入"🗣️ TTS字幕转语音"标签页
- 选择翻译后的文本文件
- 选择语音角色并开始转换

#### 视频剪辑
- 进入"✂️ 视频剪辑"标签页
- 上传视频文件
- 设置裁剪时间并处理

#### B站上传
- 进入"📤 B站上传"标签页
- 配置B站账号信息
- 上传处理完成的视频

## 功能详解

### 一键工作流

自动化执行以下步骤：
1. **下载字幕**: 从YouTube获取字幕文件
2. **翻译标题**: 使用AI生成吸引眼球的中文标题
3. **翻译字幕**: 将英文字幕翻译成中文
4. **转语音**: 多进程TTS合成中文语音
5. **下载视频**: 获取YouTube视频文件
6. **处理封面**: 生成视频封面图片
7. **上传B站**: 自动上传到B站（如果勾选）

### 多进程加速

- **TTS处理**: 4进程并行合成语音
- **速度调整**: 8进程并行优化音频时长
- **智能重试**: 网络错误自动重试（最多5次）
- **内存优化**: 共享内存处理大文件

### 智能翻译

- **上下文感知**: 保持视频内容的语义完整性
- **时间戳保留**: 维持字幕的时间同步
- **质量优化**: 使用专业翻译提示词
- **分段处理**: 长文本自动分段翻译

## 注意事项

### 存储和性能

- 应用会自动清理临时文件
- 大文件处理可能需要较长时间
- 建议单次处理不超过10分钟的视频

### API使用

- OpenAI API有使用费用，请注意预算
- B站上传需要有效的账号和cookies
- YouTube cookies可提升下载成功率

### 法律合规

- 请确保有权处理和上传相关内容
- 遵守YouTube和B站的使用条款
- 尊重版权和知识产权

## 故障排除

### 常见问题

1. **下载失败**: 检查YouTube URL是否正确，必要时配置cookies
2. **翻译错误**: 确认OpenAI API密钥和配额
3. **语音合成失败**: 检查网络连接，可能需要重试
4. **上传失败**: 验证B站账号信息和cookies

### 调试模式

设置环境变量启用详细日志：
```bash
export STREAMLIT_DEBUG=true
```

## 贡献指南

欢迎提交Issue和Pull Request来改进这个项目！

### 开发环境设置

1. Fork项目
2. 创建特性分支
3. 提交更改
4. 发起Pull Request

## 许可证

[MIT License](LICENSE)

## 致谢

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - 强大的视频下载工具
- [Edge TTS](https://github.com/rany2/edge-tts) - 微软TTS集成
- [bilibili-api](https://github.com/Nemo2011/bilibili-api) - B站API客户端
- [Streamlit](https://streamlit.io/) - 简易Web应用框架

## 更新日志

### v2.1
- ✨ 新增一键工作流功能
- 🚀 实现多进程TTS加速
- 🔧 优化视频处理性能
- 🐛 修复序列化错误

### v2.0
- 🎯 完全重构用户界面
- 🌐 支持多语言处理
- 📱 改善移动端适配
- ⚡ 提升整体性能

---

如有问题或建议，请提交GitHub Issue。