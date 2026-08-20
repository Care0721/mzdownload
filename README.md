# astrbot_plugin_mzdownload
## 注意！ 此插件随时因萌宅社区版本更新而失效，仅供个人使用

对接萌宅社区（mengzhai.club）HTTP API 的 AstrBot 插件，支持软件搜索、最新/热门列表、详情查看以及下载链接获取。

- 点此链接`https://mengzhai.club`下载萌宅社区软件获取账号密码

## 功能

- `/mz搜索 <关键词>`：搜索软件
- `/mz最新`：获取最新软件列表
- `/mz热门`：获取热门软件列表
- `/mz详情 <软件ID>`：查看软件详情
- `/mz下载 <软件ID>`：获取下载链接（需登录）
- `/mz小说搜索 <名称>`：搜索小说内容
- `/mz小说详情 <path_word>`：获取小说详情
- `/mz小说分卷 <path_word>`：查看小说卷数
- `/mz小说主题`

特性：

- Token 内存缓存，过期前约 60 秒自动刷新
- 账号/密码变更后强制重新登录
- 登录使用 asyncio.Lock，防止并发重复登录
- 同一用户冷却（管理员免冷却）
- 支持合并转发发送下载结果（主要适配 OneBot v11，失败自动回退纯文本）
- 完善的异常处理（超时、非 JSON、登录失败、限流、网络错误等）

## 安装

1. 将本插件目录放入 AstrBot 的 `data/plugins/` 下，目录名建议为：

```
data/plugins/astrbot_plugin_mzdownload/
```

2. 确保目录内包含以下文件：

```
astrbot_plugin_mzdownload/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

3. 在 AstrBot WebUI → 插件管理 中找到「萌宅下载」，点击重载（或重启 AstrBot）。

4. 依赖会自动安装（httpx>=0.27.0）。若未自动安装，可手动执行：

```
pip install "httpx>=0.27.0"
```

## 配置（WebUI）

进入 插件管理 → 萌宅下载 → 配置，填写以下项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| email | string | 空 | 萌宅账号（邮箱或用户名） |
| password | string | 空 | 萌宅密码 |
| admin_only | bool | false | 为 true 时仅管理员可用 |
| cooldown_seconds | int | 8 | 同一用户冷却秒数；管理员不受限制；0 表示不限制 |
| list_limit | int | 10 | 搜索/最新/热门列表最多显示条数（建议 5～30） |
| send_as_forward | bool | false | true 时下载结果使用合并转发（聊天记录）发送 |

注意：只有 `/mz下载` 需要登录。搜索、最新、热门、详情为公开接口，可不填账号密码。

## 使用示例

```
/mz搜索 Photoshop
/mz最新
/mz热门
/mz详情 123e4567-e89b-12d3-a456-426614174000
/mz下载 123e4567-e89b-12d3-a456-426614174000
```

软件 ID 必须为标准 UUID 格式。

## 指令说明

### /mz搜索 <关键词>

搜索软件，返回标题、ID、大小，并提示使用 /mz下载。

### /mz最新

获取最新软件列表。

### /mz热门

获取热门（点赞较多）软件列表。

### /mz详情 <软件ID>

查看指定软件的标题、大小与描述。

### /mz下载 <软件ID>

调用下载准备接口，返回文件名、大小、是否会员资源及下载链接。需要在 WebUI 中正确配置 email 与 password。

若开启 send_as_forward，结果会以合并转发形式发送（主要适配 OneBot v11）；构建失败时自动回退为纯文本。

## API 说明（已对接）

Base URL：https://cn-api.mengzhai.club

| 功能 | 方法 | 路径 | 是否需要登录 |
|------|------|------|--------------|
| 登录 | POST | /api/auth/login | - |
| 搜索 | GET | /api/software?keyword= | 否 |
| 最新 | GET | /api/software/home/latest | 否 |
| 热门 | GET | /api/software/home/most-liked | 否 |
| 详情 | GET | /api/software/{id} | 否 |
| 下载准备 | POST | /api/software/{id}/download | 是（Bearer Token） |

下载接口可能返回限流错误 DOWNLOAD_RATE_LIMITED（带 retryAfterSec），插件会友好提示。

## 注意事项

1. 请勿将该插件用于官方群聊
2. 下载接口有频率限制，请合理使用，避免触发限流。
3. 合并转发主要适配 OneBot v11；其他平台建议保持 send_as_forward = false。
4. 插件卸载/停用时会自动关闭 httpx.AsyncClient。
5. 冷却仅对同一用户生效，管理员默认免冷却。

## 依赖

- Python 3.10+
- AstrBot
- httpx>=0.27.0

## 开发与规范

本插件遵循 AstrBot 官方插件规范：

- 模板：https://github.com/Soulter/helloworld
- 开发文档：https://docs.astrbot.app/dev/star/plugin-new.html
- 配置说明：https://docs.astrbot.app/dev/star/guides/plugin-config.html
- 消息发送：https://docs.astrbot.app/dev/star/guides/send-message.html

使用 @register 注册，类继承 Star，网络请求仅使用 httpx 异步客户端，指令使用 @filter.command。

## 许可证

请遵循 AstrBot 及相关项目的开源协议。使用本插件获取的下载链接请遵守萌宅网站的服务条款与版权规定，仅供个人学习研究使用。