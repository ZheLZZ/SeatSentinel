# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| `0.2.5-beta` | Yes |
| `0.2.4-beta` | No |
| `0.2.3-beta` | No |
| `0.2.1-beta` | No |
| `0.2.0-beta` | No |
| `0.1.1-beta` | No |
| `0.1.0-beta` | No |
| Earlier development snapshots | No |

## Reporting a vulnerability

请优先使用 GitHub 仓库的私密漏洞报告功能：

`https://github.com/ZheLZZ/SeatSentinel/security/advisories/new`

如果仓库尚未启用私密漏洞报告，请只创建一个不包含漏洞细节的 Issue，请求
维护者提供私密联系方式。不要在公开 Issue 中发布利用代码、摄像头画面、
本地路径、日志中的个人信息或其他敏感内容。

报告中建议包含：

- 受影响版本；
- Windows、Python 和 OpenVINO 版本；
- 可复现步骤；
- 预期行为与实际行为；
- 风险影响；
- 已做脱敏的日志片段。

## Security boundaries

- 系统模式调用 Windows 锁屏；可选应用锁屏只提供普通进程内的密码遮挡，不是 Windows
  安全桌面、操作系统登录或可靠的本机访问控制。管理员、Ctrl+Alt+Del、任务管理器、
  进程终止、同账户配置修改及系统移除输入钩子都可能绕过它；不能用于替代强安全锁屏；
- 应用密码仅以随机盐 PBKDF2-HMAC-SHA256 派生值保存在本地设置中。错误密码有渐进
  等待，但不抵御同账户对配置或进程的篡改；不提供防进程终止或内核级键盘过滤；
- 解锁状态下可直接在设置中重设应用密码，无需验证旧密码；锁定期间不能打开或保存设置；
- 应用模式通过 Windows 保持唤醒接口请求防止空闲休眠及关闭屏幕；不修改系统策略，
  不承诺阻止所有系统锁屏或网络断线；
- 仅本人模式是一对一在场判断，不提供活体检测，不得作为身份认证或 Windows
  Hello 的替代方案；照片或屏幕视频可能造成误识别；
- 第二人隐私毛玻璃属于尽力而为的置顶遮挡，不是 Windows 安全桌面或访问控制；
  需要强安全边界时应使用系统锁屏；
- 本人人脸模板通过当前 Windows 用户的 DPAPI 加密保存，删除操作会永久移除模板；
- 摄像头或推理状态不明确时禁止自动锁屏；
- 模型下载使用 HTTPS，并校验固定 SHA-256；
- 正常监控不需要管理员权限；
- 项目当前处于 beta 阶段，使用前应在可控环境测试。

## Unsigned builds

本仓库只发布源码。维护者自行生成的未签名 EXE 可能触发 Windows SmartScreen
或安全软件提示。请勿从非官方 Issue、网盘或第三方站点下载可执行文件。
