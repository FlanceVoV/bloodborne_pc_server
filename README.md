# Bloodborne PC Server v1.0

为 [Supermedo/bloodborne_pc](https://github.com/Supermedo/bloodborne_pc) 的 Windows 1.6 版提供联机大厅、服务器目录、玩家开服入口、房间、管理网页和 Mod 分发。游戏实际使用的登录、匹配、信令及召唤协议由开源 [Wozzardman/shadnet-p2p](https://github.com/Wozzardman/shadnet-p2p) 服务端处理；本仓库负责管理与发现，不复制游戏文件或存档。

## 快速启动

需要 Python 3.11+。Windows 双击 `run.bat`；Linux 执行 `chmod +x run.sh && ./run.sh`。脚本首次运行会创建虚拟环境并安装依赖，随后自动生成 `config.json` 和随机管理员密码，终端会显示一次。管理页默认是 <http://127.0.0.1:18080/>。配置文件包含管理员密码与签名密钥，已被 `.gitignore` 排除；不要公开。

此步骤会启动大厅与管理 API。如果要让本机也承担游戏联机协议，先安装 shadNet 后端。最省事的是 Windows 运行 `fetch-backend.bat`，Linux 运行 `./fetch-backend.sh`：脚本从 [Wozzardman/shadnet-p2p 的 GitHub Actions](https://github.com/Wozzardman/shadnet-p2p/actions) 下载锁定提交 `5a849a1` 的产物，校验 SHA-256，自动写入本机 `config.json`。GitHub Actions 产物下载需要 `GITHUB_TOKEN` 或本机 Git 凭据。Linux 产物仍需系统 Qt 6 运行库。

也可以从源码构建：

1. 安装 Git、CMake、C++ 编译器、Qt 6 Core/Network/SQL/Concurrent/HttpServer 和 SQLite Qt SQL 驱动。Linux/Windows 的具体 Qt 构建要求见 [shadnet-p2p README](https://github.com/Wozzardman/shadnet-p2p)。
2. Windows 运行 `build-backend.bat`，Linux 运行 `./build-backend.sh`。脚本固定到已核对的 `5a849a192fd4450593c57b10697316ac324c856e` 提交，并拉取其子模块。Windows 构建时按 Qt 安装位置设置 `CMAKE_PREFIX_PATH`。
3. 若从源码构建，编辑 `config.json` 的 `shadnet.enabled` 为 `true`，`shadnet.executable` 指向构建出的 `shadnet.exe` 或 `shadnet`，重启 `run`。首次启动自动生成带随机注册密钥的 `shadnet.cfg`；管理员可以在网页「服务器」页创建游戏账号。后端账户由 shadNet 自己维护，本服务通过其 Create 协议注册，不直接改写后端数据库。
4. 在管理网页「服务器」新增本机实际可供玩家访问的主机/IP、游戏协议端口（通常 31313）和 WebAPI 地址（通常 31315）。如需让局域网或 Tailscale 玩家访问大厅，设置 `listen_host`、`public_base_url` 后重启。公网部署须使用 HTTPS 反向代理。

游戏协议默认走 TCP 31313、WebAPI TCP 31315；匹配信令可用 UDP 31314。玩家必须使用同一兼容版本和可互通的网络。Supermedo 当前客户端仍将游戏社区请求（建言、血迹、幻影）交给 `community_url`，默认 The Hunter's Dream；它们**没有**在本服务重实现。

## 客户端联机大厅

配套的 [FlanceVoV/bloodborne_pc](https://github.com/FlanceVoV/bloodborne_pc) Windows 启动器在「Online co-op / 在线联机」页增加「联机大厅」。填大厅地址后，可以浏览兼容服务器、输入 shadNet NPID/密码、选择服务器并下载/安装兼容 Mod。密码只交给游戏的 shadNet 客户端，不发送给大厅 API。选择服务器会写入启动器现有的 shadNet 与社区服务字段；运行游戏期间，大厅收到独立的连接心跳，用于显示近似在线人数。未使用大厅时，原有手填联机配置仍可用。

服务器初始兼容配置为 `supermedo-windows-v1.6`，游戏 `01.09`、协议 `shadnet-p2p-v1`。管理页可增加其他 `profile_id`；将来新增客户端适配器时按 ID 查询对应服务器和 Mod。

## 玩家自助开服与房间

`/host` 页面可注册平台账号、发布自己运行的 shadNet 服务器、创建房间。发布的服务器每分钟心跳，关闭页面约三分钟后自动从大厅列表隐藏。平台账号只管理发布权限；shadNet NPID 需要在对应游戏服务端另行创建。

玩家自开服目前采用可访问地址或同一 Tailscale 网络；平台负责目录、版本匹配和房间协调，**尚未实现游戏包的 TCP/UDP 反向中转或 NAT 穿透**。shadNet 自带的匹配与信令负责实际联机。管理页「下线」会结束平台连接记录与令牌；它不能强制踢出 shadNet 内的游戏会话。管理员封禁同样先作用于平台和大厅，后端账户封禁需在 shadNet 中同步处理。

## 大厅与统计

公开大厅与玩家开服页参考 The Hunter's Dream 的深色哥特视觉，但使用本项目自己的页面和样式。公开页显示大厅在线猎人、平台已注册账号、可用服务器和开放房间。管理页提供近七日加入趋势、近 24 小时加入与独立猎人、累计加入、服务器活跃度、最近加入、玩家开服数、Mod 下载数和建言待处理数。数据分别来自本地数据库的连接心跳、玩家、服务器、房间、Mod 与建言记录；不混用 The Hunter's Dream 公开站点的实时数字。

游戏内死亡、敌人击杀、灯笼、圣杯和游戏内建言由独立社区服务处理，当前客户端没有向本平台上报这些事件。管理页将其明确标为「待接入」，不会用零冒充已统计的真实值。`GET /api/v1/public/stats` 提供可公开展示的摘要；`GET /api/v1/admin/analytics` 需要管理员登录。在线猎人按最近 120 秒启动器心跳估算，不能视为 shadNet 内部的权威人数。

## Mod 分发

管理员上传 ZIP，根目录必须包含 `mod.json`，例如：

```json
{"slug":"sample-mod","version":"1.0"}
```

ZIP 可包含 `dvdroot_ps4/` 下的游戏资源。服务器限制压缩包 100 MiB、解压后 500 MiB，并拒绝越界路径和软链接。上传后保存 SHA-256，客户端复验后把原 ZIP 保存到 `mods-down/lobby/`，将内容安装到 `mods/<slug>/`。旧安装会移到 `mods-down/lobby-backups/`。所有玩家应使用相同 Mod 版本；平台只按兼容配置筛选，不判断 Mod 之间的冲突或游戏规则。

## 数据库

默认 SQLite 数据文件为 `data/server.db`，使用 WAL 与外键。SQLAlchemy 模型包括兼容版本、服务器、玩家、连接及心跳、建言、房间、Mod、令牌和审计。配置中 `database_url` 可以改为 PostgreSQL URL（例如 `postgresql+psycopg://user:password@host:5432/bloodborne`），先安装 `requirements-postgres.txt`。业务查询只使用 SQLAlchemy 通用表达式；新库可直接建表。既有 SQLite 数据迁移到 PostgreSQL 需单独执行迁移，不能仅改 URL。

## 主要 API

| 功能 | 端点 |
| --- | --- |
| 兼容配置、服务器、Mod 目录 | `GET /api/v1/profiles`, `/servers`, `/mods` |
| 公开统计、管理分析 | `GET /api/v1/public/stats`, `/admin/analytics` |
| Mod 下载 | `GET /api/v1/mods/{id}/download` |
| 启动器连接统计 | `POST /api/v1/connections/start`, `/{id}/heartbeat`, `/{id}/end` |
| 平台玩家注册、登录、房间 | `/api/v1/players/*`, `/api/v1/rooms/*` |
| 玩家自开服 | `/api/v1/host/servers/*` |
| 管理员 | `/api/v1/admin/*`，登录后用 HttpOnly Cookie 与 CSRF 请求头 |

## 安全与运行限制

- 默认只监听本机。公网必须使用 HTTPS、限制管理接口访问，并定期备份 `config.json` 与 `data/`。不要将 shadNet 明文游戏协议直接暴露到不可信公网；私有网络或 Tailscale 更合适。
- 大厅在线人数由启动器心跳估算，不是 shadNet 内部权威人数。平台封禁与强制下线不会直接操作 shadNet 的会话。
- 本仓库不携带 shadNet 二进制；运行 `fetch-backend` 或自行构建后，`run` 可同时拉起它。未安装后端时大厅与管理 API 仍可使用，实际游戏联机服务不可用。

测试：`python -m pip install -r requirements-dev.txt` 后运行 `python -m pytest -q`。
