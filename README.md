# 🤖 HiveDeploy — 多租户机器人托管平台

基于 **AstrBot + NapCat / LLOneBot + Docker** 的共享机器人框架托管平台，支持多用户独立实例、Web 管理面板，自动端口分配。每个用户拥有一套独立的 AstrBot + QQ Bot 容器环境。

完整变更记录见 [CHANGELOG.md](CHANGELOG.md)。

---

## 📋 功能特性

-   👥 **多租户管理** — 用户注册（支持邮箱验证码 + 邀请码）、登录、密码重置
-   🐳 **Docker 容器编排** — 一键创建 AstrBot + NapCat / LLOneBot 实例，自动分配端口
-   🖥️ **公开首页与 Web 管理面板** — 根域名展示产品首页，登录后进入用户仪表板或管理员后台
-   🔌 **弹性端口** — 每用户可额外映射 7 个自定义端口，适配特殊插件需求
-   📮 **邮件系统** — SMTP 配置、可自定义的邮件模板（注册验证、到期提醒等）
-   📢 **公告系统** — 支持多类型公告（信息 / 价格 / 迁移 / 警告 / 封禁），可置顶、自定义样式
-   📋 **邀请码系统** — 支持邀请码注册、生成限制（使用期限 / 每月配额 / 活跃上限 / 编码长度）
-   🌐 **多节点 Hub 同步** — 邀请码和封禁用户跨节点自动同步
-   🔒 **用户封禁** — 支持全局封禁 / 解封，自动同步到 Hub
-   💰 **续期与支付** — 自助续期功能、微信 / 支付宝收款码展示、续期记录
-   📁 **文件管理** — 在线浏览 / 编辑 / 上传容器内文件
-   🖥️ **Web 终端** — 浏览器内直接连接容器终端
-   🚦 **Traefik HTTPS** — 内置 Traefik 反向代理，支持自有 SSL 证书
-   🔄 **持久任务进度** — 创建、更新、切换 Bot 和端口重建进度可跨页面刷新恢复
-   🌐 **镜像源管理** — 管理员维护、排序和设置默认源，用户可手动选择并自动回退
-   🧹 **安全镜像清理** — 只清理三类受管旧镜像，任何容器引用的镜像绝不删除
-   🔗 **一键连接** — 自动配置 AstrBot 与 NapCat 或 LLOneBot 的反向 WebSocket、共享 Token、自身消息上报和调试开关
-   🧭 **按部署显示工具** — 日志、终端和文件入口只显示当前实例实际部署的服务

---

## 📋 架构概览

```
                    ┌─────────────┐
                    │  Traefik    │  ← 反向代理 (80/443)
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   Panel     │  ← FastAPI 后端 + Jinja2 模板
                    │  (端口 3000) │
                    └──────┬──────┘
                           │ Docker API
                    ┌──────▼──────────────────────────┐
                    │         用户实例容器               │
                    │  ┌─────────┐  ┌───────────────┐  │
                    │  │ AstrBot │  │ NapCat/LLOneBot│  │
                    │  │ :6185   │◄─┤ :6099/:3080   │  │
                    │  └─────────┘  └───────────────┘  │
                    │  端口范围: 20000 ~ 29999          │
                    └─────────────────────────────────┘
```

---

## 📋 前置要求

