# 部署到 Zeabur 指南

Zeabur 是一个现代化的部署平台，支持从 GitHub 自动部署 Docker 应用。相比 Hugging Face Spaces，它的网络环境通常更灵活（建议选择香港或日本区域）。

## 1. 准备 GitHub 仓库

由于 Zeabur 需要从 GitHub 拉取代码，你需要先将本项目上传到一个 GitHub 仓库。

### 步骤 1.1: 在 GitHub 创建新仓库
1. 登录 [GitHub](https://github.com/)。
2. 点击右上角 **+** 号 -> **New repository**。
3. 仓库名填入 `yt-video-trans` (或其他你喜欢的名字)。
4. 选择 **Public** 或 **Private** (私有仓库也可以)。
5. 点击 **Create repository**。

### 步骤 1.2: 推送代码到 GitHub
在你的本地项目根目录 (`D:\python-env\YTvideotrans - GLM4.7\`) 打开终端：

```bash
# 1. 移除旧的 git 关联 (如果是从 Hugging Face 迁移过来)
rmdir /s /q .git  # Windows 命令，小心使用，确保只删除了 .git 文件夹
# 或者直接在资源管理器中把隐藏的 .git 文件夹删掉

# 2. 重新初始化 git
git init

# 3. 添加所有文件
git add .

# 4. 提交
git commit -m "Initial commit for Zeabur deployment"

# 5. 关联 GitHub 仓库 (将 YOUR_USERNAME 替换为你的 GitHub 用户名)
git remote add origin https://github.com/YOUR_USERNAME/yt-video-trans.git

# 6. 推送到 GitHub
git push -u origin main
```

## 2. 在 Zeabur 上部署

### 步骤 2.1: 创建项目
1. 登录 [Zeabur Dashboard](https://dash.zeabur.com/) (可以使用 GitHub 账号登录)。
2. 点击 **Create Project** (创建项目)。
3. 选择一个区域 (Region)。**强烈建议选择香港 (Hong Kong) 或新加坡 (Singapore)**，这样访问 YouTube 和 B站的速度都比较平衡。

### 步骤 2.2: 创建服务
1. 在项目页面，点击 **Deploy New Service** (部署新服务)。
2. 选择 **Git**。
3. 如果是第一次使用，需要授权 Zeabur 访问你的 GitHub 仓库。
4. 在列表中选择你刚才创建的 `yt-video-trans` 仓库。
5. 点击 **Deploy**。

Zeabur 会自动检测到项目中的 `Dockerfile` 并开始构建。

## 3. 配置环境变量

项目构建过程中（或者构建失败后），你需要配置必要的环境变量。

1. 点击刚才创建的服务卡片，进入服务详情页。
2. 点击顶部的 **Variables** (环境变量) 标签。
3. 点击 **Add Variable**，添加以下变量：

| Key | Value | 说明 |
| :--- | :--- | :--- |
| `API_KEY` | `sk-...` | 你的 OpenAI/SiliconFlow API Key |
| `API_URL` | `https://api.openai.com/v1/chat/completions` | API 地址 |
| `MODEL_NAME` | `gpt-3.5-turbo` | 模型名称 |
| `BILI_SESSDATA` | (你的 sessdata) | B站上传凭证 |
| `BILI_BILI_JCT` | (你的 jct) | B站上传凭证 |
| `BILI_BUVID3` | (你的 buvid3) | B站上传凭证 |
| `YT_COOKIES` | (可选) | YouTube Cookies |

**注意**：添加完变量后，服务会自动重新部署以生效。

## 4. 绑定域名与访问

1. 在服务详情页，点击 **Networking** (网络) 标签。
2. 在 **Domains** 部分，点击 **Generate Domain** (生成域名) 或者绑定你自己的域名。
3. 等待几秒钟，Zeabur 会生成一个类似 `xxx.zeabur.app` 的网址。
4. 点击该网址即可访问你的应用！

## 常见问题

*   **构建失败**: 检查 Logs (日志) 标签页，通常是 `requirements.txt` 里的依赖版本问题。
*   **YouTube 无法下载**: 
    *   确保你在创建项目时选择了海外区域（香港/新加坡/美国）。
    *   如果仍然报错，可能需要配置 IPv6 或代理，但在 Zeabur 上通常不需要。
