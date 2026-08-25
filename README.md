# Codex Bark Watch Notifier

让 Codex 任务结束或需要用户操作时，通过 Bark 将中文提醒发送到 iPhone，并由 iPhone 镜像到 Apple Watch。

> 这是社区方案，不是 OpenAI、Bark 或 Apple 的官方集成。

## 能做什么

- Codex 生成最终回复后发送“任务已完成”提醒。
- 最终回复明确要求确认、选择或补充信息时，发送“等待确认”提醒。
- Codex 发出可识别的授权请求时，发送“等待授权”提醒。
- 使用 Mac 钥匙串保存 Bark 推送地址，脚本内不写入设备密钥。
- 按任务和回合去重，减少重复通知。
- 普通完成使用普通通知；确认与授权使用时效性通知。

## 工作原理

```text
Codex notify
  -> 本地通知脚本
  -> 读取 Codex 最终回复并分类
  -> 从 macOS 钥匙串读取 Bark 地址
  -> Bark
  -> iPhone
  -> Apple Watch 通知镜像
```

Apple Watch 并不直接连接 Codex。手表能否收到提醒，取决于 iPhone 的 Bark 通知权限和 Apple Watch 的通知镜像设置。

## 环境要求

- macOS
- Codex CLI 或支持用户级 `~/.codex/config.toml` 的 Codex 环境
- iPhone 上已安装并启用 Bark
- 如需手表提醒：Apple Watch 已开启 Bark 通知镜像
- 系统自带 Python 3

## 安装

### 1. 下载脚本

```bash
mkdir -p ~/.codex/notifications
cp codex_notify_dispatcher.py ~/.codex/notifications/
cp setup_codex_bark_keychain.sh ~/.codex/notifications/
chmod 700 ~/.codex/notifications/codex_notify_dispatcher.py
chmod 700 ~/.codex/notifications/setup_codex_bark_keychain.sh
```

### 2. 保存 Bark 地址

在 Bark 首页复制个人推送地址，然后运行：

```bash
~/.codex/notifications/setup_codex_bark_keychain.sh
```

输入内容不会显示。地址会保存在 macOS 钥匙串的 `codex-bark-push-url` 项中。

### 3. 配置 Codex

先备份配置：

```bash
cp ~/.codex/config.toml ~/.codex/config.toml.bak
```

在用户级 `~/.codex/config.toml` 中加入：

```toml
notify = ["/usr/bin/python3", "/Users/你的用户名/.codex/notifications/codex_notify_dispatcher.py"]
```

将路径中的 `你的用户名` 改为实际用户名，然后彻底退出并重新打开 Codex。

Codex 官方配置参考说明，`notify` 是一个字符串数组，对应的命令会收到 Codex 传入的 JSON 数据。通知配置只能放在用户级配置中，不能由项目级配置覆盖：

- https://developers.openai.com/codex/config-reference/

### 4. 测试

```bash
/usr/bin/python3 ~/.codex/notifications/codex_notify_dispatcher.py --test-push complete
/usr/bin/python3 ~/.codex/notifications/codex_notify_dispatcher.py --test-push confirmation
/usr/bin/python3 ~/.codex/notifications/codex_notify_dispatcher.py --test-push authorization
```

三条测试通知都到达后，再执行一次真实 Codex 任务验证最终回复通知。

## 已有 notify 配置怎么办

不要直接覆盖原配置，否则可能导致已有通知功能失效。`notify` 本身只接受一条命令链；如果已经使用其他通知程序，需要自行编写一个分发脚本，同时调用原程序和本项目脚本。

本仓库不自动修改 `config.toml`，就是为了避免破坏现有通知链。

## 隐私与安全

- Bark 个人推送地址等同于设备密钥，不要提交到 GitHub、截图或日志。
- 本项目只从 macOS 钥匙串读取 Bark 地址。
- 为提高通知准确性，脚本会只读检查本机 Codex 的状态数据库和会话记录，确认最终回复是否真实落盘。
- 审计日志只记录字段名、任务/回合标识、项目目录名以及回复摘要，不记录回复正文。
- 如果任务内容高度敏感，建议使用自建 Bark Server，或不要启用远程推送。

## 准确性边界

- “任务已完成”以 Codex 最终回复为准，不能代表外部系统操作一定成功。
- “等待确认”主要依据最终回复中的行动句式识别，可能误判或漏判。
- “等待授权”优先识别 Codex 授权事件；不同 Codex 版本的事件结构可能变化。
- 脚本会读取 Codex 本地状态文件，这是实现细节，Codex 升级后可能需要适配。
- 网络、Bark 服务、iPhone 通知设置和手表镜像均可能影响送达。

## 卸载

1. 从 `~/.codex/config.toml` 删除或恢复原来的 `notify` 配置。
2. 删除 `~/.codex/notifications/codex_notify_dispatcher.py`。
3. 如需删除 Bark 地址：

```bash
security delete-generic-password -a "$USER" -s codex-bark-push-url
```

## License

MIT