-   Linux 服务器（推荐 Ubuntu 22.04+）
-   Docker 24+（[安装文档](https://docs.docker.com/engine/install/)）
-   Docker Compose v2
-   开放防火墙端口：`80`、`443`、`20000~29999`

---

## 🚀 快速部署

```bash
# 1. 进入项目目录
cd HiveDeploy

# 2. 配置环境变量
cp .env.example .env
nano .env   # 修改 SECRET_KEY、管理员密码、域名等

# 3. 运行部署向导
bash scripts/setup.sh
```

向导会引导你填入：
-   服务器公网 IP 或域名
-   管理员用户名和密码

完成后访问 `http://你的IP/` 可查看公开首页，账号登录入口为 `/login`。

公开首页会根据站点名称、注册开关和最大用户数动态显示入口；已有登录会话可直接进入 `/dashboard`。

---

## ⚙️ 环境变量说明（.env）

| 变量                    | 默认值                         | 说明                                                     |
| ----------------------- | ------------------------------ | -------------------------------------------------------- |
| `SECRET_KEY`            | —                              | JWT 签名密钥，**务必修改为随机长字符串**                  |
| `ADMIN_USERNAME`        | `admin`                        | 管理员用户名（首次启动自动创建）                          |
| `ADMIN_PASSWORD`        | `admin123`                     | 管理员密码，**务必修改**                                  |
| `PLATFORM_HOST`         | `localhost`                    | 服务器公网 IP 或域名（用于展示实例访问地址）              |
| `INSTANCE_PORT_BASE`    | `20000`                        | 用户实例端口起始值                                       |
| `DATA_DIR`              | `/data/instances`              | 用户实例数据持久化目录                                   |
| `BOT_NETWORK`           | `bot_user_net`                 | 用户容器所属 Docker 网络                                 |
| `SITE_NAME`             | —                              | 节点名称（多节点 Hub 同步时标识当前节点）                |

---

## 📡 端口分配规则

每用户分配 **10 个端口**（stride = 10）：

| 用户 ID | AstrBot WebUI (:6185) | Bot WebUI (:6099/3080) | AstrBot WS (:6199) | 弹性端口 (7 个)  |
| ------- | --------------------- | ---------------------- | ------------------ | ---------------- |
| 1       | 20000                 | 20001                  | 20002              | 20003 ~ 20009    |
| 2       | 20010                 | 20011                  | 20012              | 20013 ~ 20019    |
| N       | 20000 + (N-1)×10     | +1                     | +2                 | +3 ~ +9          |

> 防火墙需开放 `20000~29999` 端口段。

---

## 🧭 公开首页

-   根域名 `/` 展示产品首页，介绍实例编排、一键连接、任务进度和安全运维能力。
-   `/login` 保持为账号登录入口，`/dashboard` 为登录后的控制台。
-   首页注册按钮会遵守后台的注册开关和最大用户数限制。
-   页面支持桌面与移动端导航、滚动动画和系统“减少动态效果”设置。

---

## 🔄 持久任务与镜像操作

-   创建、更新、Bot 切换和端口重建进度写入数据库，刷新页面后会恢复同一任务，不会重复执行操作。
-   面板重启后，失去后台线程的任务会明确标记为“已中断”。
-   同一账号只允许一个镜像或重建任务运行，避免并发操作互相覆盖。
-   管理员可在 **镜像管理** 中新增、排序、启停镜像源并设置默认来源；Docker Hub 官方源始终保留。
-   用户可选择自动模式或指定来源。指定来源失败后，仍会按管理员顺序继续回退。
-   清理器只处理 AstrBot、NapCat 和 LLOneBot 镜像，并保护最新镜像以及被任意运行中或已停止容器引用的镜像。

---

## 🔗 一键连接与 LLOneBot 持久化

-   一键连接会根据实例当前 Bot 类型自动选择 NapCat 或 LLOneBot，并配置 AstrBot 的单个 `aiocqhttp` 反向 WebSocket 平台。
-   AstrBot 与当前 QQ Bot 使用同一个随机 Token；NapCat/LLOneBot 同时开启自身消息上报和调试。
-   必须先完成扫码登录并且只能发现一个账号配置；缺少配置或存在多个账号时会停止并提示。
-   配置在容器停止后原子写入持久目录，失败时会尝试恢复原配置。
-   LLOneBot 的 `/root/llonebot/data` 已持久化，账号配置、授权文件、`auth_token.txt`/`webui_token.txt` 和连接配置可跨重建保留。
-   面板不再预写无效的 `config/llonebot.json`，而是读取扫码登录生成的真实 `config_<QQ号>.json`。
-   重建时会复用已保存的 WebUI Token；仅有一个账号配置时自动恢复对应 QQ 登录。

---

## 📖 使用流程

### 管理员

1.  登录面板，进入 **管理后台**
2.  配置 **站点设置**（注册开关、最大用户数、邮箱域名限制、邀请码策略等）
3.  配置 **SMTP**（用于邮箱验证和到期提醒）
4.  可选配置 **支付信息**（收款码、续期开关）
5.  发布 **公告**（支持信息 / 价格 / 迁移 / 警告 / 封禁等类型）
6.  在 **镜像管理** 中维护下载源，并按需预览、执行或定时清理旧镜像
7.  管理用户（创建 / 封禁 / 删除 / 续期 / 重置密码）
8.  查看续期记录和邀请码统计

### 用户

1.  注册账号（通过邮箱验证码 + 邀请码）或由管理员创建
2.  登录后进入 **仪表板**
3.  点击 **「创建我的实例」**，选择 NapCat 或 LLOneBot（首次需拉取镜像，约 1~3 分钟）
4.  实例创建后可以：
    -   打开 **AstrBot WebUI** 配置 AI 模型和插件
    -   打开 **NapCat / LLOneBot WebUI** 扫码登录 QQ
    -   使用 **一键配置** 自动完成 AstrBot 与当前 NapCat / LLOneBot 的反向 WebSocket 连接，并开启自身消息上报和调试
    -   管理 **弹性端口**（额外映射 7 个自定义端口）
    -   查看实时系统资源（CPU / 内存 / 磁盘）
    -   管理当前已部署服务的容器文件（浏览 / 编辑 / 上传）
    -   打开当前已部署服务的 **日志** 或 **Web 终端**
    -   拉取最新镜像更新实例
    -   自助续期（如管理员开启）

---

## 🔒 配置 HTTPS（自有 SSL 证书）

### 1. 放置证书文件

将证书文件复制到 `traefik/certs/` 目录：

```bash
cp 你的证书.crt traefik/certs/fullchain.crt
cp 你的私钥.key traefik/certs/private.pem
```

> 宝塔面板证书通常在 `/www/server/panel/vhost/cert/你的域名/` 下，`fullchain.pem` 为证书，`privkey.pem` 为私钥。

### 2. 修改域名

编辑 `docker-compose.yml`，将 Traefik labels 中的 `Host()` 替换为你的域名。

### 3. 启用 Traefik HTTPS

编辑 `traefik/tls.yml`，取消注释 tls 配置块。

### 4. 重启服务

```bash
docker compose down && docker compose up -d
docker compose logs traefik --tail=15
```

日志无 `ERR` 即成功，访问 `https://你的域名.com`。

---

## 🛠️ 常用运维命令

```bash
# 查看面板日志
docker compose logs -f panel

# 重启面板
docker compose restart panel

# 重建并重启面板（代码更新后）
docker compose build --no-cache panel && docker compose up -d panel

# 重启 Traefik
docker compose restart traefik

# 停止所有服务
docker compose down

# 证书更新后重载 Traefik
docker compose restart traefik

# 备份所有实例数据
bash scripts/backup.sh

# 备份指定用户
bash scripts/backup.sh 用户名

# 重置管理员密码
bash scripts/reset_admin.sh

# 预览现有 LLOneBot 容器数据迁移（默认不会写入，并自动识别实例卷路径）
bash scripts/migrate_llonebot_data.sh

# 确认路径后执行 LLOneBot 数据迁移
DRY_RUN=0 bash scripts/migrate_llonebot_data.sh
```

### 镜像自动清理切换说明

网页端清理计划默认关闭。若服务器上已有旧的 systemd 清理任务，部署新版后应先执行
`systemctl disable --now hivedeploy-image-cleanup.timer`，确认旧调度已停止并移除旧 service、timer 和脚本，再在“管理后台 → 镜像管理”中开启每日清理，避免同一时间重复执行。

清理器不会使用 `docker image prune -a`，不会处理未知项目镜像、容器或卷；每类保留最新镜像，并在删除前再次检查运行中和已停止容器的引用。

---

## 🐳 从容器同步代码到宿主机

```bash
docker cp bot_panel:/app/app/. panel/app/
docker cp bot_panel:/app/templates/. panel/templates/
```

---

## ❓ 常见问题

**Q: 创建实例卡在「正在拉取镜像」很久？**
A: 首次需下载 AstrBot 和 NapCat / LLOneBot 镜像，共约 500MB~1GB。面板会自动尝试多个镜像加速源，如遇超时请检查服务器网络。

**Q: 忘记管理员密码？**
A: 在服务器上执行 `bash scripts/reset_admin.sh`，选择管理员账号并输入新密码即可。

**Q: 端口被占用？**
A: 修改 `.env` 中的 `INSTANCE_PORT_BASE`，例如改为 `30000`，并确保防火墙对应开放。

**Q: NapCat 扫码后连接不上 AstrBot？**
A: 点击快捷工具中的 **「一键配置连接」**。面板会自动配置 AstrBot 与当前 NapCat/LLOneBot 的反向 WebSocket、共享 Token、自身消息上报和调试。若提示未找到或存在多个账号配置，请先确认扫码登录只生成了一个账号配置。

**Q: 如何彻底重置某用户实例？**
A: 在管理后台删除用户后，数据目录保留在 `/data/instances/用户名/`，重新创建用户并创建实例即可复用数据（或手动删除该目录彻底清除）。

**Q: 如何更换 QQ Bot 类型（NapCat ↔ LLOneBot）？**
A: NapCat 和 LLOneBot 互斥部署，切换时旧容器会自动清除。在创建实例或管理实例时选择即可。

---

## 💝 赞助支持

如果这个项目对你有帮助，欢迎请开发者喝杯咖啡 ☕

-   **Patreon** — [patreon.com/cw/YukiSoffd](https://www.patreon.com/cw/YukiSoffd)
-   **爱发电** — [ifdian.net/a/HiveDeploy](https://www.ifdian.net/a/HiveDeploy)

---

## 📂 项目结构

```
HiveDeploy/
├── .env.example              # 环境变量模板
├── docker-compose.yml        # Docker Compose 编排文件
├── panel/                    # 面板后端
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/                  # FastAPI 应用
│   │   ├── main.py           # 入口：挂载所有路由
│   │   ├── bootstrap.py      # 应用初始化、DB 迁移、模板配置
│   │   ├── auth.py           # 认证工具（JWT、密码哈希）
│   │   ├── database.py       # SQLAlchemy 数据库连接
│   │   ├── models.py         # 数据模型（用户、实例、配置等）
│   │   ├── docker_manager.py # Docker 容器编排（创建/启停/端口管理）
│   │   ├── progress_store.py # 可跨页面恢复的后台任务进度
│   │   ├── image_management.py # 镜像源排序、安全清理和定时计划
│   │   ├── service_access.py # 当前实例服务访问控制
│   │   ├── email_service.py  # 邮件发送与到期提醒
│   │   ├── hub_sync.py       # 多节点 Hub 同步
│   │   ├── filemanager.py    # 容器文件管理
│   │   ├── routes_auth.py    # 认证路由（登录/注册/密码重置）
│   │   ├── routes_user.py    # 用户路由（仪表板/续期/统计）
│   │   ├── routes_instances.py # 实例管理路由
│   │   ├── routes_images.py  # 镜像源与清理管理路由
│   │   ├── routes_files.py   # 文件管理路由
│   │   ├── routes_admin.py   # 管理后台路由
│   │   ├── routes_invites.py # 邀请码路由
│   │   ├── routes_nodes.py   # 服务器节点路由
│   │   └── routes_terminal.py# Web 终端路由
│   ├── templates/            # Jinja2 前端模板（含公开首页）
│   ├── static/               # 静态资源
│   └── tests/                # 任务、镜像、连接和入口回归测试
├── traefik/                  # Traefik 配置
│   ├── tls.yml               # TLS 证书配置
│   └── certs/                # 证书文件目录
├── scripts/                  # 运维脚本
│   ├── setup.sh              # 部署向导
│   ├── backup.sh             # 数据备份
│   ├── reset_admin.sh        # 管理员密码重置
│   └── migrate_llonebot_data.sh # 现有 LLOneBot 数据迁移
└── CHANGELOG.md              # 完整更新日志
