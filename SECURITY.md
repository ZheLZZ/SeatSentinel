# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| `0.2.2-beta` | Yes |
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

- SeatSentinel 只调用 Windows 锁屏，不实现或绕过 Windows 登录与解锁；
- 仅本人模式是一对一在场判断，不提供活体检测，不得作为身份认证或 Windows
  Hello 的替代方案；照片或屏幕视频可能造成误识别；
- 本人人脸模板通过当前 Windows 用户的 DPAPI 加密保存，删除操作会永久移除模板；
- 摄像头或推理状态不明确时禁止自动锁屏；
- 模型下载使用 HTTPS，并校验固定 SHA-256；
- 正常监控不需要管理员权限；
- 项目当前处于 beta 阶段，使用前应在可控环境测试。

## Unsigned builds

本仓库只发布源码。维护者自行生成的未签名 EXE 可能触发 Windows SmartScreen
或安全软件提示。请勿从非官方 Issue、网盘或第三方站点下载可执行文件。
